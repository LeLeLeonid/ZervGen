import base64
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from src.config import AnthropicSettings
from src.core.provider import (
    is_provider_fault,
    AIProvider,
    ProviderError,
    ProviderMeta,
    get_http_client,
    load_image,
    normalize_messages,
    raise_for_status,
    split_system,
)
from src.utils import async_retry

logger = logging.getLogger(__name__)

META = ProviderMeta(name="anthropic", display_name="Anthropic")

DEFAULT_BASE_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 8192


def _decode(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


class Provider(AIProvider):
    META = META
    NATIVE_TOOLS = True

    def __init__(self, settings: AnthropicSettings):
        if not settings.api_key:
            raise ValueError("Anthropic API Key is not set.")
        super().__init__(DEFAULT_BASE_URL, settings.api_key, settings.model)
        self.name = "anthropic"
        self.settings = settings
        self.headers = {
            "x-api-key": settings.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    @classmethod
    def from_endpoint(cls, name: str, base_url: str, api_key: str, model: str) -> "Provider":
        if not api_key:
            raise ValueError(f"{name} API Key is not set.")
        provider = cls.__new__(cls)
        AIProvider.__init__(provider, base_url or DEFAULT_BASE_URL, api_key, model)
        provider.name = name
        provider.settings = None
        provider.headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        return provider

    def _payload(self, history: List[Dict], system_prompt: str, stream: bool) -> Dict[str, Any]:
        system, messages = split_system(normalize_messages(history, system_prompt))
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": MAX_TOKENS,
            "temperature": 0.7,
            "stream": bool(stream),
        }
        if system:
            payload["system"] = system
        if self.native_tools:
            payload["tools"] = [
                {"name": spec["name"], "description": spec.get("description", ""),
                 "input_schema": spec.get("parameters") or {"type": "object", "properties": {}}}
                for spec in self.native_tools
            ]
            payload["tool_choice"] = {"type": "auto"}
        return payload

    def _track_usage(self, usage: Dict[str, Any]) -> None:
        if not usage:
            return
        merged = dict(self._last_usage)
        merged.update({k: v for k, v in usage.items() if isinstance(v, (int, float))})
        self._record_usage(merged)

    def _content(self, blocks: List[Dict[str, Any]]) -> str:
        return "".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict)).strip()

    def _calls(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Tool calls from the assistant content blocks."""
        return [
            {"name": block["name"], "arguments": block.get("input") or {}}
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name")
        ]

    async def _non_stream(self, payload: Dict[str, Any]) -> str:
        response = await get_http_client().post(self.base_url, headers=self.headers, json=payload)
        raise_for_status(response, self.name)
        data = response.json()
        if data.get("type") == "error":
            raise ProviderError(f"{self.name} error: {data.get('error')}", retryable=True)
        blocks = data.get("content") or []
        content = self._content(blocks)
        self.native_calls = self._calls(blocks)
        if not content and not self.native_calls:
            raise ProviderError(f"{self.name} returned empty content", retryable=False)
        self._track_usage(data.get("usage") or {})
        return content

    async def _stream(self, payload: Dict[str, Any], on_token: Callable[[str], None]) -> str:
        full = ""
        blocks: Dict[int, Dict[str, Any]] = {}
        async with get_http_client().stream("POST", self.base_url, headers=self.headers, json=payload) as response:
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
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "error":
                    raise ProviderError(f"{self.name} stream error: {event.get('error')}", retryable=True, stream_started=True)
                if event_type == "message_start":
                    self._track_usage((event.get("message") or {}).get("usage") or {})
                elif event_type == "message_delta":
                    self._track_usage(event.get("usage") or {})
                elif event_type == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        blocks[int(event.get("index") or 0)] = {"name": block.get("name", ""), "json": ""}
                elif event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "input_json_delta":
                        slot = blocks.setdefault(int(event.get("index") or 0), {"name": "", "json": ""})
                        slot["json"] += delta.get("partial_json") or ""
                        continue
                    text = delta.get("text") or ""
                    if text:
                        full += text
                        on_token(text)
        self.native_calls = [{"name": item["name"], "arguments": _decode(item["json"])}
                             for item in blocks.values() if item["name"]]
        if not full.strip() and not self.native_calls:
            raise ProviderError(f"{self.name} streamed empty content", retryable=True)
        return full

    @async_retry(retries=5, delays=[2, 5, 10, 25, 60])
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
        except Exception as exc:
            if is_provider_fault(exc):
                self._fail()
            raise
        self._succeed()
        return result

    async def generate_image(self, prompt: str, width: Optional[int] = None, height: Optional[int] = None) -> str:
        raise NotImplementedError("Anthropic does not support image generation")

    async def generate_audio(self, text: str) -> bytes:
        raise NotImplementedError("Anthropic does not support audio generation")

    @async_retry(retries=3, delays=[2, 5, 10])
    async def analyze_image(self, prompt: str, image_path_or_url: str) -> str:
        raw, media_type = await load_image(image_path_or_url)
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(raw).decode("ascii")}},
                {"type": "text", "text": prompt},
            ]}],
        }
        response = await get_http_client().post(self.base_url, headers=self.headers, json=payload)
        raise_for_status(response, self.name)
        data = response.json()
        if data.get("type") == "error":
            raise ProviderError(f"{self.name} error: {data.get('error')}", retryable=False)
        return self._content(data.get("content") or []) or "No analysis returned."