import google.generativeai as genai
from typing import List, Dict
from src.core.provider import AIProvider
from src.config import GeminiSettings
from src.utils import async_retry

# Not Accurate 
GEMINI_MODELS = [
    "gemini-2.5-pro-preview-03-25",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash-exp",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite-preview-02-05",
    "gemini-2.0-pro-exp-02-05",
    "gemini-flash-latest",
    "gemini-pro-latest",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-1.5-flash-8b",
    "gemini-exp-1206",
    "gemini-2.0-flash-thinking-exp-01-21",
    "learnlm-2.0-flash-experimental",
    "gemma-3-27b-it"
]

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