from src.config import OpenAISettings
from src.core.provider import ProviderMeta
from src.providers.base import OpenAIProvider

META = ProviderMeta(name="openai", display_name="OpenAI")

BASE_URL = "https://api.openai.com/v1"


class Provider(OpenAIProvider):
    META = META

    def __init__(self, settings: OpenAISettings):
        if not settings.api_key:
            raise ValueError("OpenAI API Key is not set.")
        super().__init__(
            name="openai",
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
            model=settings.model,
        )
        self.settings = settings