import asyncio
import functools
import os
import platform
import re
import secrets
import threading
from datetime import datetime
from typing import Dict, Optional, List
from rich.console import Console

console = Console()


class TokenTracker:
    _instance = None
    _usage: Dict[str, Dict[str, int]] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._total = 0
            cls._instance._usage = {}
            cls._instance._lock = threading.Lock()
        return cls._instance

    def get_total(self) -> int:
        return self._total

    def add_tokens(self, tokens: int) -> int:
        with self._lock:
            self._total += tokens
            return self._total

    def get_provider(self, provider: str) -> Dict[str, int]:
        return self._usage.get(provider, {"input": 0, "output": 0})

    def add_provider(self, provider: str, input_tokens: int, output_tokens: int) -> Dict[str, int]:
        with self._lock:
            if provider not in self._usage:
                self._usage[provider] = {"input": 0, "output": 0}
            self._usage[provider]["input"] += input_tokens
            self._usage[provider]["output"] += output_tokens
            return self._usage[provider]

    def get_all(self) -> Dict[str, Dict[str, int]]:
        return dict(self._usage)

    def reset_total(self) -> None:
        with self._lock:
            self._total = 0

    def reset_provider(self, provider: Optional[str] = None) -> None:
        with self._lock:
            if provider:
                self._usage.pop(provider, None)
            else:
                self._usage.clear()


_tracker = TokenTracker()

get_global_tokens = _tracker.get_total
add_global_tokens = _tracker.add_tokens
get_provider_tokens = _tracker.get_provider
add_provider_tokens = _tracker.add_provider
get_all_provider_tokens = _tracker.get_all
reset_global_tokens = _tracker.reset_total
reset_provider_tokens = _tracker.reset_provider


def count_tokens(text_or_history, response: str = "") -> tuple:
    if isinstance(text_or_history, list):
        input_tokens = estimate_messages_tokens(text_or_history)
    else:
        input_text = str(text_or_history)
        input_tokens = count_tokens_tiktoken(input_text)

    output_tokens = count_tokens_tiktoken(response) if response else 0
    return input_tokens, output_tokens


def async_retry(retries=3, delays=(2, 5, 10)):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for i in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "rate limit" in err_str.lower() or "CircuitBreaker" in type(e).__name__:
                        raise e
                    if i == retries:
                        raise e
                    wait_time = delays[i] if i < len(delays) else delays[-1]
                    console.print(f"[yellow]Retry {i+1}/{retries} in {wait_time}s...[/yellow]")
                    await asyncio.sleep(wait_time)
        return wrapper
    return decorator


def sanitize_for_prompt(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    replacements = [
        (r'===', '\\==='), (r'---', '\\---'), (r'```', '\\`\\`\\`'),
        (r'<\|', '\\<\\|'), (r'\|>', '\\|\\>'),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def generate_random_delimiter(length: int = 24) -> str:
    return secrets.token_urlsafe(length)



def get_system_context() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os_info = f"{platform.system()} {platform.release()}"
    shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "Unknown"))
    return f"CONTEXT: [Time: {now}] [OS: {os_info}] [Shell: {shell}]"


def count_tokens_tiktoken(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        return int(len(text.split()) * 1.3)


def estimate_messages_tokens(messages: List[Dict]) -> int:
    total = 4
    for m in messages:
        total += 4
        for v in m.values():
            total += count_tokens_tiktoken(str(v))
    return total


class ContextCompressor:
    def __init__(self, provider):
        self.provider = provider
        self._prev = None

    async def compress(self, msgs, keep_last=8):
        head = [m for m in msgs if m.get("role") == "system"]
        tail = msgs[-keep_last:]
        middle = msgs[len(head):-keep_last]
        if len(middle) < 4:
            return msgs
        transcript = "\n".join(f"[{m.get('role','?')}]: {m.get('content','')[:500]}" for m in middle)
        prompt = (f"Update previous summary with new messages.\n\nPrevious:\n{self._prev}\n\nNew:\n{transcript}" if self._prev
                  else f"Summarize this conversation segment in 2-4 sentences. Preserve facts, decisions, and tool results. Do not add new information.\n\n{transcript}")
        sys_prompt = "You are a precise summarizer. Output ONLY the summary text. No preamble, no commentary. Preserve key facts and decisions exactly. Never invent information."
        try:
            r = await self.provider.generate_text([{"role":"user","content":prompt}], sys_prompt)
            s = (r.get("content","") if isinstance(r,dict) else str(r)).strip()
            self._prev = s[:4000]
        except Exception:
            s = f"[{len(middle)} messages trimmed]"
        return head + [{"role":"assistant","content":s}] + tail


