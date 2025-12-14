import asyncio
import functools
import platform
import datetime
import pkgutil
import re
from pathlib import Path
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
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os_info = f"{platform.system()} {platform.release()}"
    return f"CONTEXT: [Time: {now}] [OS: {os_info}]"

def scan_available_agents() -> str:
    agents = []
    agents_path = Path("src/agents")
    if agents_path.exists():
        for _, name, _ in pkgutil.iter_modules([str(agents_path)]):
            agents.append(name)
    return ", ".join(agents)