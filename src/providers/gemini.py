import json
import logging
from typing import Any, Callable, Dict, List, Optional

import httpx

from src.config import GeminiSettings
from src.core.provider import (
    AIProvider,
    ProviderError,
    ProviderMeta,
    get_http_client,
    normalize_messages,
    raise_for_status,
    split_system,
    sse_payload,
)
from src.utils import async_retry

META = ProviderMeta(name="gemini", display_name="Google Gemini")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_OUTPUT_TOKENS = 8192
LIST_TIMEOUT = 10.0

logger = logging.getLogger(__name__)


class Provider(AIProvider):
    META = META

    def __init__(self, settings: GeminiSettings):
        if not settings.api_key:
            raise ValueError("Gemini API Key is not set.")
        super().__init__(BASE_URL, api_key=settings.api_key, model=settings.model)
        self.name = "gemini"
        self.settings = settings
        self.temperature = settings.temperature

    @classmethod
    def from_settings(cls, settings: GeminiSettings) -> "Provider":
        return cls(settings)

    @classmethod
    def from_endpoint(cls, name: str, base_url: str, api_key: str, model: str) -> "Provider":
        if not api_key:
            raise ValueError(f"{name} API Key is not set.")
        provider = cls.__new__(cls)
        AIProvider.__init__(provider, base_url or BASE_URL, api_key=api_key, model=model)
        provider.name = name
        provider.settings = None
        provider.temperature = 0.7
        return provider

    def _endpoint(self, method: str, stream: bool) -> str:
        suffix = f"?alt=sse&key={self.api_key}" if stream else f"?key={self.api_key}"
        return f"{self.base_url}/models/{self.model}:{method}{suffix}"

    def _payload(self, history: List[Dict], system_prompt: str, stream: bool) -> Dict[str, Any]:
        system, messages = split_system(normalize_messages(history, system_prompt))
        contents = []
        for message in messages:
            role = "model" if message["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message["content"]}]})
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def _track_usage(self, data: Dict[str, Any]) -> None:
        usage = data.get("usageMetadata") or {}
        if usage:
            self._record_usage({
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
            })

    def _content(self, data: Dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            raise ProviderError(
                f"{self.name} returned no candidates (blockReason={feedback.get('blockReason')})",
                retryable=False,
            )
        parts = (candidates[0].get("content") or {}).get("parts") or []
        content = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
        if not content:
            reason = candidates[0].get("finishReason", "unknown")
            raise ProviderError(f"{self.name} returned empty content (finishReason={reason})", retryable=False)
        return content

    async def _non_stream(self, payload: Dict[str, Any]) -> str:
        response = await get_http_client().post(self._endpoint("generateContent", False), json=payload)
        raise_for_status(response, self.name)
        data = response.json()
        content = self._content(data)
        self._track_usage(data)
        return content

    async def _stream(self, payload: Dict[str, Any], on_token: Callable[[str], None]) -> str:
        full = ""
        async with get_http_client().stream(
            "POST", self._endpoint("streamGenerateContent", True), json=payload
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")[:300]
                retryable = response.status_code in (408, 409, 425, 429) or response.status_code >= 500
                raise ProviderError(
                    f"{self.name} HTTP {response.status_code}: {body}",
                    status_code=response.status_code,
                    retryable=retryable,
                )
            async for line in response.aiter_lines():
                raw = sse_payload(line)
                if raw is None:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                error = event.get("error")
                if error:
                    message = error.get("message", error) if isinstance(error, dict) else error
                    raise ProviderError(f"{self.name} stream error: {message}", retryable=True, stream_started=True)
                self._track_usage(event)
                for candidate in event.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        delta = part.get("text") or ""
                        if delta:
                            full += delta
                            on_token(delta)
        if not full.strip():
            raise ProviderError(f"{self.name} streamed empty content", retryable=True)
        return full

    @async_retry()
    async def generate_text(self, history: List[Dict], system_prompt: str, on_token: Optional[Callable[[str], None]] = None) -> str:
        self._preflight()
        try:
            if on_token:
                try:
                    result = await self._stream(self._payload(history, system_prompt, True), on_token)
                except ProviderError as exc:
                    if exc.status_code is not None or exc.stream_started:
                        raise
                    logger.debug("%s stream failed, falling back: %s", self.name, exc)
                else:
                    self._succeed()
                    return result
            result = await self._non_stream(self._payload(history, system_prompt, False))
        except Exception:
            self._fail()
            raise
        self._succeed()
        return result

    async def generate_image(self, prompt: str, width: Optional[int] = None, height: Optional[int] = None) -> str:
        raise NotImplementedError("Gemini does not support direct image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("Gemini does not support direct audio generation")

    async def analyze_image(self, prompt: str, image_url: str) -> str:
        raise NotImplementedError("Gemini vision not implemented")