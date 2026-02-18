import json
from typing import List, Dict
from src.core.provider import AIProvider, ProviderMeta, get_http_client, _record_failure, _record_success
from src.config import SiliconFlowSettings
from src.utils import async_retry

META = ProviderMeta(name="siliconflow", display_name="SiliconFlow", module="src.providers.siliconflow")


class Provider(AIProvider):
    META = META
    
    def __init__(self, settings: SiliconFlowSettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("SiliconFlow API Key is not set.")
        self.base_url = "https://api.siliconflow.cn/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json"
        }

    @classmethod
    def get_model_name(cls, config) -> str:
        return config.siliconflow.model.split("/")[-1]

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        messages = [{"role": "system", "content": system_prompt}] + history
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "top_p": self.settings.top_p,
            "stream": False
        }
        
        client = get_http_client()
        try:
            resp = await client.post(self.base_url, headers=self.headers, json=payload)
            
            if resp.status_code != 200:
                _record_failure("siliconflow")
                raise Exception(f"SiliconFlow HTTP {resp.status_code}")
            
            data = resp.json()
            if "error" in data:
                _record_failure("siliconflow")
                raise Exception(f"SiliconFlow API Error: {data['error']}")
            
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            if not content:
                raise Exception("SiliconFlow returned empty content")
            
            _record_success("siliconflow")
            return content
            
        except Exception as e:
            _record_failure("siliconflow")
            raise e

    async def generate_image(self, prompt: str) -> str:
        raise NotImplementedError("SiliconFlow does not support image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("SiliconFlow does not support audio generation")

    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        raise NotImplementedError("SiliconFlow vision not implemented")
