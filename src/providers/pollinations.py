from typing import Callable, Dict, List, Optional
from urllib.parse import quote

from src.config import PollinationsSettings
from src.core.provider import (
    AIProvider,
    ProviderError,
    ProviderMeta,
    get_http_client,
    normalize_messages,
)
from src.utils import async_retry

META = ProviderMeta(name="pollinations", display_name="Pollinations (Default)", requires_key=False)

TEXT_BASE = "https://text.pollinations.ai"
IMAGE_BASE = "https://image.pollinations.ai/prompt"
AD_MARKER = "Support Pollinations.AI:"
HISTORY_WINDOW = 10


class Provider(AIProvider):
    META = META

    def __init__(self, settings: PollinationsSettings):
        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        super().__init__(TEXT_BASE, model=settings.text_model, headers=headers)
        self.name = "pollinations"
        self.settings = settings

    def _clean(self, text: str) -> str:
        if AD_MARKER in text:
            text = text.split(AD_MARKER)[0]
        return text.strip()

    def _check(self, response) -> None:
        if response.status_code == 402:
            raise ProviderError("Pollinations tier restriction", status_code=402, retryable=False)
        if response.status_code in (500, 502, 503, 504):
            raise ProviderError(f"Pollinations server error {response.status_code}", status_code=response.status_code, retryable=True)
        if response.status_code != 200:
            body = response.text[:300]
            lowered = body.lower()
            retryable = any(k in lowered for k in ("bad gateway", "cloudflare", "service unavailable", "rate limit"))
            raise ProviderError(f"Pollinations HTTP {response.status_code}: {body}", status_code=response.status_code, retryable=retryable)

    def _payload(self, messages: List[Dict]) -> Dict:
        payload: Dict = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
        }
        if self.settings.reasoning_effort != "minimal":
            payload["reasoning_effort"] = self.settings.reasoning_effort
        return payload

    async def _fallback(self, messages: List[Dict], system_prompt: str) -> str:
        conversation = f"System: {system_prompt}\n" + "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        url = f"{TEXT_BASE}/{quote(conversation[-4000:])}?model={quote(self.settings.text_model)}"
        response = await get_http_client().get(url)
        self._check(response)
        return response.text

    @async_retry(retries=5, delays=[1, 2, 5, 10, 20])
    async def generate_text(self, history: List[Dict], system_prompt: str, on_token: Callable = None) -> str:
        self._preflight()
        messages = normalize_messages(history, system_prompt)
        if len(messages) > HISTORY_WINDOW:
            messages = messages[:1] + messages[-HISTORY_WINDOW + 1:]
        try:
            try:
                response = await get_http_client().post(f"{TEXT_BASE}/openai", json=self._payload(messages), headers=self.headers)
                self._check(response)
                data = response.json()
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                self._record_usage(data.get("usage"))
            except ProviderError as exc:
                if not exc.retryable or exc.status_code == 402:
                    raise
                content = await self._fallback(messages, system_prompt)
            except Exception:
                content = await self._fallback(messages, system_prompt)
            cleaned = self._clean(content)
            if not cleaned:
                raise ProviderError("Pollinations returned empty content", retryable=True)
        except Exception:
            self._fail()
            raise
        self._succeed()
        return cleaned

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_image(self, prompt: str, width: Optional[int] = None, height: Optional[int] = None) -> str:
        w = width if width is not None else self.settings.image_width
        h = height if height is not None else self.settings.image_height
        params = f"width={w}&height={h}&nologo=true&enhance=true&model={quote(self.settings.image_model)}"
        return f"{IMAGE_BASE}/{quote(prompt)}?{params}"

    @async_retry(retries=3, delays=[2, 5, 10])
    async def generate_audio(self, text: str) -> bytes:
        url = f"{TEXT_BASE}/{quote(text)}?model={quote(self.settings.audio_model)}&voice={quote(self.settings.voice)}"
        response = await get_http_client().get(url)
        self._check(response)
        return response.content

    async def analyze_image(self, prompt: str, image_url: str) -> str:
        raise NotImplementedError("Pollinations does not support image analysis")