import json
import logging
from typing import Any, Callable, Dict, List, Optional

import httpx

from src.core.provider import (
    is_provider_fault,
    AIProvider,
    ProviderError,
    get_http_client,
    normalize_messages,
    raise_for_status,
)
from src.utils import async_retry

logger = logging.getLogger(__name__)


def _decode(raw: str) -> Dict[str, Any]:
    """Parse streamed JSON arguments; an unfinished object becomes empty."""
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _rejects_tools(exc: ProviderError) -> bool:
    """True when the backend refused the request because of its tools field."""
    if exc.status_code not in (400, 404, 422):
        return False
    return "tool" in str(exc).lower()


class OpenAIProvider(AIProvider):
    NATIVE_TOOLS = True

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
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if not self._is_reasoning_model():
            payload["temperature"] = 0.7
        if self.native_tools:
            payload["tools"] = [{"type": "function", "function": spec} for spec in self.native_tools]
            payload["tool_choice"] = "auto"
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
        message = choices[0].get("message") or {}
        content = self._content(message)
        self.native_calls = self._calls(message)
        if not content and not self.native_calls:
            finish = choices[0].get("finish_reason", "unknown")
            raise ProviderError(f"{self.name} returned empty content (finish_reason={finish})", retryable=False)
        self._record_usage(data.get("usage"))
        return content

    @staticmethod
    def _calls(message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Tool calls from one assistant message, arguments decoded."""
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if function.get("name"):
                calls.append({"name": function["name"], "arguments": arguments if isinstance(arguments, dict) else {}})
        return calls

    async def _stream(self, payload: Dict[str, Any], on_token: Callable[[str], None]) -> str:
        kwargs: Dict[str, Any] = {"headers": self.headers, "json": payload}
        timeout = self._timeout()
        if timeout is not None:
            kwargs["timeout"] = timeout
        full = ""
        partial: List[Dict[str, str]] = []
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
                delta = choices[0].get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    slot = int(call.get("index") or 0)
                    while len(partial) <= slot:
                        partial.append({"name": "", "arguments": ""})
                    function = call.get("function") or {}
                    if function.get("name"):
                        partial[slot]["name"] = function["name"]
                    if function.get("arguments"):
                        partial[slot]["arguments"] += function["arguments"]
                text = self._delta(delta)
                if text:
                    full += text
                    on_token(text)
        self.native_calls = [{"name": item["name"], "arguments": _decode(item["arguments"])}
                             for item in partial if item["name"]]
        if not full.strip() and not self.native_calls:
            raise ProviderError(f"{self.name} streamed empty content", retryable=True)
        return full

    async def _dispatch(self, history: List[Dict], system_prompt: str, on_token: Optional[Callable[[str], None]]) -> str:
        try:
            return await self._dispatch_once(history, system_prompt, on_token)
        except ProviderError as exc:
            if not self.native_tools or not _rejects_tools(exc):
                raise
            logger.debug("%s rejected the tools payload, continuing without it", self.name)
            self.native_tools = []
            return await self._dispatch_once(history, system_prompt, on_token)

    async def _dispatch_once(self, history: List[Dict], system_prompt: str,
                             on_token: Optional[Callable[[str], None]]) -> str:
        if not on_token:
            return await self._non_stream(self._payload(history, system_prompt, False))
        relay, state = self._guard_stream(on_token)
        try:
            return await self._stream(self._payload(history, system_prompt, True), relay)
        except ProviderError as exc:
            if state["emitted"]:
                exc.stream_started = True
            if exc.status_code is not None or exc.stream_started or state["emitted"]:
                raise
            logger.debug("%s stream failed, retrying without streaming: %s", self.name, exc)
        return await self._non_stream(self._payload(history, system_prompt, False))

    @async_retry(retries=5, delays=[2, 5, 10, 25, 60])
    async def generate_text(self, history: List[Dict], system_prompt: str, on_token: Callable = None) -> str:
        self._preflight()
        self._attempts = getattr(self, "_attempts", 0) + 1
        try:
            result = await self._dispatch(history, system_prompt, on_token)
        except Exception as exc:
            if self._attempts >= 6:
                self._attempts = 0
            if is_provider_fault(exc):
                self._fail()
            raise
        self._succeed()
        self._last_attempts = self._attempts
        self._attempts = 0
        return result

    async def generate_image(self, prompt: str, width: Optional[int] = None, height: Optional[int] = None) -> str:
        raise NotImplementedError(f"{self.name} does not support image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError(f"{self.name} does not support audio generation")

    async def analyze_image(self, prompt: str, image: str) -> str:
        raise NotImplementedError(f"{self.name} does not support image analysis")