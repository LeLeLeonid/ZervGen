from typing import List, Dict
from src.core.provider import AIProvider, ProviderMeta, get_http_client, _record_failure, _record_success
from src.config import LocalSettings

META = ProviderMeta(name="local", display_name="Local (Ollama/LM Studio)", module="src.providers.local", requires_key=False)


class Provider(AIProvider):
    META = META

    def __init__(self, settings: LocalSettings):
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if settings.api_key:
            self.headers["Authorization"] = f"Bearer {settings.api_key}"

    @classmethod
    def get_model_name(cls, config) -> str:
        return config.local.model

    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        messages = [{"role": "system", "content": system_prompt}] + history
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens
        }
        
        client = get_http_client()
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
            if resp.status_code != 200:
                _record_failure("local")
                raise Exception(f"Local HTTP {resp.status_code}")
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            _record_success("local")
            return content
        except Exception as e:
            _record_failure("local")
            raise e

    async def generate_image(self, prompt: str) -> str:
        raise NotImplementedError("Local provider does not support image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("Local provider does not support audio generation")

    async def analyze_image(self, prompt: str, image_path: str) -> str:
        raise NotImplementedError("Local provider does not support vision")
