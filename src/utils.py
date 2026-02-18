import asyncio
import functools
import json
import re
import secrets
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlparse
from rich.console import Console

console = Console()

# Global token counter
_global_tokens = 0


def get_global_tokens() -> int:
    return _global_tokens


def add_global_tokens(tokens: int) -> int:
    global _global_tokens
    _global_tokens += tokens
    return _global_tokens


def reset_global_tokens() -> None:
    global _global_tokens
    _global_tokens = 0


def count_tokens(history: list, response: str) -> tuple:
    input_text = "".join([str(m.get('content', '')) for m in history])
    return len(input_text) // 4, len(response) // 4


def format_token_display(total_tokens: int) -> str:
    if total_tokens > 16000:
        color = "red"
    elif total_tokens > 4000:
        color = "yellow"
    else:
        color = "green"
    return f"[dim]📊 Tokens: [{color}]{total_tokens}[/{color}][/dim]"


def detect_loop(history: List[Dict], window: int = 5, threshold: int = 3) -> Optional[str]:
    if len(history) < window * 2:
        return None
    recent = history[-window:]
    contents = [m.get("content", "")[:100] for m in recent if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if len(contents) < threshold:
        return None
    counts = Counter(contents)
    for content, count in counts.items():
        if count >= threshold:
            return content
    return None


def async_retry(retries=3, delays=[2, 5, 10]):
    """Decorator for async retry with backoff."""
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
    sanitized = text
    for pattern, replacement in replacements:
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
    return sanitized


def generate_random_delimiter(length: int = 24) -> str:
    return secrets.token_urlsafe(length)


def validate_url_scheme(url: str, allowed_schemes: Optional[set] = None) -> bool:
    if allowed_schemes is None:
        allowed_schemes = {'http', 'https'}
    try:
        parsed = urlparse(url)
        return parsed.scheme in allowed_schemes
    except Exception:
        return False


def extract_json_from_text(text: str) -> Optional[str]:
    """Extract JSON from text - handles code blocks and raw JSON."""
    if not text:
        return None
    
    # Try code blocks first
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        candidate = match.group(1)
        try:
            json.loads(candidate)
            return candidate
        except:
            pass
    
    # Find balanced JSON objects
    depth = 0
    start = None
    for i, c in enumerate(text):
        if c == '{':
            if depth == 0:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i+1]
                try:
                    json.loads(candidate)
                    return candidate
                except:
                    pass
                start = None
    
    return None


def get_system_context() -> str:
    import platform, os
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os_info = f"{platform.system()} {platform.release()}"
    shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "Unknown"))
    return f"CONTEXT: [Time: {now}] [OS: {os_info}] [Shell: {shell}]"
