import asyncio
import functools
import platform
from datetime import datetime
import pkgutil
import json
import time
import re
from pathlib import Path
from rich.console import Console

console = Console()

class SimpleLogger:
    def __init__(self):
        self.log_dir = Path("tmp")
        self.log_dir.mkdir(exist_ok=True)
        self.session_file = self.log_dir / f"session_{int(time.time())}.jsonl"
    
    def log(self, role: str, event: str, data: dict = None):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "role": role,
            "event": event,
            "data": data or {}
        }
        try:
            with open(self.session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except:
            pass

sys_logger = SimpleLogger()

class SimpleRateLimiter:
    async def acquire(self, key: str, tokens: int = 1) -> bool:
        return True
    def increment_usage(self, key: str, amount: int = 1) -> bool:
        return True

rate_limiter = SimpleRateLimiter()
quota_manager = rate_limiter

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
    return f"CONTEXT: [Time: {now}] [OS: {os_info}]"

def scan_available_agents() -> str:
    agents = []
    agents_path = Path("src/agents")
    if agents_path.exists():
        for _, name, _ in pkgutil.iter_modules([str(agents_path)]):
            agents.append(name)
    return ", ".join(agents)

def extract_json_from_text(text: str):
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match: return match.group(1)
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match: return match.group(1)
    return None