import httpx
from typing import List, Dict
from urllib.parse import quote
from src.core.provider import AIProvider
from src.config import PollinationsSettings
from src.utils import async_retry

class PollinationsProvider(AIProvider):
    def __init__(self, settings: PollinationsSettings):
        self.settings = settings
        self.headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            self.headers["Authorization"] = f"Bearer {self.settings.api_key}"
            
        self.base_url_text = "https://text.pollinations.ai"
        self.base_url_image = "https://image.pollinations.ai/prompt"

    def _clean_response(self, text: str) -> str:
        ad_marker = "Support Pollinations.AI:"
        if ad_marker in text:
            return text.split(ad_marker)[0].strip()
        return text

    @async_retry()
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        
        short_history = history[-10:] if len(history) > 10 else history
        
        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "model": self.settings.text_model,
                    "messages": [{"role": "system", "content": system_prompt}] + short_history,
                    "temperature": 0.7,
                    "stream": False
                }
                if self.settings.reasoning_effort != "minimal":
                    payload["reasoning_effort"] = self.settings.reasoning_effort

                resp = await client.post(f"{self.base_url_text}/openai", json=payload, headers=self.headers, timeout=60)
                
                if resp.status_code == 402: raise Exception("Tier Restriction")
                resp.raise_for_status()
                raw_text = resp.json()['choices'][0]['message']['content']
                return self._clean_response(raw_text)
        except Exception:
            # Fallback
            conversation = f"System: {system_prompt}\n"
            for msg in short_history: conversation += f"{msg['role']}: {msg['content']}\n"
            safe_prompt = quote(conversation[-4000:]) 
            url = f"{self.base_url_text}/{safe_prompt}?model={self.settings.text_model}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=60)
                return self._clean_response(resp.text)

    @async_retry()
    async def generate_image(self, prompt: str) -> str:
        safe_prompt = quote(prompt)
        params = f"width={self.settings.image_width}&height={self.settings.image_height}&nologo=true&enhance=true"
        return f"{self.base_url_image}/{safe_prompt}?{params}&model={self.settings.image_model}"

    @async_retry()
    async def generate_audio(self, text: str) -> bytes:
        url = f"{self.base_url_text}/{safe_text}?model=openai-audio&voice={self.settings.voice}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=60)
            if resp.status_code != 200: raise Exception("API Error")
            return resp.content

    async def analyze_image(self, prompt: str, image_url: str) -> str:
        return "Vision not available."