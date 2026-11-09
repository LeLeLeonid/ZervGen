import httpx
import logging
import os
import time
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "0.0.0.0", "::1"}


class ProviderError(Exception):
    """A provider failure that callers may retry or surface as-is."""

    def __init__(self, message: str, status_code: Optional[int] = None, retryable: bool = True,
                 stream_started: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.stream_started = stream_started


class CircuitBreakerError(ProviderError):
    def __init__(self, provider: str):
        super().__init__(f"{provider} circuit open", retryable=False)
        self.provider = provider


def normalize_messages(history: List[Dict], system_prompt: str = "") -> List[Dict]:
    merged: List[Dict] = []
    if system_prompt:
        merged.append({"role": "system", "content": system_prompt})
    for msg in history or []:
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("system", "user", "assistant"):
            role = "assistant"
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
        content = str(content or "").strip()
        if not content:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] = f"{merged[-1]['content']}\n\n{content}"
        else:
            merged.append({"role": role, "content": content})
    first = 0
    while first < len(merged) and merged[first]["role"] == "system":
        first += 1
    if first < len(merged) and merged[first]["role"] != "user":
        merged.insert(first, {"role": "user", "content": "Continue."})
    return merged


def split_system(messages: List[Dict]) -> tuple[str, List[Dict]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    return system, rest


def sse_payload(line: str) -> Optional[str]:
    if not line or not line.startswith("data:"):
        return None
    raw = line[5:].strip()
    if not raw or raw == "[DONE]":
        return None
    return raw


def raise_for_status(response: httpx.Response, provider: str) -> None:
    if response.status_code == 200:
        return
    try:
        body = response.text[:300]
    except Exception:
        body = ""
    retryable = response.status_code in (408, 409, 425, 429) or response.status_code >= 500
    raise ProviderError(f"{provider} HTTP {response.status_code}: {body}", status_code=response.status_code, retryable=retryable)


def is_local_url(url: Optional[str]) -> bool:
    try:
        return (httpx.URL(url or "").host or "").lower() in LOCAL_HOSTS
    except Exception:
        return False


class CircuitBreaker:
    def __init__(self, threshold: int = 5, reset_seconds: float = 30.0):
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        self._failures: Dict[str, int] = {}
        self._open_at: Dict[str, float] = {}

    def is_available(self, provider: str) -> bool:
        if provider not in self._open_at:
            return True
        if time.time() - self._open_at[provider] > self._reset_seconds:
            del self._open_at[provider]
            self._failures[provider] = 0
            return True
        return False

    def remaining(self, provider: str) -> float:
        if provider not in self._open_at:
            return 0.0
        return max(0.0, self._reset_seconds - (time.time() - self._open_at[provider]))

    def record_failure(self, provider: str) -> None:
        self._failures[provider] = self._failures.get(provider, 0) + 1
        if self._failures[provider] >= self._threshold:
            self._open_at[provider] = time.time()
            logger.info(f"circuit open: {provider}")

    def record_success(self, provider: str) -> None:
        self._failures[provider] = 0
        self._open_at.pop(provider, None)

    def get_health(self, providers: List[str]) -> Dict[str, Dict[str, Any]]:
        return {
            name: {
                "failures": self._failures.get(name, 0),
                "circuit_open": name in self._open_at,
                "available": self.is_available(name),
            }
            for name in providers
        }


_circuit_breaker = CircuitBreaker()


class AIProvider:
    name = ""
    META = None

    def __init__(self, base_url: str, api_key: str = "", model: str = "", headers: Optional[Dict[str, str]] = None, timeout: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.headers = dict(headers) if headers is not None else {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.model = model
        self.timeout = httpx.Timeout(timeout, connect=5.0)
        self._reset_usage()

    def _reset_usage(self) -> None:
        self._last_usage: Dict[str, Any] = {}
        self._last_cost = 0.0

    def _preflight(self) -> None:
        self._reset_usage()
        if not _circuit_breaker.is_available(self.name):
            raise CircuitBreakerError(self.name)

    def _succeed(self) -> None:
        _circuit_breaker.record_success(self.name)

    def _fail(self) -> None:
        _circuit_breaker.record_failure(self.name)

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Cost in USD from the catalog; local and unknown models are free."""
        from src.utils import ModelCatalog
        if is_local_url(getattr(self, "base_url", "")):
            return 0.0
        return float(ModelCatalog.get_cost(self.name, self.model, input_tokens, output_tokens) or 0.0)

    def _record_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        self._last_usage = dict(usage or {})
        input_tokens = int(self._last_usage.get("prompt_tokens") or self._last_usage.get("input_tokens") or 0)
        output_tokens = int(self._last_usage.get("completion_tokens") or self._last_usage.get("output_tokens") or 0)
        self._last_cost = float(self._estimate_cost(input_tokens, output_tokens) or 0.0)

    async def generate_text(self, history: List[Dict], system_prompt: str, on_token: callable = None) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def get_model_name(self) -> str:
        return self.model


class HttpClientManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._client = None
        return cls._instance

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                timeout=httpx.Timeout(120.0, connect=30.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


_http_manager = HttpClientManager()


class ProviderMeta:
    def __init__(self, name: str, display_name: str, requires_key: bool = True, available: Optional[bool] = None):
        self.name = name
        self.display_name = display_name
        self.requires_key = requires_key
        self.available = (not requires_key) if available is None else available


def _preset(name: str, settings) -> Optional[Dict[str, str]]:
    from src.config import _get_all_presets
    return _get_all_presets(settings).get(name)


def _section_name(name: str) -> str:
    return "gemini" if name == "google" else name


def _tier_model(settings, name: str) -> str:
    """Model chosen by the active tier for this provider, if any."""
    tiers = getattr(settings, "model_tiers", None) or {}
    tier = str(getattr(settings, "active_model_tier", "") or "").strip()
    table = tiers.get(tier) if isinstance(tiers, dict) else None
    if not isinstance(table, dict):
        return ""
    return str(table.get(name) or table.get("*") or "")


def _resolve_model(name: str, settings, preset: Optional[Dict[str, str]]) -> str:
    chosen = _tier_model(settings, name) or (getattr(settings, "provider_models", None) or {}).get(name)
    if chosen:
        return chosen
    section = getattr(settings, _section_name(name), None) if settings is not None else None
    configured = None
    if section is not None:
        configured = getattr(section, "model", None) or getattr(section, "text_model", None)
    if configured:
        return configured
    env = os.environ.get(f"{name.upper().replace('-', '_')}_MODEL")
    if env:
        return env
    if preset and preset.get("default_model"):
        return preset["default_model"]
    from src.utils import ModelCatalog
    return ModelCatalog.default_model(name) or "default"


def _api_key(name: str, settings, preset: Optional[Dict[str, str]]) -> str:
    section = getattr(settings, _section_name(name), None) if settings is not None else None
    key = getattr(section, "api_key", None) if section is not None else None
    if key:
        return key
    env = (preset or {}).get("api_key_env") or ""
    if env:
        value = os.environ.get(env, "")
        if value:
            return value
    return os.environ.get(f"{name.upper().replace('-', '_')}_API_KEY", "")


NATIVE_PROVIDERS: Dict[str, str] = {
    "openrouter": "src.providers.openrouter",
    "openai": "src.providers.openai",
    "anthropic": "src.providers.anthropic",
    "google": "src.providers.gemini",
    "pollinations": "src.providers.pollinations",
}


def get_provider(name: str, settings) -> AIProvider:
    from src.config import _build_preset_headers, provider_dialect, resolve_provider
    key = resolve_provider(name)
    preset = _preset(key, settings)
    if key in NATIVE_PROVIDERS:
        module = __import__(NATIVE_PROVIDERS[key], fromlist=["Provider"])
        section = getattr(settings, _section_name(key), settings)
        provider = module.Provider(section)
        chosen = (getattr(settings, "provider_models", None) or {}).get(key)
        if chosen:
            provider.model = chosen
        return provider
    if not preset:
        raise ValueError(f"provider '{name}' not found")
    model = _resolve_model(key, settings, preset)
    api_key = _api_key(key, settings, preset)
    dialect = provider_dialect(key)
    if dialect == "anthropic":
        from src.providers.anthropic import Provider
        return Provider.from_endpoint(key, preset["base_url"], api_key, model)
    if dialect == "google":
        from src.providers.gemini import Provider
        return Provider.from_endpoint(key, preset["base_url"], api_key, model)
    from src.providers.base import OpenAIProvider
    headers = _build_preset_headers(preset, api_key)
    return OpenAIProvider(name=key, base_url=preset["base_url"], headers=headers, model=model)


def get_model_name(provider_name: str, settings) -> str:
    from src.config import resolve_provider
    key = resolve_provider(provider_name)
    return _resolve_model(key, settings, _preset(key, settings))


def _models_request(name: str, settings) -> tuple:
    from src.config import _build_preset_headers
    preset = _preset(name, settings) or {}
    api_key = _api_key(name, settings, preset)
    bearer = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if name == "google":
        return ("https://generativelanguage.googleapis.com/v1beta/models", {}, {"key": api_key, "pageSize": 1000})
    if name == "anthropic":
        return ("https://api.anthropic.com/v1/models",
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"} if api_key else {"anthropic-version": "2023-06-01"},
                {"limit": 1000})
    if name == "openai":
        return ("https://api.openai.com/v1/models", bearer, {})
    if name == "openrouter":
        return ("https://openrouter.ai/api/v1/models", bearer, {})
    if name == "pollinations":
        return ("https://text.pollinations.ai/models", {}, {})
    base_url = (preset.get("base_url") or "").rstrip("/")
    for suffix in ("/chat/completions", "/messages"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
            break
    if not base_url:
        return ("", {}, {})
    return (f"{base_url}/models", _build_preset_headers(preset, api_key), {})


def _parse_models(data) -> List[str]:
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    names = []
    for item in items:
        if isinstance(item, dict):
            identifier = item.get("id") or item.get("name") or item.get("model")
        else:
            identifier = item
        if not identifier:
            continue
        names.append(str(identifier).split("/", 1)[-1])
    return sorted(set(names))


def list_live_models(provider_name: str, settings=None) -> List[str]:
    from src.config import resolve_provider
    key = resolve_provider(provider_name)
    url, headers, params = _models_request(key, settings)
    if not url:
        return []
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=8.0, follow_redirects=True)
        response.raise_for_status()
        return _parse_models(response.json())
    except Exception as exc:
        logger.debug("%s model listing failed: %s", key, exc)
        return []


def list_providers() -> List[ProviderMeta]:
    from src.config import _get_all_presets
    results: List[ProviderMeta] = []
    native = {
        "pollinations": "src.providers.pollinations",
        "openrouter": "src.providers.openrouter",
        "openai": "src.providers.openai",
        "anthropic": "src.providers.anthropic",
        "gemini": "src.providers.gemini",
    }
    for module_path in native.values():
        try:
            mod = __import__(module_path, fromlist=["META"])
            meta = getattr(mod, "META", None)
            if meta:
                results.append(meta)
        except Exception:
            pass
    seen = {m.name for m in results}
    for name, preset in sorted(_get_all_presets().items()):
        if name in seen:
            continue
        env = preset.get("api_key_env") or ""
        results.append(ProviderMeta(name=name, display_name=name.replace("-", " ").title(), requires_key=bool(env), available=not env or bool(os.environ.get(env))))
    return results


def get_http_client() -> httpx.AsyncClient:
    return _http_manager.get_client()


async def close_http_client() -> None:
    await _http_manager.close()


def _record_failure(provider: str) -> None:
    _circuit_breaker.record_failure(provider)


def _record_success(provider: str) -> None:
    _circuit_breaker.record_success(provider)