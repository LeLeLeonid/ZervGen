from typing import List, Dict
from src.core.provider import AIProvider, ProviderMeta, _record_failure, _record_success
from src.config import GeminiSettings
from src.utils import async_retry

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

META = ProviderMeta(name="gemini", display_name="Google Gemini", module="src.providers.gemini")


def fetch_available_models(api_key: str) -> List[str]:
    if not GEMINI_AVAILABLE:
        return ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.0-flash"]
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


class Provider(AIProvider):
    META = META
    
    def __init__(self, settings: GeminiSettings):
        if not GEMINI_AVAILABLE:
            raise ImportError("google-generativeai not installed. Run: pip install google-generativeai")
        self.settings = settings
        if not self.settings.api_key:
            raise ValueError("Gemini API Key is not set.")
        genai.configure(api_key=self.settings.api_key)
        self.model = genai.GenerativeModel(self.settings.model)

    @classmethod
    def get_model_name(cls, config) -> str:
        return config.gemini.model

    @async_retry()
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        try:
            gemini_history = []
            for msg in history:
                role = "user" if msg["role"] == "user" else "model"
                gemini_history.append({"role": role, "parts": [msg["content"]]})

            chat = self.model.start_chat(history=gemini_history)
            response = await chat.send_message_async(f"System Instruction: {system_prompt}\n\nTask: Generate response.")
            _record_success("gemini")
            return response.text
        except Exception as e:
            _record_failure("gemini")
            raise e

    async def generate_image(self, prompt: str) -> str:
        raise NotImplementedError("Gemini does not support direct image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("Gemini does not support direct audio generation")

    async def analyze_image(self, prompt: str, image_url: str) -> str:
        raise NotImplementedError("Gemini vision not implemented")
