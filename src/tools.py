import os
import subprocess
import platform
import httpx
import edge_tts
import requests
import time
import re
from bs4 import BeautifulSoup
from pathlib import Path
from duckduckgo_search import DDGS

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

def extract_json_from_text(text: str):
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match: return match.group(1)
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match: return match.group(1)
    return None

async def download_and_open_image(url: str) -> str:
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

async def web_search(query: str) -> str:
    try:
        results = DDGS().text(query, max_results=5)
        if not results: return "No results found."
        return "\n".join([f"- {r['title']}: {r['href']}\n  Snippet: {r['body']}" for r in results])
    except Exception as e:
        return f"Search Error: {e}"

async def visit_page(url: str) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for script in soup(["script", "style", "nav", "footer", "header", "form", "svg"]):
            script.decompose()
        text = soup.get_text(separator=' ', strip=True)
        clean_text = ' '.join(text.split())
        return clean_text[:4000] + ("..." if len(clean_text) > 4000 else "")
    except Exception as e:
        return f"Browsing Error: {e}"

async def get_weather(city: str) -> str:
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

async def speak(text: str) -> str:
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

async def read_file(path: str) -> str:
    try:
        if not os.path.exists(path): return "File not found."
        with open(path, "r", encoding="utf-8") as f: return f.read()
    except Exception as e: return f"Read Error: {e}"

async def write_file(path: str, content: str) -> str:
    try:
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f: f.write(content)
        return f"File written: {path}"
    except Exception as e: return f"Write Error: {e}"

async def execute_command(command: str) -> str:
    try:
        system = platform.system()
        shell_cmd = f'powershell -Command "{command}"' if system == "Windows" else command
        result = subprocess.run(shell_cmd, shell=True, capture_output=True, text=True)
        output = result.stdout
        if result.stderr: output += f"\nSTDERR:\n{result.stderr}"
        return output.strip() if output.strip() else "Executed."
    except Exception as e: return f"Execution Error: {e}"

async def list_dir(path: str = ".") -> str:
    try: return "\n".join(os.listdir(path))
    except Exception as e: return f"List Dir Error: {e}"

async def delegate_to_coder(task: str) -> str:
    from src.config import load_config
    from src.providers.pollinations import PollinationsProvider
    from src.agents.coder import Coder
    config = load_config()
    provider = PollinationsProvider(config.pollinations)
    agent = Coder(provider)
    return await agent.run(task)

async def delegate_to_researcher(task: str) -> str:
    from src.config import load_config
    from src.providers.pollinations import PollinationsProvider
    from src.agents.researcher import Researcher
    config = load_config()
    provider = PollinationsProvider(config.pollinations)
    agent = Researcher(provider)
    return await agent.run(task)

def get_tools_schema() -> str:
    return ", ".join(TOOL_REGISTRY.keys())

TOOL_REGISTRY = {
    "web_search": web_search,
    "visit_page": visit_page,
    "get_weather": get_weather,
    "speak": speak,
    "read_file": read_file,
    "write_file": write_file,
    "execute_command": execute_command,
    "list_dir": list_dir,
    "delegate_to_coder": delegate_to_coder,
    "delegate_to_researcher": delegate_to_researcher
}