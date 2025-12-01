import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

CONFIG_PATH = Path("config.json")

class PollinationsSettings(BaseModel):
    text_model: str = "openai"
    image_model: str = "flux"
    audio_model: str = "openai-audio"
    voice: str = "nova"
    reasoning_effort: str = "minimal"
    safe_filter: bool = True
    image_width: int = 1024
    image_height: int = 1024
    output_path: str = "tmp"

class GlobalSettings(BaseModel):
    provider: Literal["pollinations"] = "pollinations"
    pollinations: PollinationsSettings = Field(default_factory=PollinationsSettings)

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