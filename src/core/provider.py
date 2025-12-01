from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class AIProvider(ABC):
    """
    The Universal Contract. 
    Any AI added to ZervGen MUST implement these methods.
    """
    
    @abstractmethod
    async def generate_text(self, history: List[Dict], system_prompt: str) -> str:
        """Standard chat completion."""
        pass

    @abstractmethod
    async def generate_image(self, prompt: str) -> str:
        """Returns URL or File Path of generated image."""
        pass

    @abstractmethod
    async def generate_audio(self, text: str) -> bytes:
        """Returns raw audio bytes (MP3/WAV)."""
        pass

    @abstractmethod
    async def analyze_image(self, prompt: str, image_url: str) -> str:
        """Vision capabilities."""
        pass