import os
import httpx
import platform
import subprocess
import time
from pathlib import Path
from typing import List, Dict
from src.core.provider import AIProvider
from src.config import GlobalSettings

class Orchestrator:
    def __init__(self, provider: AIProvider, settings: GlobalSettings):
        self.provider = provider
        self.settings = settings
        self.history: List[Dict[str, str]] = []
        self.tmp_dir = Path(self.settings.pollinations.output_path)
        self.tmp_dir.mkdir(exist_ok=True)

    def _play_audio(self, file_path: Path):
        system = platform.system()
        str_path = str(file_path.absolute())
        
        try:
            if system == "Windows":
                cmd = f'powershell -c (New-Object Media.SoundPlayer "{str_path}").PlaySync()'
                subprocess.Popen(cmd, shell=True)
            elif system == "Darwin":
                subprocess.Popen(["afplay", str_path])
            else:
                subprocess.Popen(["xdg-open", str_path])
        except Exception:
            pass

    async def _download_image(self, url: str) -> Path:
        filename = f"img_{int(time.time())}.jpg"
        path = self.tmp_dir / filename
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            with open(path, "wb") as f:
                f.write(resp.content)
        return path

    async def process(self, user_input: str) -> str:
        self.history.append({"role": "user", "content": user_input})
        
        if user_input.lower().startswith("/image"):
            prompt = user_input.replace("/image", "").strip()
            url = await self.provider.generate_image(prompt)
            local_path = await self._download_image(url)
            
            if platform.system() == "Windows": os.startfile(local_path)
            elif platform.system() == "Darwin": subprocess.run(["open", str(local_path)])
            
            response = f"Image saved: {local_path}\nURL: {url}"
        
        elif user_input.lower().startswith("/speak"):
            text = user_input.replace("/speak", "").strip()
            try:
                audio_bytes = await self.provider.generate_audio(text)
                filename = f"audio_{int(time.time())}.wav"
                path = self.tmp_dir / filename
                
                with open(path, "wb") as f:
                    f.write(audio_bytes)
                
                self._play_audio(path)
                response = f"Audio playing... (Saved to {path})"
            except Exception as e:
                response = f"Audio Generation Failed: {str(e)}"
            
        else:
            response = await self.provider.generate_text(self.history, "You are ZervGen (The Orchestrator).")

        self.history.append({"role": "assistant", "content": response})
        return response