from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class AIProvider(ABC):
    
    @abstractmethod
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        pass

    @abstractmethod
    async def generate_image(self, prompt: str) -> str:
        pass

    @abstractmethod
    async def generate_audio(self, text: str) -> bytes:
        pass

    @abstractmethod
    async def analyze_image(self, prompt: str, image_url: str) -> str:
        pass