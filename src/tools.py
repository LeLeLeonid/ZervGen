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
from typing import List
from bs4 import BeautifulSoup
from pathlib import Path
from urllib.parse import quote
from ddgs import DDGS
from fake_useragent import UserAgent
from src.core.memory import memory_core
from src.utils import extract_json_from_text

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
        if not path_str or not isinstance(path_str, str): return False
        
        from src.config import load_config
        config = load_config()
        
        target_path = Path(path_str).resolve()
        project_root = Path.cwd().resolve()
        
        if target_path.is_relative_to(project_root):
            if any(s in str(target_path).lower() for s in ['config.json', 'tmp/memory', 'cache', '.git', 'node_modules']):
                return False
            return True
        
        for allowed in config.allowed_directories:
            if not allowed: continue
            if target_path.is_relative_to(Path(allowed).resolve()):
                return True
        return False
    except: return False

def _get_active_provider():
    from src.config import load_config
    from src.providers.pollinations import PollinationsProvider
    from src.providers.gemini import GeminiProvider
    from src.providers.openrouter import OpenRouterProvider
    config = load_config()
    try:
        if config.provider == "gemini" and config.gemini.api_key: return GeminiProvider(config.gemini)
        elif config.provider == "openrouter" and config.openrouter.api_key: return OpenRouterProvider(config.openrouter)
        else: return PollinationsProvider(config.pollinations)
    except: return PollinationsProvider(config.pollinations)

async def download_and_open_image(url: str, **kwargs) -> str:
    try:
        filename = f"img_{int(time.time())}.jpg"
        path = TEMP_DIR / filename
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            if resp.status_code != 200: return f"Download Failed: {resp.status_code}"
            with open(path, "wb") as f: f.write(resp.content)
        
        if platform.system() == "Windows": os.startfile(path)
        elif platform.system() == "Darwin": subprocess.run(["open", str(path)])
        else: subprocess.run(["xdg-open", str(path)])
        return str(path)
    except Exception as e: return f"Image Download Error: {e}"

async def generate_image(prompt: str, **kwargs) -> str:
    try:
        safe_prompt = quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&nologo=true&enhance=true"
        return await download_and_open_image(url)
    except Exception as e: return f"Generation Error: {e}"

async def web_search(query: str, **kwargs) -> str:
    try:
        results = DDGS().text(query, max_results=5)
        if not results: return "No results found."
        return "\n".join([f"- {r['title']}: {r['href']}\n  Snippet: {r['body']}" for r in results])
    except Exception as e: return f"Search Error: {e}"

async def visit_page(url: str, **kwargs) -> str:
    try:
        ua = UserAgent()
        headers = {"User-Agent": ua.random, "Accept": "text/html"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 403: return f"Error 403: Access Denied."
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, 'html.parser')
        for s in soup(["script", "style", "nav", "footer", "header", "form", "svg"]): s.decompose()
        text = ' '.join(soup.get_text(separator=' ', strip=True).split())
        return text[:14000] + ("..." if len(text) > 14000 else "")
    except Exception as e: return f"Browsing Error: {e}"

async def get_weather(city: str, **kwargs) -> str:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json")
            data = resp.json()
            if not data.get("results"): return "City not found."
            lat, lon = data["results"][0]["latitude"], data["results"][0]["longitude"]
            
            w_resp = await client.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m")
            curr = w_resp.json()["current"]
            cond = WMO_CODES.get(curr['weather_code'], "Unknown")
            return f"Weather in {data['results'][0]['name']}:\nCondition: {cond}\nTemp: {curr['temperature_2m']}°C\nHumidity: {curr['relative_humidity_2m']}%\nWind: {curr['wind_speed_10m']} km/h"
    except Exception as e: return f"Weather Error: {e}"

async def speak(text: str, **kwargs) -> str:
    try:
        if not text.strip(): return "Error: Empty text."
        filename = f"tts_{abs(hash(text))}.mp3"
        output_file = TEMP_DIR / filename
        communicate = edge_tts.Communicate(text, "en-US-AriaNeural")
        await communicate.save(str(output_file))
        
        if platform.system() == "Windows": os.startfile(output_file)
        elif platform.system() == "Darwin": subprocess.run(["open", str(output_file)])
        else: subprocess.run(["xdg-open", str(output_file)])
        return f"[Audio Played]: \"{text}\""
    except Exception as e: return f"TTS Error: {e}"

async def read_files(paths: str, **kwargs) -> str:
    """Reads one or multiple files. Usage: 'file1.py' or 'file1.py, file2.py'."""
    try:
        # Handle list or comma-separated string
        file_list = paths if isinstance(paths, list) else [p.strip() for p in paths.split(',')]
        results = []

        for p in file_list:
            try:
                if not _is_safe_path(p):
                    results.append(f"### {p}\n[SECURITY ERROR: Access Denied]")
                    continue

                if not os.path.exists(p):
                    results.append(f"### {p}\n[ERROR: File Not Found]")
                    continue

                with open(p, "r", encoding="utf-8") as f:
                    content = f.read()
                
                # Safety cap for huge files
                if len(content) > 50000:
                    content = content[:50000] + "\n... [TRUNCATED 50KB+]"

                results.append(f"### {p}\n{content}")
            except Exception as e:
                results.append(f"### {p}\n[READ ERROR: {e}]")

        return "\n\n".join(results)
    except Exception as e:
        return f"Read Files Error: {e}"

async def grep_files(pattern: str, path: str = ".", **kwargs) -> str:
    try:
        if not _is_safe_path(path): return "Access Denied."
        
        results = []
        root_path = Path(path)
        ignore_dirs = {'.git', '__pycache__', 'venv', 'node_modules', 'tmp'}
        
        for root, dirs, files in os.walk(root_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                if not file.endswith(('.py', '.md', '.json', '.txt')): continue
                
                file_path = Path(root) / file
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        for i, line in enumerate(f, 1):
                            if pattern in line:
                                results.append(f"{file_path}:{i}: {line.strip()}")
                except: pass
                
                if len(results) > 100:
                    results.append("... [Too many matches]")
                    return "\n".join(results)
                    
        return "\n".join(results) if results else "No matches found."
    except Exception as e:
        return f"Grep Error: {e}"

async def write_file(path: str, content: str, **kwargs) -> str:
    try:
        if not _is_safe_path(path): return "Security Error: Path outside project."
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f: f.write(content)
        return f"File written: {path}"
    except Exception as e: return f"Write Error: {e}"

async def append_file(path: str, content: str, **kwargs) -> str:
    try:
        if not _is_safe_path(path): return "Security Error: Path outside project."
        with open(path, "a", encoding="utf-8") as f: f.write(content)
        return f"Appended to: {path}"
    except Exception as e: return f"Append Error: {e}"

def execute_command(command: str, **kwargs) -> str:
    try:
        if not command or not isinstance(command, str):
            return "Error: Invalid command."

        if platform.system() == "Windows":
            safe_cmd = command.replace('"', '\\"')
            shell_cmd = f'powershell -Command "{safe_cmd}"'
        else:
            shell_cmd = command

        result = subprocess.run(
            shell_cmd, 
            shell=True, 
            capture_output=True, 
            text=True,
            timeout=120
        )
        
        output = result.stdout
        if result.stderr: 
            output += f"\nSTDERR:\n{result.stderr}"
        return output.strip() or "Executed successfully (no output)."
    except subprocess.TimeoutExpired:
        return "Error: Command timed out."
    except Exception as e:
        return f"Execution Error: {e}"

async def list_dir(path: str = ".", **kwargs) -> str:
    try: return "\n".join(os.listdir(path))
    except Exception as e: return f"List Dir Error: {e}"

async def list_files_recursive(path: str = ".", **kwargs) -> str:
    try:
        if not _is_safe_path(path):
            return "Security Error: Access denied."

        root_path = Path(path)
        if not root_path.exists():
            return "Error: Path not found."

        results = []
        file_count = 0
        max_files = 500
        ignore_dirs = {'.git', '__pycache__', 'venv', 'env', 'node_modules', '.idea', '.vscode', 'tmp'}

        for root, dirs, files in os.walk(root_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                if file_count >= max_files:
                    results.append("... [TRUNCATED]")
                    return "\n".join(results)
                full_path = Path(root) / file
                try:
                    rel_path = full_path.relative_to(root_path)
                    results.append(str(rel_path).replace("\\", "/"))
                except:
                    results.append(str(full_path).replace("\\", "/"))
                file_count += 1
        if not results: return "Directory is empty."
        return "\n".join(results)

    except Exception as e:
        return f"Recursive List Error: {e}"

async def delegate_to(agent_name: str, task: str, **kwargs) -> str:
    """Delegates to a specialized agent."""
    try:
        from src.core.base_agent import BaseAgent
        from src.config import load_config
        config = load_config()
        provider = _get_active_provider()
        agent = BaseAgent(name=agent_name.capitalize(), provider=provider, skill_name=agent_name, settings=config)
        agent.tools = TOOL_REGISTRY
        return await agent.run(task)
    except Exception as e:
        return f"Delegation Error: {e}"

async def remember(fact: str, category: str = "general", **kwargs) -> str:
    try:
        if not fact or not isinstance(fact, str):
            return "Error: Invalid fact."
        return memory_core.add_memory(fact, category)
    except Exception as e:
        return f"Remember Error: {e}"

async def recall(query: str, **kwargs) -> str:
    try:
        if not query or not isinstance(query, str):
            return "Error: Invalid query."
        return memory_core.search_memory(query)
    except Exception as e:
        return f"Recall Error: {e}"

async def memory_stats(**kwargs) -> str:
    try:
        return memory_core.get_stats()
    except Exception as e:
        return f"Stats Error: {e}"

async def clear_memory(confirm: str = "no", **kwargs) -> str:
    try:
        if confirm.lower() != "yes":
            return "Error: Use confirm='yes' to clear memory."
        
        import shutil
        from pathlib import Path
        
        memory_dir = Path("tmp/memory")
        if memory_dir.exists():
            shutil.rmtree(memory_dir)
            memory_dir.mkdir(parents=True, exist_ok=True)
            
        global memory_core
        from src.core.memory import MemoryManager
        memory_core = MemoryManager()
        
        return "Memory cleared."
    except Exception as e:
        return f"Clear Memory Error: {e}"

async def append_file(path: str, content: str, **kwargs) -> str:
    try:
        if not _is_safe_path(path):
            return "Security Error: Cannot write outside project directory."

        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended to file: {path}"
    except Exception as e:
        return f"Append Error: {e}"

async def debug_system_prompt(**kwargs) -> str:
    from src.utils import get_system_context
    from src.skills_loader import load_skill, get_skills_schema
    from src.core.memory import memory_core
    from src.config import load_config
    from src.core.mcp_manager import MCPManager
    
    config = load_config()
    
    try:
        base_prompt = load_skill("system")
        if not base_prompt:
            with open("src/skills/system.md", "r", encoding="utf-8") as f:
                base_prompt = f.read()
    except:
        base_prompt = "You are ZervGen Supervisor. Route tasks via JSON."

    context = get_system_context()
    skills = get_skills_schema()
    local_tools = get_tools_schema()
    memories = memory_core.get_recent_memories(limit=5)

    mcp_tools = "MCP DISABLED"
    if config.mcp_enabled:
        # Create temp manager just to get schema string
        temp_mcp = MCPManager(config)
        mcp_tools = mcp.get_tools_schema()

    return (
        f"=== DEBUG SNAPSHOT ===\n"
        f"{context}\n\n"
        f"--- SYSTEM PROMPT ---\n{base_prompt}\n\n"
        f"--- MEMORY ---\n{memories}\n\n"
        f"--- SKILLS ---\n{skills}\n\n"
        f"--- TOOLS ---\n{local_tools}\n{mcp_tools}"
    )

def _generate_registry():
    current_module = sys.modules[__name__]
    registry = {}
    for name, func in inspect.getmembers(current_module, inspect.isfunction):
        if not name.startswith("_") and func.__module__ == __name__ and name != "get_tools_schema":
            registry[name] = func
    return registry

TOOL_REGISTRY = _generate_registry()

def get_tools_schema() -> str:
    schema = []
    for name, func in TOOL_REGISTRY.items():
        sig = inspect.signature(func)
        params = str(sig).replace(" -> str", "")
        doc = inspect.getdoc(func) or "Tool."
        schema.append(f"- {name}{params}: {doc}")
    return "\n".join(schema)