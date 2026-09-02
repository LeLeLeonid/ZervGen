import httpx
import asyncio
import logging
import re
import time
import os
from typing import List, Dict, Optional, Any

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
            logger.info(f"Circuit breaker OPEN for {provider}")

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

_circuit_breaker = CircuitBreaker()

class AIProvider:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.model = model
        self.timeout = httpx.Timeout(timeout, connect=5.0)
        self.client = httpx.AsyncClient(timeout=self.timeout)
        self.name = model

    async def generate_text(self, history: List[Dict], system_prompt: str, on_token: callable = None) -> str:
        payload = {"model": self.model, "messages": history}
        if system_prompt:
            payload["messages"] = [{"role": "system", "content": system_prompt}] + payload["messages"]

        if any(k in self.model.lower() for k in ["o1", "o3", "r1", "reasoning", "deepseek-r"]):
            payload["temperature"] = 0.0

        try:
            resp = await self.client.post(f"{self.base_url}/chat/completions", json=payload, headers=self.headers)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except httpx.TimeoutException:
            return "Error: Provider timeout. Task halted."
        except Exception as e:
            logger.error(f"Provider fail: {e}")
            return f"Error: Provider request failed -> {str(e)[:200]}"

    async def _handle_stream(self, resp: httpx.Response) -> str:
        full = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                data = line[6:]
                if data.strip() == "[DONE]": break
                try:
                    chunk = __import__("json").loads(data)
                    content = chunk["choices"][0]["delta"].get("content", "")
                    if content: full.append(content)
                except: pass
        return "".join(full)

    async def close(self):
        await self.client.aclose()

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
                timeout=httpx.Timeout(120.0, connect=30.0)
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

_http_manager = HttpClientManager()

class ProviderMeta:
    def __init__(self, name: str, display_name: str, requires_key: bool = True):
        self.name = name
        self.display_name = display_name
        self.requires_key = requires_key

def get_provider(name: str, settings) -> AIProvider:
    from src.config import COMPATIBLE_PRESETS, _get_all_presets, _build_preset_headers
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
    all_presets = _get_all_presets(settings)
    preset = all_presets.get(name)
    if preset:
        from src.providers.base import OpenAIProvider
        model = os.environ.get(f"{name.upper()}_MODEL") or preset.get("default_model") or "default"
        headers = _build_preset_headers(preset)
        return OpenAIProvider(name=name, base_url=preset["base_url"], headers=headers, model=model)
    raise ValueError(f"Provider '{name}' not found")

def get_model_name(provider_name: str, settings) -> str:
    provider_settings = getattr(settings, provider_name.lower(), None)
    if provider_settings and hasattr(provider_settings, "model") and provider_settings.model:
        return provider_settings.model
    _env_model = os.environ.get(f"{provider_name.upper()}_MODEL")
    if _env_model:
        return _env_model
    from src.config import _get_all_presets
    preset = _get_all_presets(settings).get(provider_name)
    if preset:
        return preset.get("default_model", "default")
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
    from src.config import _get_all_presets
    display_map = {"koboldcpp": "KoboldCpp", "ollama": "Ollama", "lmstudio": "LM Studio",
                   "siliconflow": "SiliconFlow", "groq": "Groq", "deepseek": "DeepSeek"}
    for name, preset in _get_all_presets().items():
        requires_key = bool(preset.get("api_key_env"))
        results.append(ProviderMeta(name=name, display_name=display_map.get(name, name.capitalize()), requires_key=requires_key))
    return results

def get_http_client() -> httpx.AsyncClient:
    return _http_manager.get_client()

async def close_http_client() -> None:
    await _http_manager.close()

def _record_failure(provider: str) -> None:
    _circuit_breaker.record_failure(provider)

def _record_success(provider: str) -> None:
    _circuit_breaker.record_success(provider)
    
def resolve_model_tier(tier: str, settings) -> tuple[str, str]:
    tiers = getattr(settings, "model_tiers", {}) or {}
    cfg = tiers.get(str(tier).upper(), {}) if isinstance(tiers, dict) else {}
    provider = cfg.get("provider") or getattr(settings, "provider", "pollinations")
    model = cfg.get("model") or get_model_name(provider, settings)
    return provider, model
