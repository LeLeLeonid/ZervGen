from typing import List, Dict
from urllib.parse import quote
from src.core.provider import AIProvider, ProviderMeta, get_http_client, _record_failure, _record_success
from src.config import PollinationsSettings
from src.utils import async_retry

META = ProviderMeta(name="pollinations", display_name="Pollinations (Default)", module="src.providers.pollinations", requires_key=False)


class Provider(AIProvider):
    META = META
    
    def __init__(self, settings: PollinationsSettings):
        self.settings = settings
        self.headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            self.headers["Authorization"] = f"Bearer {self.settings.api_key}"
        self.base_url_text = "https://text.pollinations.ai"
        self.base_url_image = "https://image.pollinations.ai/prompt"

    @classmethod
    def get_model_name(cls, config) -> str:
        return config.pollinations.text_model

    def _clean_response(self, text: str) -> str:
        ad_marker = "Support Pollinations.AI:"
        if ad_marker in text:
            return text.split(ad_marker)[0].strip()
        return text

    def _check_errors(self, response):
        if response.status_code in [500, 502, 503, 504]:
            raise Exception(f"Server Error: {response.status_code}")
        text = response.text.lower()
        if "bad gateway" in text or "cloudflare" in text or "service unavailable" in text:
            raise Exception(f"Gateway Error: {text[:100]}...")

    @async_retry(retries=5, delays=[1, 2, 5, 10, 20])
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        short_history = history[-10:] if len(history) > 10 else history
        client = get_http_client()
        
        try:
            payload = {
                "model": self.settings.text_model,
                "messages": [{"role": "system", "content": system_prompt}] + short_history,
                "temperature": 0.7
            }
            if self.settings.reasoning_effort != "minimal":
                payload["reasoning_effort"] = self.settings.reasoning_effort

            resp = await client.post(f"{self.base_url_text}/openai", json=payload, headers=self.headers)
            self._check_errors(resp)
            
            if resp.status_code == 402:
                raise Exception("Tier Restriction")
            resp.raise_for_status()
            
            json_resp = resp.json()
            content = json_resp['choices'][0]['message']['content']
            if not content or not content.strip():
                raise Exception("API returned empty content")
            _record_success("pollinations")
            return self._clean_response(content)
            
        except Exception as e:
            if "Tier Restriction" in str(e):
                _record_failure("pollinations")
                raise e

            # Fallback to GET request
            conversation = f"System: {system_prompt}\n"
            for msg in short_history:
                conversation += f"{msg['role']}: {msg['content']}\n"
            safe_prompt = quote(conversation[-4000:])
            url = f"{self.base_url_text}/{safe_prompt}?model={self.settings.text_model}"
            
            resp = await client.get(url)
            self._check_errors(resp)
            _record_success("pollinations")
            cleaned = self._clean_response(resp.text)
            if not cleaned or not cleaned.strip():
                raise Exception("Fallback API returned empty response")
            return cleaned

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_image(self, prompt: str) -> str:
        safe_prompt = quote(prompt)
        params = f"width={self.settings.image_width}&height={self.settings.image_height}&nologo=true&enhance=true"
        return f"{self.base_url_image}/{safe_prompt}?{params}&model={self.settings.image_model}"

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_audio(self, text: str) -> bytes:
        safe_text = quote(text)
        url = f"{self.base_url_text}/{safe_text}?model=openai-audio&voice={self.settings.voice}"
        client = get_http_client()
        resp = await client.get(url)
        self._check_errors(resp)
        if resp.status_code != 200:
            raise Exception("API Error")
        return resp.content

    async def analyze_image(self, prompt: str, image_url: str) -> str:
        raise NotImplementedError("Pollinations does not support image analysis")
