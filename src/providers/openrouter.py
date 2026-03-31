import json
from typing import List, Dict
from src.core.provider import AIProvider, ProviderMeta, get_http_client, _record_failure, _record_success
from src.config import OpenRouterSettings
from src.utils import async_retry

META = ProviderMeta(name="openrouter", display_name="OpenRouter")


class Provider(AIProvider):
    META = META

    def __init__(self, settings: OpenRouterSettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("OpenRouter API Key is not set.")
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.settings.site_url,
            "X-Title": self.settings.app_name
        }

    @classmethod
    def get_model_name(cls, config) -> str:
        return config.openrouter.model.split("/")[-1]

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_text(self, history: List[Dict], system_prompt: str, on_token=None) -> str:
        messages = [{"role": "system", "content": system_prompt}] + history
        payload = {"model": self.settings.model, "messages": messages, "temperature": 0.7}

        client = get_http_client()
        try:
            if on_token:
                payload["stream"] = True
                async with client.stream("POST", self.base_url, headers=self.headers, json=payload) as resp:
                    if resp.status_code != 200:
                        _record_failure("openrouter")
                        raise Exception(f"OpenRouter HTTP {resp.status_code}")
                    full = ""
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices",[{}])[0].get("delta",{}).get("content","")
                            if delta:
                                full += delta
                                on_token(delta)
                        except json.JSONDecodeError:
                            pass
                    _record_success("openrouter")
                    return full
            else:
                resp = await client.post(self.base_url, headers=self.headers, json=payload)

                if resp.status_code != 200:
                    _record_failure("openrouter")
                    raise Exception(f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}")

                data = resp.json()
                if "error" in data:
                    _record_failure("openrouter")
                    raise Exception(f"OpenRouter API Error: {data['error']}")

                choices = data.get('choices', [])
                if not choices:
                    _record_failure("openrouter")
                    raise Exception("OpenRouter returned no choices")

                content = choices[0].get('message', {}).get('content', '')
                if not content:
                    _record_failure("openrouter")
                    raise Exception("OpenRouter returned empty content")

                _record_success("openrouter")
                return content

        except Exception as e:
            _record_failure("openrouter")
            raise e

    async def generate_image(self, prompt: str) -> str:
        raise NotImplementedError("OpenRouter does not support direct image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("OpenRouter does not support audio generation")

    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        raise NotImplementedError("OpenRouter vision not implemented")
