import asyncio
import functools
import os
import platform
from datetime import datetime
import re
from rich.console import Console

console = Console()

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
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os_info = f"{platform.system()} {platform.release()}"
    shell_info = os.getenv("SHELL", "Unknown")
    return f"CONTEXT: [Time: {now}] [OS: {os_info}] [Shell: {shell_info}]"

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