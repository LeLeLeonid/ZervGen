import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Protocol, runtime_checkable
import httpx

logger = logging.getLogger(__name__)


class CircuitBreakerError(Exception):
    pass


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

    def record_failure(self, provider: str) -> None:
        self._failures[provider] = self._failures.get(provider, 0) + 1
        if self._failures[provider] >= self._threshold:
            self._open_at[provider] = time.time()
            logger.warning(f"Circuit breaker OPEN for {provider}")

    def record_success(self, provider: str) -> None:
        self._failures[provider] = 0
        self._open_at.pop(provider, None)

    def get_health(self, providers: List[str]) -> Dict[str, Dict[str, Any]]:
        return {
            name: {
                "failures": self._failures.get(name, 0),
                "circuit_open": name in self._open_at,
                "available": self.is_available(name)
            }
            for name in providers
        }


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
                timeout=httpx.Timeout(120.0, connect=30.0)
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


_circuit_breaker = CircuitBreaker()
_http_manager = HttpClientManager()


@runtime_checkable
class AIProvider(Protocol):
    async def generate_text(self, history: List[Dict[str, str]], system_prompt: str, on_token=None) -> str: ...
    def get_model_name(self) -> str: ...


@dataclass
class ProviderMeta:
    name: str
    display_name: str
    requires_key: bool = True


def get_provider(name: str, settings) -> Any:
    from src.config import COMPATIBLE_PRESETS
    if not _circuit_breaker.is_available(name):
        raise CircuitBreakerError(f"{name} circuit open")
    if name == "openrouter":
        from src.providers.openrouter import Provider
        return Provider(getattr(settings, 'openrouter', settings))
    if name == "openai":
        from src.providers.openai import Provider
        return Provider(getattr(settings, 'openai', settings))
    if name == "anthropic":
        from src.providers.anthropic import Provider
        return Provider(getattr(settings, 'anthropic', settings))
    if name == "gemini":
        from src.providers.gemini import Provider
        return Provider(getattr(settings, 'gemini', settings))
    if name == "pollinations":
        from src.providers.pollinations import Provider
        return Provider(getattr(settings, 'pollinations', settings))
    preset = COMPATIBLE_PRESETS.get(name)
    if preset:
        from src.providers.base import OpenAIProvider
        provider_settings = getattr(settings, name.lower(), None)
        api_key = getattr(provider_settings, 'api_key', '') if provider_settings else ''
        model = getattr(provider_settings, 'model', None) if provider_settings else None
        if not model:
            model = preset.get('default_model', 'default')
        headers = preset["headers_fn"](api_key)
        headers["Content-Type"] = "application/json"
        return OpenAIProvider(name=name, base_url=preset["base_url"], headers=headers, model=model)
    raise ValueError(f"Provider '{name}' not found")


def get_model_name(provider_name: str, settings) -> str:
    provider_settings = getattr(settings, provider_name.lower(), None)
    if provider_settings and hasattr(provider_settings, "model"):
        return provider_settings.model
    return "default"


def list_providers() -> list:
    results = []
    provider_modules = {
        "pollinations": "src.providers.pollinations",
        "openrouter": "src.providers.openrouter",
        "openai": "src.providers.openai",
        "anthropic": "src.providers.anthropic",
        "gemini": "src.providers.gemini",
    }
    for name, module_path in provider_modules.items():
        try:
            mod = __import__(module_path, fromlist=["META"])
            if hasattr(mod, "META"):
                results.append(mod.META)
        except Exception:
            pass
    from src.config import COMPATIBLE_PRESETS
    for name in COMPATIBLE_PRESETS:
        results.append(ProviderMeta(name=name, display_name=name.capitalize(), requires_key=True))
    return results


def get_http_client() -> httpx.AsyncClient:
    return _http_manager.get_client()


async def close_http_client() -> None:
    await _http_manager.close()


def _record_failure(provider: str) -> None:
    _circuit_breaker.record_failure(provider)


def _record_success(provider: str) -> None:
    _circuit_breaker.record_success(provider)
