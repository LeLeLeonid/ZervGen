import json
import logging
from typing import Any, Callable, Dict, List, Optional

import httpx

from src.core.provider import (
    AIProvider,
    ProviderError,
    get_http_client,
    normalize_messages,
    raise_for_status,
)

logger = logging.getLogger(__name__)


class OpenAIProvider(AIProvider):
    def __init__(self, name: str, base_url: str, headers: Dict[str, str], model: str):
        endpoint = (base_url or "").rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
        super().__init__(base_url=endpoint, model=model, headers=headers)
        self.name = name
        self._cached_prefix_hash = None

    def _is_reasoning_model(self) -> bool:
        from src.utils import ModelCatalog

        flag = ModelCatalog.supports(self.name, self.model, "reasoning")
        if flag is not None:
            return flag
        lowered = (self.model or "").lower()
        return any(k in lowered for k in ("o1", "o3", "o4", "r1", "reasoning", "deepseek-r"))

    def _timeout(self) -> Optional[httpx.Timeout]:
        return httpx.Timeout(180.0, connect=10.0) if self._is_reasoning_model() else None

    def _payload(self, history: List[Dict], system_prompt: str, stream: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": normalize_messages(history, system_prompt),
            "stream": bool(stream),
        }
        if not self._is_reasoning_model():
            payload["temperature"] = 0.7
        return payload

    def _content(self, message: Dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return str(content or "").strip()

    def _delta(self, chunk: Dict[str, Any]) -> str:
        """Stream fragments verbatim: whitespace-only deltas are load-bearing."""
        content = chunk.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return str(content or "")

    async def _non_stream(self, payload: Dict[str, Any]) -> str:
        kwargs: Dict[str, Any] = {"headers": self.headers, "json": payload}
        timeout = self._timeout()
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = await get_http_client().post(self.base_url, **kwargs)
        raise_for_status(response, self.name)
        data = response.json()
        error = data.get("error")
        if error:
            message = error.get("message", error) if isinstance(error, dict) else error
            raise ProviderError(f"{self.name} error: {message}", retryable=False)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name} returned no choices", retryable=True)
        content = self._content(choices[0].get("message") or {})
        if not content:
            finish = choices[0].get("finish_reason", "unknown")
            raise ProviderError(f"{self.name} returned empty content (finish_reason={finish})", retryable=False)
        self._record_usage(data.get("usage"))
        return content

    async def _stream(self, payload: Dict[str, Any], on_token: Callable[[str], None]) -> str:
        kwargs: Dict[str, Any] = {"headers": self.headers, "json": payload}
        timeout = self._timeout()
        if timeout is not None:
            kwargs["timeout"] = timeout
        full = ""
        async with get_http_client().stream("POST", self.base_url, **kwargs) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")[:300]
                retryable = response.status_code in (408, 409, 425, 429) or response.status_code >= 500
                raise ProviderError(
                    f"{self.name} HTTP {response.status_code}: {body}",
                    status_code=response.status_code,
                    retryable=retryable,
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                error = event.get("error")
                if error:
                    message = error.get("message", error) if isinstance(error, dict) else error
                    raise ProviderError(f"{self.name} stream error: {message}", retryable=True, stream_started=True)
                if isinstance(event.get("usage"), dict):
                    self._record_usage(event["usage"])
                choices = event.get("choices") or []
                if not choices:
                    continue
                text = self._delta(choices[0].get("delta") or {})
                if text:
                    full += text
                    on_token(text)
        if not full.strip():
            raise ProviderError(f"{self.name} streamed empty content", retryable=True)
        return full

    async def _dispatch(self, history: List[Dict], system_prompt: str, on_token: Optional[Callable[[str], None]]) -> str:
        if not on_token:
            return await self._non_stream(self._payload(history, system_prompt, False))
        emitted = False

        def _relay(text: str) -> None:
            nonlocal emitted
            emitted = True
            on_token(text)

        try:
            return await self._stream(self._payload(history, system_prompt, True), _relay)
        except ProviderError as exc:
            if exc.status_code is not None or exc.stream_started or emitted:
                raise
            logger.debug("%s stream failed, retrying without streaming: %s", self.name, exc)
        return await self._non_stream(self._payload(history, system_prompt, False))

    async def generate_text(self, history: List[Dict], system_prompt: str, on_token: Callable = None) -> str:
        self._preflight()
        try:
            result = await self._dispatch(history, system_prompt, on_token)
        except Exception:
            self._fail()
            raise
        self._succeed()
        return result

    async def generate_image(self, prompt: str, width: Optional[int] = None, height: Optional[int] = None) -> str:
        raise NotImplementedError(f"{self.name} does not support image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError(f"{self.name} does not support audio generation")

    async def analyze_image(self, prompt: str, image: str) -> str:
        raise NotImplementedError(f"{self.name} does not support image analysis")