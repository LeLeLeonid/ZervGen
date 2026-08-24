import json
from typing import List, Dict, Callable, Optional
from src.core.provider import AIProvider, ProviderMeta, get_http_client, _record_failure, _record_success
from src.config import AnthropicSettings
from src.utils import async_retry

META = ProviderMeta(name="anthropic", display_name="Anthropic")


class Provider(AIProvider):
    META = META

    def __init__(self, settings: AnthropicSettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("Anthropic API Key is not set.")
        self.base_url = "https://api.anthropic.com/v1/messages"
        self.headers = {
            "x-api-key": self.settings.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }

    @classmethod
    def get_model_name(cls, config) -> str:
        return config.anthropic.model

    @async_retry(retries=5, delays=[2, 5, 10, 25, 60])
    async def generate_text(self, history: List[Dict], system_prompt: str, on_token: Optional[Callable[[str], None]] = None) -> str:
        messages = [{"role": m.get("role"), "content": m.get("content", "")} for m in history]
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "system": system_prompt,
            "max_tokens": 4096,
            "temperature": 0.7,
            "stream": on_token is not None
        }

        client = get_http_client()
        try:
            if on_token:
                async with client.stream("POST", self.base_url, headers=self.headers, json=payload) as response:
                    if response.status_code != 200:
                        raise Exception(f"Anthropic HTTP {response.status_code}")
                    full_content = ""
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        try:
                            data = json.loads(data_str)
                            if data.get("type") == "content_block_delta":
                                delta = data.get("delta", {}).get("text", "")
                                if delta:
                                    full_content += delta
                                    on_token(delta)
                        except json.JSONDecodeError:
                            pass
                    _record_success("anthropic")
                    return full_content
            else:
                resp = await client.post(self.base_url, headers=self.headers, json=payload)

                if resp.status_code != 200:
                    raise Exception(f"Anthropic HTTP {resp.status_code}")

                data = resp.json()
                if "error" in data:
                    raise Exception(f"Anthropic API Error: {data['error']}")

                content = data.get('content', [{}])[0].get('text', '')
                if not content:
                    raise Exception("Anthropic returned empty content")

                _record_success("anthropic")
                return content

        except Exception as e:
            _record_failure("anthropic")
            raise e

    async def generate_image(self, prompt: str) -> str:
        raise NotImplementedError("Anthropic does not support image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("Anthropic does not support audio generation")

    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        raise NotImplementedError("Anthropic vision not implemented")
