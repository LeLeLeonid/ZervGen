import os
import json
from dotenv import load_dotenv, set_key
from pathlib import Path

class Settings:
    """Centralized configuration management."""
    def __init__(self):
        self.project_root = Path(__file__).parent.parent
        self.env_path = self.project_root / ".env"
        self.config_path = self.project_root / "config.json"
        
        self._load_env()
        self._load_config()

    def _load_env(self):
        if not self.env_path.exists():
            self.env_path.touch()
        load_dotenv(dotenv_path=self.env_path)
        self.pollinations_api_token: str | None = os.getenv("POLLINATIONS_API_TOKEN")

    def _load_config(self):
        if not self.config_path.exists():
            raise FileNotFoundError("config.json not found in the project root.")
        with open(self.config_path, 'r') as f:
            config_data = json.load(f)
        
        self.features = config_data.get('features', {})
        self.api_settings = config_data.get('api_settings', {})
        self.performance = config_data.get('performance', {})

    def save_token(self, token: str):
        set_key(self.env_path, "POLLINATIONS_API_TOKEN", token)
        self.pollinations_api_token = token

settings = Settings()