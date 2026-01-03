import os
import subprocess
import platform
import httpx
import edge_tts
import time
import sys
import inspect
import re
from typing import List
from bs4 import BeautifulSoup
from pathlib import Path
from ddgs import DDGS
from fake_useragent import UserAgent

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
        target_path = Path(path_str).resolve()
        project_root = Path.cwd().resolve()
        return target_path.is_relative_to(project_root)
    except Exception:
        return False

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
        filename = f"img_{int(time.time())}.jpg"
        path = TEMP_DIR / filename
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            with open(path, "wb") as f: f.write(resp.content)
        
        system = platform.system()
        if system == "Windows": os.startfile(path)
        elif system == "Darwin": subprocess.run(["open", str(path)])
        else: subprocess.run(["xdg-open", str(path)])
        
        return str(path)
    except Exception as e:
        return f"Image Download Error: {e}"

async def web_search(query: str, **kwargs) -> str:
    try:
        results = DDGS().text(query, max_results=5)
        if not results: return "No results found."
        return "\n".join([f"- {r['title']}: {r['href']}\n  Snippet: {r['body']}" for r in results])
    except Exception as e:
        return f"Search Error: {e}"

async def visit_page(url: str, **kwargs) -> str:
    try:
        ua = UserAgent()
        random_ua = ua.random
        headers = {
            "User-Agent": random_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        }

        async with httpx.AsyncClient(follow_redirects=True, timeout=20, http2=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 403:
                return f"Error 403: Access Denied by {url} (Anti-Bot detected)."
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, 'html.parser')
        
        for script in soup(["script", "style", "nav", "footer", "header", "form", "svg", "iframe", "noscript"]):
            script.decompose()
        text = soup.get_text(separator=' ', strip=True)
        clean_text = ' '.join(text.split())
        
        return clean_text[:14000] + ("..." if len(clean_text) > 14000 else "")
    except Exception as e:
        return f"Browsing Error: {e}"

async def get_weather(city: str, **kwargs) -> str:
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            data = resp.json()
            if not data.get("results"): return "City not found."
            lat, lon = data["results"][0]["latitude"], data["results"][0]["longitude"]
            w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
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
        if not text or not text.strip(): return "Error: Empty text."
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

async def read_file(path: str, **kwargs) -> str:
    try:
        if not _is_safe_path(path):
            return "Security Error: Access denied to path outside project directory."
        if not os.path.exists(path): return "File not found."
        with open(path, "r", encoding="utf-8") as f: return f.read()
    except Exception as e: return f"Read Error: {e}"

async def read_multiple_files(paths: List[str], **kwargs) -> str:
    result = []
    for path in paths:
        try:
            if not _is_safe_path(path):
                result.append(f"### File: {path}\n[Error: Access Denied]")
                continue
            if not os.path.exists(path):
                result.append(f"### File: {path}\n[Error: Not Found]")
                continue
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            result.append(f"### File: {path}\n{content}")
        except Exception as e:
            result.append(f"### File: {path}\n[Error: {e}]")
    return "\n\n".join(result)

async def write_file(path: str, content: str, **kwargs) -> str:
    try:
        if not _is_safe_path(path):
            return "Security Error: Cannot write outside project directory."
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f: f.write(content)
        return f"File written: {path}"
    except Exception as e: return f"Write Error: {e}"

async def execute_command(command: str, **kwargs) -> str:
    try:
        system = platform.system()
        shell_cmd = f'powershell -Command "{command}"' if system == "Windows" else command
        result = subprocess.run(shell_cmd, shell=True, capture_output=True, text=True)
        output = result.stdout
        if result.stderr: output += f"\nSTDERR:\n{result.stderr}"
        return output.strip() if output.strip() else "Executed."
    except Exception as e: return f"Execution Error: {e}"

async def list_dir(path: str = ".", **kwargs) -> str:
    try: return "\n".join(os.listdir(path))
    except Exception as e: return f"List Dir Error: {e}"

async def delegate_to_coder(task: str, **kwargs) -> str:
    from src.agents.coder import Coder
    provider = _get_active_provider()
    agent = Coder(provider)
    return await agent.run(task)

async def delegate_to_researcher(task: str, **kwargs) -> str:
    from src.agents.researcher import Researcher
    provider = _get_active_provider()
    agent = Researcher(provider)
    return await agent.run(task)

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
        schema.append(f"- {name}{params}")
    return "\n".join(schema)