import asyncio
import functools
from datetime import datetime
import re
import secrets
from typing import Optional
from urllib.parse import urlparse
from rich.console import Console

console = Console()

def sanitize_for_prompt(text: str) -> str:
    """
    Sanitize user input to prevent prompt injection attacks.
    Escapes delimiter patterns that could be used to manipulate prompts.
    """
    if not text or not isinstance(text, str):
        return text

    replacements = [
        (r'===', '\\==='),
        (r'---', '\\---'),
        (r'```', '\\`\\`\\`'),
        (r'<\|', '\\<\\|'),
        (r'\|>', '\\|\\>'),
        (r'\[SYSTEM\]', '\\[SYSTEM\\]'),
        (r'\[USER\]', '\\[USER\\]'),
        (r'\[ASSISTANT\]', '\\[ASSISTANT\\]'),
    ]

    sanitized = text
    for pattern, replacement in replacements:
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)

    return sanitized


def generate_random_delimiter(length: int = 24) -> str:
    """
    Generate a cryptographically secure random delimiter string.
    Used for boundary markers in prompts to prevent injection.
    """
    return secrets.token_urlsafe(length)


def validate_url_scheme(url: str, allowed_schemes: Optional[set] = None) -> bool:
    """
    Validate that a URL uses an allowed scheme.
    Prevents file:// and other potentially dangerous protocols.
    """
    if allowed_schemes is None:
        allowed_schemes = {'http', 'https'}

    try:
        parsed = urlparse(url)
        return parsed.scheme in allowed_schemes
    except Exception:
        return False




def extract_json_from_text(text: str):
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match: return match.group(1)
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match: return match.group(1)
    return None

def async_retry(retries=3, delays=[2, 5, 10]):
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
                    console.print(f"[bold yellow]Wait {wait_time}s... (Error: {e})[/bold yellow]")
                    await asyncio.sleep(wait_time)
        return wrapper
    return decorator

def get_system_context() -> str:
    import platform, os
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os_info = f"{platform.system()} {platform.release()}"
    shell = "Unknown"
    if "SHELL" in os.environ: shell = os.environ["SHELL"]
    elif "COMSPEC" in os.environ: shell = os.environ["COMSPEC"]
    return f"CONTEXT: [Time: {now}] [OS: {os_info}] [Shell: {shell}]"

def extract_json_from_text(text: str):
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match: return match.group(1)
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match: return match.group(1)
    return None

def print_token_usage(history: list, response: str):
    """Prints estimated token usage and cost indicator."""
    input_text = "".join([str(m.get('content', '')) for m in history])
    input_tokens = len(input_text) // 4
    output_tokens = len(response) // 4
    total = input_tokens + output_tokens
    color = "green"
    if total > 4000: color = "yellow"
    if total > 16000: color = "red"
    
    console.print(f"[dim]🎫 Tokens: [{color}]{total}[/{color}] (In: {input_tokens} | Out: {output_tokens})[/dim]", justify="right")