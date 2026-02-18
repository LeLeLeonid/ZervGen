import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
import httpx
from src.config import GlobalSettings

logger = logging.getLogger(__name__)

HTTP_CLIENT: Optional[httpx.AsyncClient] = None
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_RESET = 60.0
_provider_failures: Dict[str, int] = {}
_circuit_open: Dict[str, float] = {}


def get_http_client() -> httpx.AsyncClient:
    global HTTP_CLIENT
    if HTTP_CLIENT is None or HTTP_CLIENT.is_closed:
        HTTP_CLIENT = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            timeout=httpx.Timeout(120.0, connect=30.0)
        )
    return HTTP_CLIENT


async def close_http_client() -> None:
    global HTTP_CLIENT
    if HTTP_CLIENT and not HTTP_CLIENT.is_closed:
        await HTTP_CLIENT.aclose()
    HTTP_CLIENT = None


def _check_circuit(provider: str) -> bool:
    if provider not in _circuit_open:
        return True
    if time.time() - _circuit_open[provider] > CIRCUIT_BREAKER_RESET:
        del _circuit_open[provider]
        _provider_failures[provider] = 0
        return True
    return False


def _record_failure(provider: str) -> None:
    _provider_failures[provider] = _provider_failures.get(provider, 0) + 1
    if _provider_failures[provider] >= CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open[provider] = time.time()
        logger.warning(f"Circuit breaker OPEN for {provider}")


def _record_success(provider: str) -> None:
    _provider_failures[provider] = 0
    if provider in _circuit_open:
        del _circuit_open[provider]


@runtime_checkable
class AIProvider(Protocol):
    @abstractmethod
    async def generate_text(self, history: List[Dict[str, str]], system_prompt: str) -> str:
        ...

    @abstractmethod
    def get_model_name(self) -> str:
        ...


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
        import importlib
        module = importlib.import_module(meta.module)
        return getattr(module, "Provider", None)
    except Exception as e:
        logger.error(f"Failed to load provider {name}: {e}")
        return None


def get_provider(name: str, settings: GlobalSettings) -> Any:
    if not _check_circuit(name):
        raise Exception(f"Circuit breaker OPEN for {name}. Provider temporarily unavailable.")
    
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
    return {
        name: {
            "failures": _provider_failures.get(name, 0),
            "circuit_open": name in _circuit_open,
            "available": _check_circuit(name)
        }
        for name in PROVIDERS
    }
