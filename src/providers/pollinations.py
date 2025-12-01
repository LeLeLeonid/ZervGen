import httpx
from typing import List, Dict
from urllib.parse import quote
from src.core.provider import AIProvider
from src.config import PollinationsSettings

class PollinationsProvider(AIProvider):
    def __init__(self, settings: PollinationsSettings):
        self.settings = settings
        self.headers = {"Content-Type": "application/json"}
        self.base_url_text = "https://text.pollinations.ai"
        self.base_url_image = "https://image.pollinations.ai/prompt"

    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "model": self.settings.text_model,
                    "messages": [{"role": "system", "content": system_prompt}] + history,
                    "temperature": 0.7,
                    "stream": False
                }
                
                if self.settings.reasoning_effort and self.settings.reasoning_effort not in ["minimal", ""]:
                    payload["reasoning_effort"] = self.settings.reasoning_effort

                resp = await client.post(
                    f"{self.base_url_text}/openai", 
                    json=payload, 
                    headers=self.headers, 
                    timeout=60
                )
                
                if resp.status_code == 402:
                    raise Exception("Tier Restriction")
                
                resp.raise_for_status()
                return resp.json()['choices'][0]['message']['content']
        
        except Exception:
            conversation = f"System: {system_prompt}\n"
            for msg in history:
                conversation += f"{msg['role'].capitalize()}: {msg['content']}\n"
            
            safe_prompt = quote(conversation[-4000:]) 
            url = f"{self.base_url_text}/{safe_prompt}?model={self.settings.text_model}"
            
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=60)
                return resp.text

    async def generate_image(self, prompt: str) -> str:
        safe_prompt = quote(prompt)
        params = f"width={self.settings.image_width}&height={self.settings.image_height}&nologo=true&enhance=true"
        return f"{self.base_url_image}/{safe_prompt}?{params}&model={self.settings.image_model}"

    async def generate_audio(self, text: str) -> bytes:
        safe_text = quote(text)
        url = f"{self.base_url_text}/{safe_text}?model=openai-audio&voice={self.settings.voice}"
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=60)
            
            if resp.status_code == 402:
                raise Exception("Audio generation requires a paid Pollinations API tier.")
            
            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type or resp.status_code != 200:
                raise Exception(f"API Error: {resp.status_code}")
                
            return resp.content

    async def analyze_image(self, prompt: str, image_url: str) -> str:
        payload = {
            "model": "openai",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]}]
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url_text}/openai", json=payload, headers=self.headers, timeout=60)
            return resp.json()['choices'][0]['message']['content']