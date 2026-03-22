import json
import re
from typing import List, Dict
from src.core.provider import AIProvider, ProviderMeta, get_http_client, _record_failure, _record_success
from src.config import GroqSettings
from src.utils import async_retry, RateLimitError

META = ProviderMeta(name="groq", display_name="Groq", module="src.providers.groq")


class Provider(AIProvider):
    META = META
    
    def __init__(self, settings: GroqSettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("Groq API Key is not set.")
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json"
        }

    @classmethod
    def get_model_name(cls, config) -> str:
        return config.groq.model

    @async_retry(retries=3, delays=[1, 3, 5])
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        messages = [{"role": "system", "content": system_prompt}] + history
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "top_p": self.settings.top_p
        }
        
        client = get_http_client()
        try:
            resp = await client.post(self.base_url, headers=self.headers, json=payload)
            
            if resp.status_code == 429:
                _record_failure("groq")
                raise RateLimitError("Groq rate limit", retry_after=5.0)
            
            if resp.status_code != 200:
                _record_failure("groq")
                raise Exception(f"Groq HTTP {resp.status_code}")
            
            data = resp.json()
            if "error" in data:
                _record_failure("groq")
                raise Exception(f"Groq API Error: {data['error']}")
            
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            if not content:
                raise Exception("Groq returned empty content")
            
            _record_success("groq")
            return content
            
        except Exception as e:
            _record_failure("groq")
            raise e

    async def generate_image(self, prompt: str) -> str:
        raise NotImplementedError("Groq does not support image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("Groq does not support audio generation")

    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        raise NotImplementedError("Groq vision not implemented")
