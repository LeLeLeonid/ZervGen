import httpx
import json
from typing import List, Dict
from src.core.provider import AIProvider
from src.config import OpenRouterSettings
from src.utils import async_retry
from rich.console import Console

console = Console()

class OpenRouterProvider(AIProvider):
    def __init__(self, settings: OpenRouterSettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("OpenRouter API Key is not set.")
        
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "HTTP-Referer": self.settings.site_url,
            "X-Title": self.settings.app_name,
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
                
                # 1. Check HTTP Status
                if resp.status_code != 200:
                    error_text = resp.text
                    try:
                        err_json = resp.json()
                        if "error" in err_json:
                            error_text = json.dumps(err_json["error"])
                    except: pass
                    raise Exception(f"OpenRouter HTTP {resp.status_code}: {error_text}")

                # 2. Parse JSON
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    raise Exception(f"OpenRouter returned invalid JSON (possibly Cloudflare error): {resp.text[:200]}")

                # 3. Validate Structure
                if "error" in data:
                    raise Exception(f"OpenRouter API Error: {data['error']}")
                
                if "choices" not in data or not data["choices"]:
                    raise Exception(f"OpenRouter returned empty choices. Raw response: {data}")

                content = data['choices'][0]['message'].get('content')
                
                if not content:
                    raise Exception("OpenRouter returned empty content string.")

                return content

            except httpx.TimeoutException:
                raise Exception("OpenRouter Timeout (60s). The model is too slow or down.")
            except Exception as e:
                # Re-raise to trigger retry logic
                raise e

    async def generate_image(self, prompt: str) -> str:
        return "OpenRouter does not support direct image generation. Use Pollinations."

    async def generate_audio(self, text: str) -> bytes:
        return b"OpenRouter does not support audio."

    async def analyze_image(self, prompt: str, image_url: str) -> str:
        return "Vision not implemented for OpenRouter yet."