import json
from typing import List, Dict
from src.core.provider import AIProvider, ProviderMeta, get_http_client, _record_failure, _record_success
from src.config import OpenAISettings
from src.utils import async_retry

META = ProviderMeta(name="openai", display_name="OpenAI", module="src.providers.openai")


class Provider(AIProvider):
    META = META
    
    def __init__(self, settings: OpenAISettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("OpenAI API Key is not set.")
        self.base_url = "https://api.openai.com/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json"
        }

    @classmethod
    def get_model_name(cls, config) -> str:
        return config.openai.model

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_text(self, history: List[Dict], system_prompt: str, stream: bool = False) -> str:
        messages = [{"role": "system", "content": system_prompt}] + history
        payload = {"model": self.settings.model, "messages": messages, "temperature": 0.7, "stream": stream}
        
        client = get_http_client()
        try:
            if stream:
                async with client.stream("POST", self.base_url, headers=self.headers, json=payload) as response:
                    if response.status_code != 200:
                        _record_failure("openai")
                        raise Exception(f"OpenAI HTTP {response.status_code}")
                    full_content = ""
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                if "choices" in data:
                                    delta = data["choices"][0].get("delta", {}).get("content", "")
                                    if delta:
                                        full_content += delta
                            except json.JSONDecodeError:
                                pass
                    _record_success("openai")
                    return full_content
            else:
                resp = await client.post(self.base_url, headers=self.headers, json=payload)
                
                if resp.status_code != 200:
                    _record_failure("openai")
                    raise Exception(f"OpenAI HTTP {resp.status_code}")
                
                data = resp.json()
                if "error" in data:
                    _record_failure("openai")
                    raise Exception(f"OpenAI API Error: {data['error']}")
                
                content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                if not content:
                    raise Exception("OpenAI returned empty content")
                
                _record_success("openai")
                return content
                
        except Exception as e:
            _record_failure("openai")
            raise e

    async def generate_image(self, prompt: str) -> str:
        raise NotImplementedError("OpenAI does not support direct image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("OpenAI does not support audio generation")

    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        raise NotImplementedError("OpenAI vision not implemented")
