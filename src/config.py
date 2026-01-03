import json
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel, Field

CONFIG_PATH = Path("config.json")

class PollinationsSettings(BaseModel):
    api_key: Optional[str] = None
    text_model: str = "openai"
    image_model: str = "flux"
    audio_model: str = "openai-audio"
    voice: str = "nova"
    reasoning_effort: str = "minimal"
    image_width: int = 1024
    image_height: int = 1024
    output_path: str = "tmp"

class GeminiSettings(BaseModel):
    api_key: Optional[str] = None
    model: str = "gemini-2.0-flash"
    temperature: float = 0.7

class OpenRouterSettings(BaseModel):
    api_key: Optional[str] = None
    model: str = "google/gemini-2.0-flash-exp:free"
    site_url: str = "https://github.com/LeLeLeonid/ZervGen"
    app_name: str = "ZervGen"

class GlobalSettings(BaseModel):
    provider: Literal["pollinations", "gemini", "openrouter"] = "pollinations"
    max_steps: int = 100
    history_limit: int = 15
    pollinations: PollinationsSettings = Field(default_factory=PollinationsSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)

    def save(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.model_dump(), f, indent=4)

def load_config() -> GlobalSettings:
    if not CONFIG_PATH.exists():
        defaults = GlobalSettings()
        defaults.save()
        return defaults
    with open(CONFIG_PATH, "r") as f:
        return GlobalSettings.model_validate(json.load(f))