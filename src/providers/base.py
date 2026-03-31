import json
import logging
from typing import List, Dict, Callable, Optional
from src.core.provider import get_http_client, _record_failure, _record_success

logger = logging.getLogger(__name__)


class OpenAIProvider:
    def __init__(self, name: str, base_url: str, headers: dict, model: str):
        self.name = name
        self.base_url = base_url
        self.headers = headers
        self.model = model

    def get_model_name(self, config=None):
        return self.model

    async def _non_stream(self, payload: dict) -> str:
        client = get_http_client()
        resp = await client.post(self.base_url, headers=self.headers, json=payload)
        if resp.status_code == 429:
            raise Exception(f"{self.name} rate limited (429)")
        if resp.status_code != 200:
            body = resp.text[:500]
            raise Exception(f"{self.name} HTTP {resp.status_code}: {body}")
        data = resp.json()
        if "error" in data:
            err = data["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise Exception(f"{self.name} error: {msg}")
        choices = data.get("choices")
        if not choices:
            raise Exception(f"{self.name} no choices in response: {json.dumps(data)[:300]}")
        content = choices[0].get("message", {}).get("content", "")
        if not content:
            finish = choices[0].get("finish_reason", "unknown")
            raise Exception(f"{self.name} empty content (finish_reason={finish})")
        return content

    async def _stream(self, payload: dict, on_token: Callable) -> str:
        client = get_http_client()
        full = ""
        async with client.stream("POST", self.base_url, headers=self.headers, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise Exception(f"{self.name} HTTP {resp.status_code}: {body.decode()[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if "error" in data:
                    err = data["error"]
                    msg = err.get("message", err) if isinstance(err, dict) else err
                    raise Exception(f"{self.name} stream error: {msg}")
                delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    full += delta
                    on_token(delta)
        return full

    async def generate_text(self, history: List[Dict], system_prompt: str, on_token: Callable = None) -> str:
        messages = [{"role": "system", "content": system_prompt}] + history
        payload = {"model": self.model, "messages": messages, "temperature": 0.7}

        if on_token:
            try:
                result = await self._stream(payload, on_token)
                if result.strip():
                    _record_success(self.name)
                    return result
            except Exception:
                pass

        try:
            result = await self._non_stream(payload)
            _record_success(self.name)
            return result
        except Exception as e:
            _record_failure(self.name)
            raise e

    async def generate_image(self, prompt): raise NotImplementedError
    async def generate_audio(self, text): raise NotImplementedError
    async def analyze_image(self, prompt, image): raise NotImplementedError
