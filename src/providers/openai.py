import httpx
import json
from typing import List, Dict
from src.core.provider import AIProvider
from src.config import OpenAISettings
from src.utils import async_retry

class OpenAIProvider(AIProvider):
    def __init__(self, settings: OpenAISettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("OpenAI API Key is not set.")
        
        self.base_url = "https://api.openai.com/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json"
        }

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        messages = [{"role": "system", "content": system_prompt}] + history
        
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0.7,
            "stream": False
        }
        
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(self.base_url, headers=self.headers, json=payload, timeout=60)
                
                if resp.status_code != 200:
                    error_text = resp.text
                    try:
                        err_json = resp.json()
                        if "error" in err_json:
                            error_text = json.dumps(err_json["error"])
                    except: pass
                    raise Exception(f"OpenAI HTTP {resp.status_code}: {error_text}")
                
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    raise Exception(f"OpenAI returned invalid JSON: {resp.text[:200]}")
                
                if "error" in data:
                    raise Exception(f"OpenAI API Error: {data['error']}")
                
                if "choices" not in data or not data["choices"]:
                    raise Exception(f"OpenAI returned empty choices. Raw response: {data}")
                
                content = data['choices'][0]['message'].get('content')
                
                if not content:
                    raise Exception("OpenAI returned empty content string.")
                
                return content
                
            except httpx.TimeoutException:
                raise Exception("OpenAI Timeout (60s). The model is too slow or down.")
            except Exception as e:
                raise e

    async def generate_image(self, prompt: str) -> str:
        return "OpenAI does not support direct image generation. Use Pollinations."

    async def generate_audio(self, text: str) -> bytes:
        return b"OpenAI does not support audio."

    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        return "OpenAI does not support vision in this provider. Use OpenRouter."