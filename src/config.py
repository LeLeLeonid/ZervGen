import os
from dotenv import load_dotenv, set_key
from pathlib import Path

class Settings:
    """Centralized configuration management."""
    def __init__(self):
        self.project_root = Path(__file__).parent.parent
        self.env_path = self.project_root / ".env"
        
        # Ensure .env file exists
        if not self.env_path.exists():
            self.env_path.touch()

        load_dotenv(dotenv_path=self.env_path)
        
        # API Settings
        self.pollinations_api_token: str | None = os.getenv("POLLINATIONS_API_TOKEN")

        # Performance Settings
        self.TIMEOUT_IMAGE_GENERATION: int = 180
        self.TIMEOUT_TEXT_GENERATION: int = 90

    def save_token(self, token: str):
        """Saves or updates the token in the .env file."""
        set_key(self.env_path, "POLLINATIONS_API_TOKEN", token)
        self.pollinations_api_token = token

settings = Settings()