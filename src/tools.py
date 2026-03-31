import os
import sys
import ast
import json
import asyncio
import subprocess
import platform
import time
import inspect
import re
import uuid
import base64
import hashlib
import secrets
from typing import List
from pathlib import Path
from urllib.parse import quote, urlparse
from types import MappingProxyType
import aiofiles
import httpx
from ddgs import DDGS
from fake_useragent import UserAgent
from bs4 import BeautifulSoup
from rich.console import Console
from src.config import load_config

TEMP_DIR = Path("tmp")
TEMP_DIR.mkdir(exist_ok=True)
console = Console()

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Drizzle: Light", 53: "Drizzle: Moderate", 55: "Drizzle: Dense",
    61: "Rain: Slight", 63: "Rain: Moderate", 65: "Rain: Heavy",
    71: "Snow: Slight", 73: "Snow: Moderate", 75: "Snow: Heavy",
    95: "Thunderstorm: Slight or moderate", 99: "Thunderstorm with hail"
}

_ANTI_PATTERNS = [
    (r"rm\s+(-rf?|--recursive)\s+[/~]", "Recursive delete from root/home"),
    (r"rd\s+/s/q\s+[A-Z]:[\\\/]", "Windows recursive delete from root"),
    (r"(sudo|doas)\s+", "Privilege escalation"),
    (r"(chmod|chown)\s+777", "World-writable perms"),
    (r">\s*/dev/sd[a-z]", "Raw disk write"),
    (r"del\s+/[fqs]\s+[A-Z]:[\\\/]", "Windows force delete from root"),
    (r"mkfs\.\w+", "Format filesystem"),
    (r"dd\s+if=.*of=/dev/", "Raw disk write"),
    (r":\(\)\s*\{.*\|.*:.*\};:", "Fork bomb"),
    (r"(shutdown|reboot|halt|poweroff)\s", "System shutdown"),
    (r"Stop-Computer|Restart-Computer|shutdown\s+/[srf]", "Windows shutdown"),
    (r"curl\b.*\|\s*(bash|sh|powershell|pwsh)", "Remote code exec via curl"),
    (r"wget\b.*\|\s*(bash|sh|powershell|pwsh)", "Remote code exec via wget"),
    (r"iex\s*\(|Invoke-Expression", "PowerShell remote exec"),
    (r">\s*/etc/(passwd|shadow|hosts)", "Write to system config"),
    (r"chmod\s+[0-7]*\s+/(etc|usr|bin|sbin)", "System dir perm change"),
    (r"Set-ExecutionPolicy\s+Bypass", "PowerShell execution bypass"),
    (r"Remove-Item\s+-[a-z]*Recurse\s+[A-Z]:\\", "PowerShell recursive root delete"),
    (r"format\s+[A-Z]:\s*/[yq]", "Windows disk format"),
    (r"reg\s+delete\s+HKLM", "Registry delete"),
    (r"Remove-ItemProperty\s+.*HKLM", "Registry delete PowerShell"),
]

_ALLOWED_ROOTS: List[Path] = [Path.cwd().resolve()]

def _load_allowed_roots():
    wl = Path.home() / ".zervgen" / "allowed_roots.json"
    if wl.exists():
        try:
            for p in json.loads(wl.read_text()):
                pp = Path(p).resolve()
                if pp.exists() and pp not in _ALLOWED_ROOTS:
                    _ALLOWED_ROOTS.append(pp)
        except Exception:
            pass

def reload_allowed_roots():
    global _ALLOWED_ROOTS
    _ALLOWED_ROOTS = [Path.cwd().resolve()]
    _load_allowed_roots()

_load_allowed_roots()

def _is_safe_path(path: str) -> bool:
    try:
        resolved = Path(path).resolve()
        return any(resolved.is_relative_to(root) for root in _ALLOWED_ROOTS)
    except (OSError, ValueError):
        return False


# ── DELEGATION ──────────────────────────────────────────────────────────────

async def delegate_to(agent_name: str = "", task: str = "", context: str = "", mode: str = "ASK", memory: bool = True, agents: str = "") -> str:
    from src.core.base_agent import BaseAgent
    from src.skills_loader import role_exists
    from src.core.memory import memory_core
    from src.core.provider import get_provider

    if agents:
        try:
            agent_list = json.loads(agents) if isinstance(agents, str) else agents
            results = []
            for spec in agent_list:
                name = spec.get("agent_name", "code")
                t = spec.get("task", "")
                ctx = spec.get("context", "")
                r = await delegate_to(agent_name=name, task=t, context=ctx, mode=mode, memory=memory)
                results.append(f"--- {name} ---\n{r}")
            return "\n\n".join(results)
        except Exception as e:
            return f"Error parsing agents: {e}"

    if not agent_name:
        return "Error: agent_name required"
    if not role_exists(agent_name):
        return f"Error: Agent/Skill '{agent_name}' does not exist"
    config = load_config()
    full_task = f"{context}\n\n{task}" if context else task
    provider = get_provider(config.provider, config)
    agent = await BaseAgent.create(role=agent_name, mode=mode, settings=config, memory=(memory_core if memory else None))
    agent.provider = provider
    agent._is_delegated = True
    agent.tools.pop("delegate_to", None)
    return await agent.run(full_task)


# ─── WEB ─────────────────────────────────────────────────────────────────────

async def web_search(query: str, limit: int = 5) -> str:
    if not query or not isinstance(query, str):
        return "Error: Invalid search query."
    try:
        limit = max(1, min(int(limit) if isinstance(limit, (int, float, str)) else 5, 10))
    except (ValueError, TypeError):
        limit = 5
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query.strip(), max_results=limit))
        if not results:
            return f"Error: No results for '{query}'."
        structured = [{"title": r.get("title", "No Title"), "url": r.get("href", ""), "body": r.get("body", "")[:500]} for r in results]
        return json.dumps({"query": query, "results": structured}, ensure_ascii=False)
    except ImportError:
        return "Error: ddgs not installed."
    except Exception as e:
        return f"Error: Search error: {e}"


async def fetch_url(url: str, parse_html: bool = False) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return f"Error: Invalid URL scheme: {parsed.scheme}"
        from src.core.provider import get_http_client
        if not hasattr(fetch_url, '_cached_user_agent'):
            fetch_url._cached_user_agent = UserAgent().random
        client = get_http_client()
        response = await client.get(url, headers={"User-Agent": fetch_url._cached_user_agent}, follow_redirects=True)
        if response.status_code != 200:
            return f"Error: HTTP {response.status_code}"
        if parse_html:
            soup = BeautifulSoup(response.text, 'html.parser')
            title = soup.find('title').get_text(strip=True) if soup.find('title') else ""
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()
            lines = [line for line in soup.get_text(separator='\n', strip=True).split('\n') if line.strip()][:100]
            return json.dumps({"title": title, "text": '\n'.join(lines) if lines else "No readable content found.", "url": url}, ensure_ascii=False)
        return json.dumps({"text": response.text[:10000], "url": url}, ensure_ascii=False)
    except httpx.TimeoutException:
        return f"Error: Request timed out for {url}"
    except Exception as e:
        return f"Error: {e}"


async def get_weather(location: str, forecast: bool = False) -> str:
    if not location:
        return "Error: Location is required."
    from src.core.provider import get_http_client
    client = get_http_client()
    safe_loc = quote(location.strip())
    try:
        if forecast:
            resp = await client.get(f"https://wttr.in/{safe_loc}?T", timeout=10.0)
            if resp.status_code == 200 and resp.text and "Unknown location" not in resp.text:
                return resp.text.strip()
        else:
            resp = await client.get(f"https://wttr.in/{safe_loc}?format=%l:+%c+%t+%h+%w", timeout=10.0)
            if resp.status_code == 200 and resp.text:
                return f"{resp.text.strip()}"
    except Exception:
        pass
    geocode_resp = await client.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": location, "count": 1, "language": "en", "format": "json"})
    if geocode_resp.status_code != 200:
        return "Error: Geocoding failed"
    geocode_data = geocode_resp.json()
    if not geocode_data.get('results'):
        return f"Error: Location not found: {location}"
    result = geocode_data['results'][0]
    lat, lon, location_name, country = result['latitude'], result['longitude'], result['name'], result.get('country', '')
    params = {"latitude": lat, "longitude": lon, "timezone": "auto", "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"}
    if forecast:
        params["daily"] = "weather_code,temperature_2m_max,temperature_2m_min"
        params["forecast_days"] = 3
    weather_resp = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
    if weather_resp.status_code != 200:
        return "Error: Weather API failed"
    data = weather_resp.json()
    current = data.get('current', {})
    if not current:
        return f"Error: No weather data for {location_name}"
    wmo_code = current.get('weather_code', 0)
    weather_desc = WMO_CODES.get(wmo_code, f"Code {wmo_code}")
    lines = [f"{location_name}, {country}" if country else f"{location_name}", f"{current.get('temperature_2m', 'N/A')}C", f"{weather_desc}", f"Wind: {current.get('wind_speed_10m', 'N/A')} km/h | Humidity: {current.get('relative_humidity_2m', 'N/A')}%"]
    if forecast and 'daily' in data:
        daily = data['daily']
        lines.append("Forecast:")
        for i in range(min(3, len(daily.get('time', [])))):
            d = daily['time'][i]
            max_t = daily.get('temperature_2m_max', [])[i] if i < len(daily.get('temperature_2m_max', [])) else 'N/A'
            min_t = daily.get('temperature_2m_min', [])[i] if i < len(daily.get('temperature_2m_min', [])) else 'N/A'
            code = daily.get('weather_code', [])[i] if i < len(daily.get('weather_code', [])) else 0
            lines.append(f"  {d}: {min_t}-{max_t}C, {WMO_CODES.get(code, 'Unknown')}")
    return "\n".join(lines)


# ─── SHELL ───────────────────────────────────────────────────────────────────

async def run_shell(command: str, timeout: int = 60) -> str:
    if not command or not isinstance(command, str) or not command.strip():
        return "Error: Empty command."
    for pat, reason in _ANTI_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return f"BLOCKED: {reason}. Command: {command[:100]}"
    try:
        process = await asyncio.create_subprocess_shell(command.strip(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=int(timeout) if isinstance(timeout, (int, float, str)) else 60)
        out, err = stdout.decode('utf-8', errors='replace').strip(), stderr.decode('utf-8', errors='replace').strip()
        result = f"Exit: {process.returncode}"
        if out: result += f"\nSTDOUT:\n{out[:5000]}"
        if err: result += f"\nSTDERR:\n{err[:2000]}"
        return result
    except asyncio.TimeoutError:
        return f"Timeout after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# ─── FILESYSTEM ──────────────────────────────────────────────────────────────

async def list_files(path: str = ".", recursive: bool = False, exclude_cache: bool = True) -> str:
    target = Path(path)
    if not target.exists():
        return f"Error: Path does not exist: {path}"
    if not target.is_dir():
        return f"Error: Not a directory: {path}"
    if not _is_safe_path(str(target)):
        return f"Error: Access denied to {path}"
    ignore_dirs = {'.git', '__pycache__', 'venv', 'env', 'node_modules', '.idea', '.vscode', 'tmp'}
    if recursive:
        results = []
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                full_path = Path(root) / file
                results.append(str(full_path.relative_to(target)).replace("\\", "/"))
        return "\n".join(results) if results else "Directory is empty."
    else:
        items = [f"{'DIR' if item.is_dir() else '   '} {item.name}" for item in sorted(target.iterdir())]
        return "\n".join(items) if items else f"(empty directory: {path})"


async def read_file(path: str) -> str:
    if not _is_safe_path(path):
        return f"Error: Access denied: {path}"
    target = Path(path)
    if not target.exists():
        return f"Error: File not found: {path}"
    if not target.is_file():
        return f"Error: Not a file: {path}"
    try:
        return target.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        try:
            return target.read_text(encoding='latin-1')
        except Exception as e:
            return f"Error: Cannot decode file: {e}"
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except Exception as e:
        return f"Error: {e}"


async def write_file(path: str, content: str = "") -> str:
    if not path or not isinstance(path, str) or not _is_safe_path(path):
        return "Error: Invalid path or access denied."
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return f"Written to {path}"
    except Exception as e:
        return f"Error: {e}"


async def append_file(path: str, content: str = "") -> str:
    if not path or not isinstance(path, str) or not _is_safe_path(path):
        return "Error: Invalid path or access denied."
    try:
        target = Path(path)
        existing = target.read_text(encoding='utf-8') if target.exists() else ""
        target.write_text(existing + content, encoding='utf-8')
        return f"Appended to {path}"
    except Exception as e:
        return f"Error: {e}"


async def edit_file(path: str, find: str, replace: str = "") -> str:
    if not path or not isinstance(path, str) or not _is_safe_path(path):
        return "Error: Invalid path or access denied."
    try:
        target = Path(path)
        if not target.exists() or not target.is_file():
            return f"Error: File not found or not a file: {path}"
        content = target.read_text(encoding='utf-8')
        if find not in content:
            return f"Error: Text not found in file."
        target.write_text(content.replace(find, replace), encoding='utf-8')
        return f"Made {content.count(find)} replacement(s) in {path}"
    except Exception as e:
        return f"Error: {e}"


async def glob_files(pattern: str = "**/*", path: str = ".") -> str:
    base = Path(path)
    if not base.exists() or not _is_safe_path(str(base)):
        return f"Error: Invalid path or access denied: {path}"
    matches = list(base.glob(pattern))
    if not matches:
        return f"No files match: {pattern}"
    output = [f"Files matching '{pattern}' in {path}:\n"]
    for match in sorted(matches)[:50]:
        output.append(f"  {match}")
    if len(matches) > 50:
        output.append(f"  ... and {len(matches) - 50} more")
    return "\n".join(output)


async def grep_files(pattern: str, path: str = ".", file_type: str = "", use_regex: bool = False) -> str:
    if not pattern or not isinstance(pattern, str):
        return "Error: No search pattern provided."
    base = Path(path)
    if not base.exists() or not _is_safe_path(str(base)):
        return f"Error: Invalid path or access denied: {path}"
    try:
        search_pattern = re.compile(pattern) if use_regex else re.compile(re.escape(pattern))
        matches = []
        for file_path in base.rglob(f"*{file_type}" if file_type else "*"):
            if file_path.is_file() and file_path.stat().st_size <= 1000000:
                try:
                    content = file_path.read_text(encoding='utf-8', errors='ignore')
                    for i, line in enumerate(content.split('\n'), 1):
                        if search_pattern.search(line):
                            matches.append(f"{file_path}:{i}: {line.strip()}")
                except Exception:
                    pass
        if not matches:
            return f"No matches for '{pattern}' in {path}"
        output = [f"Matches for '{pattern}' in {path}:\n"] + matches[:100]
        if len(matches) > 100:
            output.append(f"  ... and {len(matches) - 100} more")
        return "\n".join(output)
    except Exception as e:
        return f"Error: {e}"


async def get_code_skeleton(file_path: str) -> str:
    if not _is_safe_path(file_path):
        return "Security Error: Access Denied."
    target_path = Path(file_path).resolve()
    if not target_path.exists():
        return f"Error: File not found: {file_path}"
    if not target_path.is_file():
        return f"Error: Not a file: {file_path}"
    try:
        source = target_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"Syntax Error in file: {e}"
    output = [f"# {target_path.name} STRUCTURE"]
    items = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            bases = [base.id if isinstance(base, ast.Name) else str(base) for base in node.bases]
            base_str = f"({', '.join(bases)})" if bases else ""
            items.append(('class', node.lineno, f"class {node.name}{base_str}", 1))
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef) and not child.name.startswith('_'):
                    args = [arg.arg for arg in child.args.args if arg.arg != 'self']
                    items.append(('method', child.lineno, f"  def {child.name}({', '.join(args)})", 2))
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
            args = [arg.arg for arg in node.args.args if arg.arg != 'self']
            items.append(('func', node.lineno, f"def {node.name}({', '.join(args)})", 0))
    if not items:
        return "No structure found."
    lines = [f"{lineno:4d} | {'  ' * indent}{name}" for item_type, lineno, name, indent in items]
    return "\n".join(output + ["\n"] + lines)


# ─── VISION ──────────────────────────────────────────────────────────────────

async def analyze_image(prompt: str, image_path: str) -> str:
    if not _is_safe_path(image_path):
        return f"Error: Access denied: {image_path}"
    try:
        from src.core.provider import get_provider
        config = load_config()
        provider = get_provider(config.provider, config)
        if not hasattr(provider, 'analyze_image'):
            return f"Error: Provider {config.provider} does not support vision."
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode('utf-8')
        return await provider.analyze_image(prompt, f"data:image/jpeg;base64,{image_data}")
    except Exception as e:
        return f"Error: {e}"


async def generate_image(prompt: str, width: int = None, height: int = None) -> str:
    config = load_config()
    safe_prompt = quote(prompt)
    actual_width = width if width is not None else config.pollinations.image_width
    actual_height = height if height is not None else config.pollinations.image_height
    url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width={actual_width}&height={actual_height}&nologo=true&enhance=true"
    return await download_and_open_image(url)


async def download_and_open_image(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return f"Security Error: URL scheme '{parsed.scheme}' is not allowed."
        from src.core.provider import get_http_client
        client = get_http_client()
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            return f"Download Failed: {resp.status_code}"
        filename = f"img_{int(time.time())}.jpg"
        path = TEMP_DIR / filename
        async with aiofiles.open(path, "wb") as f:
            await f.write(resp.content)
        if platform.system() == "Windows":
            os.startfile(str(path))
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
        return str(path)
    except Exception as e:
        return f"Image Download Error: {e}"


async def take_screenshot(filename: str = "screen.png") -> str:
    try:
        import mss
        path = TEMP_DIR / filename
        with mss.mss() as sct:
            sct.shot(mon=-1, output=str(path))
        return str(path)
    except Exception as e:
        return f"Screenshot Error: {e}"


async def analyze_screen(question: str) -> str:
    try:
        import pyautogui
        path_str = await take_screenshot("vision_buffer.png")
        from src.core.provider import get_provider
        config = load_config()
        provider = get_provider(config.provider, config)
        if not hasattr(provider, 'analyze_image'):
            return "Error: Current provider does not support Vision."
        width, height = pyautogui.size()
        return f"Screen Analysis: {await provider.analyze_image(f'{question}. Screen: {width}x{height}.', path_str)}"
    except Exception as e:
        return f"Vision Error: {e}"


# ─── DESKTOP AUTOMATION ─────────────────────────────────────────────────────

async def mouse_click(x: int, y: int) -> str:
    try:
        import pyautogui
        pyautogui.click(x=int(x) if isinstance(x, (int, float, str)) else 0, y=int(y) if isinstance(y, (int, float, str)) else 0)
        return f"Clicked at {x}, {y}"
    except Exception as e:
        return f"Click Error: {e}"


async def type_text(text: str) -> str:
    try:
        import pyautogui
        pyautogui.write(text, interval=0.05)
        return f"Typed: {text}"
    except Exception as e:
        return f"Type Error: {e}"


# ─── SKILLS ──────────────────────────────────────────────────────────────────

async def fetch_and_install_skill(url: str) -> str:
    try:
        from src.core.provider import get_http_client
        client = get_http_client()
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            return f"Error: Failed to fetch skill (HTTP {resp.status_code})"
        content = resp.text
        if not content.startswith("---"):
            return "Error: No YAML frontmatter found. Skill must start with --- block."
        parts = content.split("---", 2)
        if len(parts) < 3:
            return "Error: Invalid YAML frontmatter format."
        import yaml
        meta = yaml.safe_load(parts[1].strip())
        if not meta or not meta.get("description") or not meta.get("tools"):
            return "Error: Frontmatter must have 'description' and 'tools' fields."
        skill_name = meta.get("name", url.split("/")[-1].replace(".md", ""))
        category = meta.get("category", "INTEGRATION")
        skill_dir = Path(f"src/skills/{category}")
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / f"{skill_name}.md").write_text(content, encoding="utf-8")
        from src.skills_loader import skill_index
        skill_index.reload()
        return f"Installed skill: {skill_name} (category: {category})"
    except ImportError as e:
        return f"Error: Missing dependency - {e}"
    except Exception as e:
        return f"Error: {e}"


async def find_skill(tags) -> str:
    from src.skills_loader import skill_index
    if tags is None:
        return json.dumps([{"name": s} for s in skill_index.skills.keys()])
    if isinstance(tags, str):
        tags = [t.strip().lower() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, (list, tuple)):
        tags = [str(t).strip().lower() for t in tags if t]
    else:
        tags = [str(tags).lower()]
    if not tags or "*" in tags or "all" in tags:
        return json.dumps([{"name": s} for s in skill_index.skills.keys()])
    skill = skill_index.find_by_tags(tags)
    if skill:
        return json.dumps({"name": skill.name, "description": skill.description})
    return "Error: No matching skill found"


async def list_skills() -> str:
    from src.skills_loader import skill_index, get_all_roles
    skills = list(skill_index.skills.keys())
    roles = list(get_all_roles().keys())
    result = []
    if roles:
        result.append("Agents/Roles:\n" + "\n".join(f"  - {r}" for r in roles))
    if skills:
        result.append("Skills:\n" + "\n".join(f"  - {s}" for s in skills))
    return "\n\n".join(result) if result else "No skills or roles available."


# ─── MEMORY ──────────────────────────────────────────────────────────────────

async def add_memory(content: str, category: str = "general", tier: str = "now") -> str:
    from src.core.memory import memory_core
    return memory_core.add_memory(content, category, tier)


async def promote_memory(memory_id: str, new_tier: str = "recent") -> str:
    from src.core.memory import memory_core
    return memory_core.promote_memory(memory_id, new_tier)


async def search_memory(query: str, limit: int = 5) -> str:
    from src.core.memory import memory_core
    try:
        limit = max(1, min(int(limit) if isinstance(limit, (int, float, str)) else 5, 20))
    except (ValueError, TypeError):
        limit = 5
    results = memory_core.search_memory(query, limit)
    return json.dumps(results, ensure_ascii=False) if results else "No memories found."


# ─── TODO ────────────────────────────────────────────────────────────────────

async def manage_todo(action: str, task: str = "", todo_id: str = "") -> str:
    todo_file = Path("tmp/todos.json")
    todos = []
    if todo_file.exists():
        try:
            todos = json.loads(todo_file.read_text())
        except Exception:
            pass
    if action == "add" and task:
        todos.append({"id": len(todos) + 1, "task": task, "done": False})
        todo_file.write_text(json.dumps(todos))
        return f"Added: {task}"
    elif action == "list":
        return "\n".join(f"{t['id']}. {'[x]' if t['done'] else '[ ]'} {t['task']}" for t in todos) if todos else "No TODOs."
    elif action == "done" and todo_id:
        for t in todos:
            if str(t["id"]) == todo_id:
                t["done"] = True
                todo_file.write_text(json.dumps(todos))
                return f"Marked done: {t['task']}"
        return "TODO not found"
    elif action == "remove" and todo_id:
        todos = [t for t in todos if str(t["id"]) != todo_id]
        todo_file.write_text(json.dumps(todos))
        return "TODO removed"
    elif action == "clear":
        todo_file.write_text("[]")
        return "All TODOs cleared"
    return "Usage: manage_todo(action='add|list|done|remove|clear', task='...', todo_id='...')"


# ─── RESPONSE ────────────────────────────────────────────────────────────────

async def response(text: str = "") -> str:
    return text or ""


# ─── UTILITIES ───────────────────────────────────────────────────────────────

async def calc(expression: str) -> str:
    try:
        from simpleeval import simple_eval  # type: ignore[import-untyped]
        return str(simple_eval(expression, names={}))
    except Exception as e:
        return f"Error: {e}"


async def format_json(data: str, mode: str = "pretty") -> str:
    try:
        parsed = json.loads(data) if isinstance(data, str) else data
        return json.dumps(parsed, separators=(',', ':')) if mode == "minify" else json.dumps(parsed, indent=2)
    except Exception as e:
        return f"Error: {e}"


async def generate_uuid() -> str:
    return str(uuid.uuid4())


async def generate_random_string(length: int = 16) -> str:
    try:
        length = max(1, min(int(length) if isinstance(length, (int, float, str)) else 16, 64))
    except (ValueError, TypeError):
        length = 16
    return secrets.token_urlsafe(length)[:length]


async def hash_string(text: str, algorithm: str = "sha256") -> str:
    try:
        return hashlib.new(algorithm, text.encode('utf-8')).hexdigest()
    except Exception as e:
        return f"Error: {e}"


async def base64_encode(text: str) -> str:
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


async def base64_decode(encoded: str) -> str:
    try:
        return base64.b64decode(encoded.encode('ascii')).decode('utf-8')
    except Exception as e:
        return f"Error: {e}"


# ─── MCP ──────────────────────────────────────────────────────────────────────

async def add_mcp_server(name: str, command: str, args: str = "[]", env: str = "{}") -> str:
    if not name or not command:
        return "Error: name and command are required."
    try:
        args_list = json.loads(args) if isinstance(args, str) else args
    except json.JSONDecodeError:
        return f"Error: args must be a JSON array, got: {args}"
    try:
        env_dict = json.loads(env) if isinstance(env, str) else env
    except json.JSONDecodeError:
        return f"Error: env must be a JSON object, got: {env}"
    if not isinstance(args_list, list):
        return "Error: args must be a JSON array like [\"-y\", \"@modelcontextprotocol/server-x\"]"
    if not isinstance(env_dict, dict):
        return "Error: env must be a JSON object like {\"KEY\": \"value\"}"
    config = load_config()
    from src.config import MCPServerConfig
    config.mcp_servers[name] = MCPServerConfig(command=command, args=args_list, env=env_dict, enabled=True)
    config.save()
    return f"MCP server '{name}' added and enabled. Restart to connect."


async def list_mcp_servers() -> str:
    import shutil
    config = load_config()
    lines = []
    for name, cfg in config.mcp_servers.items():
        status = "ON" if cfg.enabled else "OFF"
        if cfg.command == "internal":
            installed = "yes"
        elif cfg.args and cfg.args[0] == "-m" and len(cfg.args) > 1:
            try:
                __import__(cfg.args[1])
                installed = "yes"
            except ImportError:
                installed = "NO"
        else:
            installed = "yes" if (shutil.which(cfg.command) or shutil.which(f"{cfg.command}.exe")) else "NO"
        lines.append(f"- {name}: {status}, installed={installed}, cmd={cfg.command}")
    return "\n".join(lines) if lines else "No MCP servers configured."


# ─── DISCOVERY ───────────────────────────────────────────────────────────────

async def scan_tools() -> str:
    return "Available tools:\n" + "\n".join(f"- {name}" for name in TOOL_REGISTRY.keys())


# ─── REGISTRY ────────────────────────────────────────────────────────────────

def _generate_registry():
    current_module = sys.modules[__name__]
    registry = {}
    for name, func in inspect.getmembers(current_module, inspect.isfunction):
        if not name.startswith("_") and func.__module__ == __name__ and name not in ('_generate_registry', 'get_tools_schema', 'download_and_open_image'):
            registry[name] = func
    return registry


def get_tools_schema(tools: dict = None) -> str:
    source = tools if tools is not None else TOOL_REGISTRY
    schema = []
    for name, func in source.items():
        try:
            sig = inspect.signature(func)
            params = str(sig).replace(" -> str", "").replace("**kwargs", "")
        except (ValueError, TypeError):
            params = "(...)"
        try:
            doc = inspect.getdoc(func) or "Tool."
        except Exception:
            doc = "Tool."
        schema.append(f"- {name}{params}: {doc}")
    return "\n".join(schema)


TOOL_REGISTRY = MappingProxyType(_generate_registry())
