import asyncio
import functools
import json
import os
import platform
import re
import secrets
import threading
from datetime import datetime
from typing import Dict, Optional
from urllib.parse import urlparse
from rich.console import Console

console = Console()

_global_tokens = 0
_token_usage: Dict[str, Dict[str, int]] = {}
_token_lock = threading.Lock()


def get_global_tokens() -> int:
    return _global_tokens


def add_global_tokens(tokens: int) -> int:
    global _global_tokens
    with _token_lock:
        _global_tokens += tokens
        return _global_tokens


def get_provider_tokens(provider: str) -> Dict[str, int]:
    return _token_usage.get(provider, {"input": 0, "output": 0})


def add_provider_tokens(provider: str, input_tokens: int, output_tokens: int) -> Dict[str, int]:
    with _token_lock:
        if provider not in _token_usage:
            _token_usage[provider] = {"input": 0, "output": 0}
        _token_usage[provider]["input"] += input_tokens
        _token_usage[provider]["output"] += output_tokens
        return _token_usage[provider]


def get_all_provider_tokens() -> Dict[str, Dict[str, int]]:
    return dict(_token_usage)


def reset_global_tokens() -> None:
    global _global_tokens
    with _token_lock:
        _global_tokens = 0


def reset_provider_tokens(provider: Optional[str] = None) -> None:
    with _token_lock:
        if provider:
            _token_usage.pop(provider, None)
        else:
            _token_usage.clear()


def count_tokens(text_or_history, response: str = "") -> tuple:
    if isinstance(text_or_history, list):
        input_text = "".join(str(m.get('content', '')) for m in text_or_history)
    else:
        input_text = str(text_or_history)

    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        input_tokens = len(enc.encode(input_text))
    except ImportError:
        input_tokens = int(len(input_text.split()) * 1.3)

    output_tokens = int(len(response.split()) * 1.3) if response else 0
    return input_tokens, output_tokens


def format_token_display(total_tokens: int) -> str:
    color = "red" if total_tokens > 16000 else "yellow" if total_tokens > 4000 else "green"
    return f"[dim]📊 Tokens: [{color}]{total_tokens}[/{color}][/dim]"


def async_retry(retries=3, delays=(2, 5, 10)):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for i in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
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


def validate_url_scheme(url: str, allowed_schemes: Optional[set] = None) -> bool:
    if allowed_schemes is None:
        allowed_schemes = {'http', 'https'}
    try:
        return urlparse(url).scheme in allowed_schemes
    except Exception:
        return False


def get_system_context() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os_info = f"{platform.system()} {platform.release()}"
    shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "Unknown"))
    return f"CONTEXT: [Time: {now}] [OS: {os_info}] [Shell: {shell}]"
