import requests
import logging
from urllib.parse import quote
from ..config import settings
import yt_dlp
import tempfile
import base64

class ApiClientError(Exception):
    """
    Custom exception for API client errors.
    """
    pass

class PollinationsClient:
    """
    A robust and professional client for the Pollinations.AI API.
    """
    def __init__(self, api_token: str | None = None):
        self.base_image_url = "https://image.pollinations.ai/prompt/"
        self.base_text_url = "https://text.pollinations.ai/openai"
        self.headers = {"Content-Type": "application/json"}

        if api_token:
            self.headers["Authorization"] = f"Bearer {api_token}"
        
        self.VALID_IMAGE_PARAMS = {'model', 'width', 'height', 'seed', 'nologo', 'enhance', 'private'}
        self.VALID_TEXT_PARAMS = {'model', 'temperature', 'max_tokens', 'stream', 'tools', 'reasoning_effort'}

        self.default_image_params = settings.api_settings.get('pollinations', {}).get('default_image_params', {})

    def _handle_request(self, method, url, **kwargs) -> requests.Response:
        """
        A centralized method to handle API requests and errors.
        """
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            raise ApiClientError(f"API Error {e.response.status_code}: {e.response.text}")
        except requests.exceptions.RequestException as e:
            raise ApiClientError(f"Network Error: {e}")

    def generate_image(self, **kwargs) -> bytes:
        """
        Generates an image using only valid, whitelisted parameters.
        """
        prompt = kwargs.pop('prompt', 'abstract art')
        url = f"{self.base_image_url}{quote(prompt)}"

        final_params = self.default_image_params.copy()
        final_params.update(kwargs)

        valid_params = {key: value for key, value in final_params.items() if key in self.VALID_IMAGE_PARAMS}
        
        response = self._handle_request(
            "get", url, params=valid_params, headers=self.headers, 
            timeout=settings.performance.get('timeout_image', 180)
        )
        return response.content

    def generate_text(self, **kwargs) -> str:
        """
        Generates text using only valid, whitelisted parameters.
        """
        if 'prompt' not in kwargs:
            raise ValueError("generate_text requires a 'prompt' parameter.")
            
        prompt_content = kwargs.pop('prompt')
        payload = {"messages": [{"role": "user", "content": prompt_content}]}
        valid_params = {key: value for key, value in kwargs.items() if key in self.VALID_TEXT_PARAMS}
        payload.update(valid_params)
        
        response = self._handle_request(
            "post", self.base_text_url, json=payload, headers=self.headers, 
            timeout=settings.performance.get('timeout_text', 90)
        )
        data = response.json()
        
        try:
            return data['choices'][0]['message']['content'].strip()
        except (KeyError, IndexError, TypeError):
            raise ApiClientError(f"API returned an unparsable response: {data}")
    
    def transcribe_youtube(self, url: str) -> str:
        """
        Downloads audio from a YouTube URL, transcribes it, and returns the text.
        """
        if not url:
            raise ValueError("YouTube URL cannot be empty.")

        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
                'preferredquality': '192',
            }],
            'outtmpl': f'{tempfile.gettempdir()}/%(id)s.%(ext)s', # Save to temp dir
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                audio_file_path = f"{tempfile.gettempdir()}/{info['id']}.wav"
            
            with open(audio_file_path, "rb") as f:
                audio_data = base64.b64encode(f.read()).decode()

            payload = {
                "model": "openai-audio",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this audio:"},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": audio_data, "format": "wav"}
                        }
                    ]
                }]
            }
            response = self._handle_request("post", self.base_text_url, json=payload, timeout=600) # Longer timeout
            data = response.json()
            return data['choices'][0]['message']['content'].strip()

        except Exception as e:
            raise ApiClientError(f"Failed to transcribe YouTube video. Error: {e}")