import google.generativeai as genai
from typing import List, Dict
from src.core.provider import AIProvider
from src.config import GeminiSettings
from src.utils import async_retry

def fetch_available_models(api_key: str) -> List[str]:
    try:
        genai.configure(api_key=api_key)
        models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                name = m.name.replace("models/", "")
                models.append(name)
        return models
    except Exception:
        return ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.0-flash"]

class GeminiProvider(AIProvider):
    def __init__(self, settings: GeminiSettings):
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("Gemini API Key is not set in configuration.")
        
        genai.configure(api_key=self.settings.api_key)
        self.model = genai.GenerativeModel(self.settings.model)

    @async_retry()
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        gemini_history = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            gemini_history.append({"role": role, "parts": [msg["content"]]})

        chat = self.model.start_chat(history=gemini_history)
        response = await chat.send_message_async(f"System Instruction: {system_prompt}\n\nTask: Generate response.")
        return response.text

    async def generate_image(self, prompt: str) -> str:
        return "Gemini Image Gen not configured. Orchestrator should route this to Pollinations."

    async def generate_audio(self, text: str) -> bytes:
        return b"Gemini Audio Gen not configured. Orchestrator should route this to Pollinations."

    async def analyze_image(self, prompt: str, image_url: str) -> str:
        return "Vision capabilities pending implementation."