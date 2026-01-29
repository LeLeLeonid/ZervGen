import httpx
import json
from typing import List, Dict
from src.core.provider import AIProvider
from src.config import GroqSettings
from src.utils import async_retry


class GroqProvider(AIProvider):
    def __init__(self, settings: GroqSettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("Groq API Key is not set. Get one at https://console.groq.com")
        
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json"
        }

    @async_retry(retries=3, delays=[1, 3, 5])
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        """Generate text using Groq's fast inference API."""
        messages = [{"role": "system", "content": system_prompt}] + history
        
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "top_p": self.settings.top_p,
            "stream": False
        }
        
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    self.base_url, 
                    headers=self.headers, 
                    json=payload, 
                    timeout=60
                )
                
                if resp.status_code != 200:
                    error_text = resp.text
                    try:
                        err_json = resp.json()
                        if "error" in err_json:
                            error_text = json.dumps(err_json["error"])
                    except:
                        pass
                    raise Exception(f"Groq HTTP {resp.status_code}: {error_text}")
                
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    raise Exception(f"Groq returned invalid JSON: {resp.text[:200]}")
                
                if "error" in data:
                    raise Exception(f"Groq API Error: {data['error']}")
                
                if "choices" not in data or not data["choices"]:
                    raise Exception(f"Groq returned empty choices. Raw response: {data}")
                
                content = data['choices'][0]['message'].get('content')
                
                if not content:
                    raise Exception("Groq returned empty content string.")
                
                return content
                
            except httpx.TimeoutException:
                raise Exception("Groq Timeout (60s). The model is too slow or down.")
            except Exception as e:
                raise e

    async def generate_image(self, prompt: str) -> str:
        """Groq does not support image generation."""
        return "Groq does not support image generation. Use Pollinations."

    async def generate_audio(self, text: str) -> bytes:
        """Groq does not support audio generation."""
        return b"Groq does not support audio."

    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        """Groq does not support vision in the current implementation."""
        return "Groq vision support not implemented. Use OpenRouter or Gemini."