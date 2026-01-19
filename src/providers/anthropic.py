import httpx
import json
from typing import List, Dict
from src.core.provider import AIProvider
from src.config import AnthropicSettings
from src.utils import async_retry

class AnthropicProvider(AIProvider):
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

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        messages = []
        for msg in history:
            messages.append({"role": msg.get("role"), "content": msg.get("content", "")})
        
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "system": system_prompt,
            "max_tokens": 4096,
            "temperature": 0.7
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
                    raise Exception(f"Anthropic HTTP {resp.status_code}: {error_text}")
                
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    raise Exception(f"Anthropic returned invalid JSON: {resp.text[:200]}")
                
                if "error" in data:
                    raise Exception(f"Anthropic API Error: {data['error']}")
                
                if "content" not in data or not data["content"]:
                    raise Exception(f"Anthropic returned empty content. Raw response: {data}")
                
                content = data['content'][0]['text'] if data['content'] else ""
                
                if not content:
                    raise Exception("Anthropic returned empty content string.")
                
                return content
                
            except httpx.TimeoutException:
                raise Exception("Anthropic Timeout (60s). The model is too slow or down.")
            except Exception as e:
                raise e

    async def generate_image(self, prompt: str) -> str:
        return "Anthropic does not support direct image generation. Use Pollinations."

    async def generate_audio(self, text: str) -> bytes:
        return b"Anthropic does not support audio."

    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        return "Anthropic does not support vision in this provider. Use OpenRouter."