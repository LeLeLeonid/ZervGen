import os
import subprocess
import platform
import httpx
import edge_tts
import requests
import time
import sys
import inspect
import re
import shlex
from typing import List
from bs4 import BeautifulSoup
from pathlib import Path
from urllib.parse import quote
from ddgs import DDGS
from fake_useragent import UserAgent
from src.core.memory import memory_core
from src.utils import rate_limiter, quota_manager, sys_logger

TEMP_DIR = Path("tmp")
TEMP_DIR.mkdir(exist_ok=True)

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Drizzle: Light", 53: "Drizzle: Moderate", 55: "Drizzle: Dense",
    61: "Rain: Slight", 63: "Rain: Moderate", 65: "Rain: Heavy",
    71: "Snow: Slight", 73: "Snow: Moderate", 75: "Snow: Heavy",
    95: "Thunderstorm: Slight or moderate", 99: "Thunderstorm with hail"
}

def _is_safe_path(path_str: str) -> bool:
    try:
        if not path_str or not isinstance(path_str, str):
            return False
        
        target_path = Path(path_str).resolve()
        project_root = Path.cwd().resolve()
        
        if ".." in str(target_path).split(os.sep):
            return False
        
        if not target_path.is_absolute():
            if target_path.is_relative_to(project_root):
                return True
        else:
            if target_path.is_relative_to(project_root):
                return True
        
        from src.config import load_config
        config = load_config()
        
        for allowed in config.allowed_directories:
            if not allowed or not isinstance(allowed, str):
                continue
                
            allowed_path = Path(allowed).resolve()
            if target_path.is_relative_to(allowed_path):
                if not any(suspicious in str(target_path).lower() 
                          for suspicious in ['tmp', 'temp', 'cache', '.git', 'node_modules']):
                    return True
                
        return False
    except (OSError, ValueError, TypeError):
        return False

def _sanitize_command(command: str) -> str:
    if not command or not isinstance(command, str):
        return ""
    
    command = re.sub(r'[;&|`$()]', '', command)
    
    try:
        parts = shlex.split(command)
        allowed_chars = r'^[a-zA-Z0-9_\-./\s]+$'
        for part in parts:
            if not re.match(allowed_chars, part):
                raise ValueError(f"Invalid characters in command part: {part}")
    except ValueError as e:
        raise ValueError(f"Command validation failed: {e}")
    
    return command

def extract_json_from_text(text: str):
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match: return match.group(1)
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match: return match.group(1)
    return None

def _get_active_provider():
    from src.config import load_config
    from src.providers.pollinations import PollinationsProvider
    from src.providers.gemini import GeminiProvider
    from src.providers.openrouter import OpenRouterProvider
    
    config = load_config()
    
    try:
        if config.provider == "gemini" and config.gemini.api_key:
            return GeminiProvider(config.gemini)
        elif config.provider == "openrouter" and config.openrouter.api_key:
            return OpenRouterProvider(config.openrouter)
        else:
            return PollinationsProvider(config.pollinations)
    except Exception:
        return PollinationsProvider(config.pollinations)

async def download_and_open_image(url: str, **kwargs) -> str:
    try:
        if not url or not isinstance(url, str):
            return "Error: Invalid URL parameter."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for image downloads."
        
        filename = f"img_{int(time.time())}.jpg"
        path = TEMP_DIR / filename
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            if resp.status_code != 200: return f"Download Failed: {resp.status_code}"
            with open(path, "wb") as f: f.write(resp.content)
        
        system = platform.system()
        if system == "Windows": os.startfile(path)
        elif system == "Darwin": subprocess.run(["open", str(path)])
        else: subprocess.run(["xdg-open", str(path)])
        
        return str(path)
    except Exception as e:
        return f"Image Download Error: {e}"

async def generate_image(prompt: str, **kwargs) -> str:
    try:
        if not prompt or not isinstance(prompt, str):
            return "Error: Invalid prompt parameter."
        
        if len(prompt) > 1000:
            return "Error: Prompt too long (max 1000 characters)."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for image generation."
        
        if not quota_manager.increment_usage("api_calls_per_day", 1):
            return "Error: Daily API quota exceeded."
        
        safe_prompt = quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&nologo=true&enhance=true"
        return await download_and_open_image(url)
    except Exception as e:
        return f"Generation Error: {e}"

async def web_search(query: str, **kwargs) -> str:
    try:
        if not query or not isinstance(query, str):
            return "Error: Invalid query parameter."
        
        if len(query) > 500:
            return "Error: Query too long (max 500 characters)."
        
        if not await rate_limiter.acquire("web_search", 1):
            return "Error: Rate limit exceeded for web searches."
        
        if not quota_manager.increment_usage("web_searches_per_day", 1):
            return "Error: Daily web search quota exceeded."
        
        results = DDGS().text(query, max_results=5)
        if not results: return "No results found."
        return "\n".join([f"- {r['title']}: {r['href']}\n  Snippet: {r['body']}" for r in results])
    except Exception as e:
        return f"Search Error: {e}"

async def visit_page(url: str, **kwargs) -> str:
    try:
        if not url or not isinstance(url, str):
            return "Error: Invalid URL parameter."
        
        if not url.startswith(('http://', 'https://')):
            return "Error: URL must start with http:// or https://"
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for page visits."
        
        ua = UserAgent()
        headers = {
            "User-Agent": ua.random,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        }

        async with httpx.AsyncClient(follow_redirects=True, timeout=20, http2=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 403:
                return f"Error 403: Access Denied by {url}."
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, 'html.parser')
        for script in soup(["script", "style", "nav", "footer", "header", "form", "svg"]):
            script.decompose()
        
        text = soup.get_text(separator=' ', strip=True)
        clean_text = ' '.join(text.split())
        return clean_text[:14000] + ("..." if len(clean_text) > 14000 else "")
    except Exception as e:
        return f"Browsing Error: {e}"

async def get_weather(city: str, **kwargs) -> str:
    try:
        if not city or not isinstance(city, str):
            return "Error: Invalid city parameter."
        
        if len(city) > 100:
            return "Error: City name too long."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for weather requests."
        
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            data = resp.json()
            if not data.get("results"): return "City not found."
            lat, lon = data["results"][0]["latitude"], data["results"][0]["longitude"]
            
            w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}¤t=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
            w_resp = await client.get(w_url)
            w_data = w_resp.json()
            curr = w_data["current"]
            code = curr['weather_code']
            condition = WMO_CODES.get(code, "Unknown")
            
            return (f"Weather in {data['results'][0]['name']}:\nCondition: {condition}\n"
                    f"Temp: {curr['temperature_2m']}°C (Feels: {curr['apparent_temperature']}°C)\n"
                    f"Humidity: {curr['relative_humidity_2m']}%\nWind: {curr['wind_speed_10m']} km/h")
    except Exception as e:
        return f"Weather Error: {e}"

async def speak(text: str, **kwargs) -> str:
    try:
        if not text or not isinstance(text, str):
            return "Error: Invalid text parameter."
        
        if not text.strip():
            return "Error: Empty text."
        
        if len(text) > 2000:
            return "Error: Text too long (max 2000 characters)."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for text-to-speech."
        
        filename = f"tts_{abs(hash(text))}.mp3"
        output_file = TEMP_DIR / filename
        communicate = edge_tts.Communicate(text, "en-US-AriaNeural")
        await communicate.save(str(output_file))
        
        system = platform.system()
        str_path = str(output_file.absolute())
        
        if system == "Windows":
            subprocess.Popen(f'powershell -c (New-Object Media.SoundPlayer "{str_path}").PlaySync()', shell=True)
        elif system == "Darwin":
            subprocess.Popen(["afplay", str_path])
        else:
            subprocess.Popen(["xdg-open", str_path])
        return f"Audio played. Saved to {output_file}"
    except Exception as e:
        return f"TTS Error: {e}"

async def read_files(path: str, **kwargs) -> str:
    paths = [p.strip() for p in path.split(',')] if ',' in path else [path]
    results = []
    
    for p in paths:
        try:
            if not _is_safe_path(p):
                results.append(f"### FILE: {p}\n[SECURITY ERROR: Access Denied]")
                continue
                
            if not os.path.exists(p):
                results.append(f"### FILE: {p}\n[ERROR: File Not Found]")
                continue
            
            with open(p, "r", encoding="utf-8") as f:
                content = f.read()
                
            results.append(f"### FILE: {p}\n{content}\n")
            
        except Exception as e:
            results.append(f"### FILE: {p}\n[READ ERROR: {e}]")

    return "\n".join(results)

async def write_file(path: str, content: str, **kwargs) -> str:
    try:
        if not path or not isinstance(path, str):
            return "Error: Invalid path parameter."
        
        if not content or not isinstance(content, str):
            return "Error: Invalid content parameter."
        
        if len(content) > 100000:
            return "Error: Content too large (max 100KB)."
        
        if not await rate_limiter.acquire("file_operations", 1):
            return "Error: Rate limit exceeded for file operations."
        
        if not quota_manager.increment_usage("file_operations_per_day", 1):
            return "Error: Daily file operation quota exceeded."
        
        if not _is_safe_path(path):
            return "Security Error: Cannot write outside project directory."
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f: f.write(content)
        return f"File written: {path}"
    except Exception as e:
        return f"Write Error: {e}"

async def execute_command(command: str, **kwargs) -> str:
    try:
        if not command or not isinstance(command, str):
            return "Error: Invalid command input."
        
        if len(command) > 500:
            return "Error: Command too long (max 500 characters)."
        
        if not await rate_limiter.acquire("command_execution", 1):
            return "Error: Rate limit exceeded for command execution."
        
        if not quota_manager.increment_usage("tool_executions_per_day", 1):
            return "Error: Daily tool execution quota exceeded."
        
        sanitized_command = _sanitize_command(command)
        if not sanitized_command:
            return "Error: Command contains invalid characters or sequences."
        
        if platform.system() == "Windows":
            if "powershell" in command.lower() and "-command" in command.lower():
                if ";" in command or "|" in command or "&" in command:
                    return "Error: Complex PowerShell commands are not allowed for security reasons."
        
        system = platform.system()
        shell_cmd = f'powershell -Command "{sanitized_command}"' if system == "Windows" else sanitized_command
        
        env = os.environ.copy()
        for key in list(env.keys()):
            if any(sensitive in key.lower() for sensitive in ['secret', 'key', 'token', 'password']):
                env.pop(key)
        
        result = subprocess.run(
            shell_cmd, 
            shell=True, 
            capture_output=True, 
            text=True, 
            timeout=30,
            env=env,
            cwd=os.getcwd()
        )
        
        output = result.stdout
        if result.stderr: 
            output += f"\nSTDERR:\n{result.stderr}"
        
        sys_logger.log("system", "command_execution", {
            "command": sanitized_command[:100] + "..." if len(sanitized_command) > 100 else sanitized_command,
            "exit_code": result.returncode,
            "output_length": len(output)
        })
        
        return output.strip() if output.strip() else "Executed."
    except subprocess.TimeoutExpired:
        return "Error: Command execution timed out (30s limit)."
    except Exception as e:
        return f"Execution Error: {e}"

async def list_dir(path: str = ".", **kwargs) -> str:
    try:
        if not path or not isinstance(path, str):
            return "Error: Invalid path parameter."
        
        if not await rate_limiter.acquire("file_operations", 1):
            return "Error: Rate limit exceeded for directory listing."
        
        if not _is_safe_path(path):
            return "Security Error: Cannot access outside project directory."
        return "\n".join(os.listdir(path))
    except Exception as e:
        return f"List Dir Error: {e}"

async def delegate_to_coder(task: str, **kwargs) -> str:
    try:
        if not task or not isinstance(task, str):
            return "Error: Invalid task parameter."
        
        if len(task) > 2000:
            return "Error: Task too long (max 2000 characters)."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for agent delegation."
        
        from src.agents.coder import Coder
        provider = _get_active_provider()
        agent = Coder(provider)
        return await agent.run(task)
    except Exception as e:
        return f"Delegate to Coder Error: {e}"

async def delegate_to_researcher(task: str, **kwargs) -> str:
    try:
        if not task or not isinstance(task, str):
            return "Error: Invalid task parameter."
        
        if len(task) > 2000:
            return "Error: Task too long (max 2000 characters)."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for agent delegation."
        
        from src.agents.researcher import Researcher
        provider = _get_active_provider()
        agent = Researcher(provider)
        return await agent.run(task)
    except Exception as e:
        return f"Delegate to Researcher Error: {e}"

async def remember(fact: str, category: str = "general", **kwargs) -> str:
    try:
        if not fact or not isinstance(fact, str):
            return "Error: Invalid fact parameter."
        
        if not category or not isinstance(category, str):
            return "Error: Invalid category parameter."
        
        if len(fact) > 5000:
            return "Error: Fact too long (max 5000 characters)."
        
        if len(category) > 50:
            return "Error: Category name too long."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for memory operations."
        
        return memory_core.add_memory(fact, category)
    except Exception as e:
        return f"Remember Error: {e}"

async def recall(query: str, **kwargs) -> str:
    try:
        if not query or not isinstance(query, str):
            return "Error: Invalid query parameter."
        
        if len(query) > 500:
            return "Error: Query too long (max 500 characters)."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for memory search."
        
        return memory_core.search_memory(query)
    except Exception as e:
        return f"Recall Error: {e}"

async def search_memory(query: str, mode: str = "semantic", **kwargs) -> str:
    """Search memory using GraphRAG with semantic/graph/keyword modes"""
    try:
        if not query or not isinstance(query, str):
            return "Error: Invalid query parameter."
        
        if len(query) > 500:
            return "Error: Query too long (max 500 characters)."
        
        if mode not in ["semantic", "graph", "keyword"]:
            return "Error: Mode must be 'semantic', 'graph', or 'keyword'."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for memory search."
        
        return memory_core.search_memory(query, mode=mode)
    except Exception as e:
        return f"Search Memory Error: {e}"

async def search_graph(node_id: str, depth: int = 2, **kwargs) -> str:
    """Graph traversal search from a specific node"""
    try:
        if not node_id or not isinstance(node_id, str):
            return "Error: Invalid node_id parameter."
        
        if depth < 1 or depth > 5:
            return "Error: Depth must be between 1 and 5."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for graph search."
        
        results = memory_core.evolver.search_graph(node_id, depth)
        if not results:
            return "No graph connections found."
        
        formatted = []
        for r in results:
            indent = "  " * r.get("depth", 0)
            formatted.append(f"{indent}- [{r['category']}] {r['content']}")
        
        return "GRAPH RESULTS:\n" + "\n".join(formatted)
    except Exception as e:
        return f"Graph Search Error: {e}"

async def evolve_memory(**kwargs) -> str:
    """Trigger self-evolution mechanism"""
    try:
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for evolution."
        
        return memory_core.evolve()
    except Exception as e:
        return f"Evolution Error: {e}"

async def memory_stats(**kwargs) -> str:
    """Get memory system statistics"""
    try:
        return memory_core.get_stats()
    except Exception as e:
        return f"Stats Error: {e}"

async def clear_memory(confirm: str = "no", **kwargs) -> str:
    """Clear all memory (requires confirmation)"""
    try:
        if confirm.lower() != "yes":
            return "Error: Use confirm='yes' to clear all memory. This cannot be undone."
        
        # Clear all data
        import shutil
        from pathlib import Path
        
        memory_dir = Path("tmp/memory")
        if memory_dir.exists():
            shutil.rmtree(memory_dir)
            memory_dir.mkdir(parents=True, exist_ok=True)
        
        # Reinitialize memory
        global memory_core
        from src.core.memory import MemoryManager
        memory_core = MemoryManager()
        
        return "Memory cleared successfully. System reinitialized."
    except Exception as e:
        return f"Clear Memory Error: {e}"

async def add_memory(content: str, category: str = "general", **kwargs) -> str:
    """Add memory with category"""
    try:
        if not content or not isinstance(content, str):
            return "Error: Invalid content parameter."
        
        if len(content) > 10000:
            return "Error: Content too long (max 10000 characters)."
        
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded for memory operations."
        
        return memory_core.add_memory(content, category)
    except Exception as e:
        return f"Add Memory Error: {e}"

def _generate_registry():
    current_module = sys.modules[__name__]
    registry = {}
    for name, func in inspect.getmembers(current_module, inspect.isfunction):
        if (not name.startswith("_") and 
            func.__module__ == __name__ and 
            name != "get_tools_schema"):
            registry[name] = func
    return registry

TOOL_REGISTRY = _generate_registry()

def get_tools_schema() -> str:
    schema = []
    for name, func in TOOL_REGISTRY.items():
        sig = inspect.signature(func)
        params = str(sig).replace(" -> str", "") 
        doc = inspect.getdoc(func) or "No description."
        schema.append(f"- {name}{params}: {doc}")
    return "\n".join(schema)