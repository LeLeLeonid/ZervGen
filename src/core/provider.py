import importlib
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
import httpx
from src.config import GlobalSettings

logger = logging.getLogger(__name__)


class CircuitBreakerError(Exception):
    pass


CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_RESET_SECONDS = 10.0

class CircuitBreaker:
    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD, reset_seconds: float = CIRCUIT_BREAKER_RESET_SECONDS):
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
    async def generate_text(self, history: List[Dict[str, str]], system_prompt: str) -> str: ...
    def get_model_name(self) -> str: ...


@dataclass
class ProviderMeta:
    name: str
    display_name: str
    module: str
    requires_key: bool = True


PROVIDERS: Dict[str, ProviderMeta] = {
    "openai": ProviderMeta("openai", "OpenAI", "src.providers.openai"),
    "anthropic": ProviderMeta("anthropic", "Anthropic", "src.providers.anthropic"),
    "gemini": ProviderMeta("gemini", "Google Gemini", "src.providers.gemini"),
    "groq": ProviderMeta("groq", "Groq", "src.providers.groq"),
    "openrouter": ProviderMeta("openrouter", "OpenRouter", "src.providers.openrouter"),
    "siliconflow": ProviderMeta("siliconflow", "SiliconFlow", "src.providers.siliconflow"),
    "pollinations": ProviderMeta("pollinations", "Pollinations (Free)", "src.providers.pollinations", requires_key=False),
    "local": ProviderMeta("local", "Local Model", "src.providers.local", requires_key=False),
}


@lru_cache(maxsize=32)
def _load_provider_class(name: str) -> Optional[type]:
    meta = PROVIDERS.get(name.lower())
    if not meta:
        return None
    try:
        module = importlib.import_module(meta.module)
        return getattr(module, "Provider", None)
    except Exception as e:
        logger.error(f"Failed to load provider {name}: {e}")
        return None


def get_provider(name: str, settings: GlobalSettings) -> Any:
    if not _circuit_breaker.is_available(name):
        logger.warning(f"Circuit breaker OPEN for {name}")
        raise CircuitBreakerError(f"Circuit breaker OPEN for {name}. Provider temporarily unavailable.")
    provider_class = _load_provider_class(name)
    if not provider_class:
        raise ValueError(f"Provider '{name}' not found")
    provider_settings = getattr(settings, name.lower(), None)
    return provider_class(provider_settings or settings)


def get_model_name(provider_name: str, settings: GlobalSettings) -> str:
    provider_settings = getattr(settings, provider_name.lower(), None)
    if provider_settings and hasattr(provider_settings, "model"):
        return provider_settings.model
    return "default"


def list_providers() -> List[ProviderMeta]:
    return list(PROVIDERS.values())


def clear_provider_cache() -> None:
    _load_provider_class.cache_clear()


def get_provider_health() -> Dict[str, Dict[str, Any]]:
    return _circuit_breaker.get_health(list(PROVIDERS.keys()))


def get_http_client() -> httpx.AsyncClient:
    return _http_manager.get_client()


async def close_http_client() -> None:
    await _http_manager.close()


def _record_failure(provider: str) -> None:
    _circuit_breaker.record_failure(provider)


def _record_success(provider: str) -> None:
    _circuit_breaker.record_success(provider)
