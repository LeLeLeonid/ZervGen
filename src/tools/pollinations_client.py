import requests
import logging
from urllib.parse import quote
from ..config import settings

class ApiClientError(Exception):
    """Custom exception for API client errors."""
    pass

class PollinationsClient:
    """
    A robust and professional client for the Pollinations.AI API.
    This version correctly handles the API response structure.
    """
    def __init__(self, api_token: str | None = None):
        self.base_image_url = "https://image.pollinations.ai/prompt/"
        self.base_text_url = "https://text.pollinations.ai/openai"
        self.headers = {"Content-Type": "application/json"}

        if api_token:
            self.headers["Authorization"] = f"Bearer {api_token}"
        
        self.VALID_IMAGE_PARAMS = {'model', 'width', 'height', 'seed', 'nologo', 'enhance', 'private'}
        self.VALID_TEXT_PARAMS = {'model', 'temperature', 'max_tokens', 'stream', 'tools', 'reasoning_effort'}

    def _handle_request(self, method, url, **kwargs) -> requests.Response:
        """A centralized method to handle API requests and errors."""
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if 400 <= status_code < 500:
                error_details = e.response.text
                raise ApiClientError(f"API returned a client error: {status_code} {e.response.reason}. Details: {error_details}")
            elif 500 <= status_code < 600:
                raise ApiClientError(f"API server error: {status_code} {e.response.reason}. This is likely a temporary issue. Please try again later.")
            else:
                raise ApiClientError(f"An unexpected HTTP error occurred: {status_code}")
        except requests.exceptions.RequestException as e:
            raise ApiClientError(f"A network error occurred: {e}")

    def generate_image(self, **kwargs) -> bytes:
        """Generates an image using only valid, whitelisted parameters."""
        prompt = kwargs.pop('prompt', 'a beautiful abstract image')
        url = f"{self.base_image_url}{quote(prompt)}"
        valid_params = {key: value for key, value in kwargs.items() if key in self.VALID_IMAGE_PARAMS}
        
        response = self._handle_request(
            "get", url, params=valid_params, headers=self.headers, 
            timeout=settings.TIMEOUT_IMAGE_GENERATION
        )
        return response.content

    def generate_text(self, **kwargs) -> str:
        """Generates text using only valid, whitelisted parameters."""
        if 'prompt' not in kwargs:
            raise ValueError("generate_text requires a 'prompt' parameter.")
            
        prompt_content = kwargs.pop('prompt')
        payload = {"messages": [{"role": "user", "content": prompt_content}]}
        valid_params = {key: value for key, value in kwargs.items() if key in self.VALID_TEXT_PARAMS}
        payload.update(valid_params)
        
        response = self._handle_request(
            "post", self.base_text_url, json=payload, headers=self.headers, 
            timeout=settings.TIMEOUT_TEXT_GENERATION
        )
        data = response.json()
        
        try:
            return data['choices'][0]['message']['content'].strip()
        except (KeyError, IndexError, TypeError):
            logging.error(f"API returned an unexpected or malformed response: {data}")
            raise ApiClientError("API returned a response that could not be parsed.")