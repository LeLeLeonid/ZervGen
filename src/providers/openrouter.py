from src.config import OpenRouterSettings
from src.core.provider import ProviderMeta
from src.providers.base import OpenAIProvider

META = ProviderMeta(name="openrouter", display_name="OpenRouter")

BASE_URL = "https://openrouter.ai/api/v1"


class Provider(OpenAIProvider):
    META = META

    def __init__(self, settings: OpenRouterSettings):
        if not settings.api_key:
            raise ValueError("OpenRouter API Key is not set.")
        super().__init__(
            name="openrouter",
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.site_url,
                "X-Title": settings.app_name,
            },
            model=settings.model,
        )
        self.settings = settings