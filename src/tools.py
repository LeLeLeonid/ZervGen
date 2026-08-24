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
import warnings
from typing import List
from pathlib import Path
from urllib.parse import quote, urlparse
from types import MappingProxyType
import aiofiles
import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from rich.console import Console
from src.config import load_config, _ANTI_PATTERNS, TEMP_DIR, WMO_CODES

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

TEMP_DIR.mkdir(exist_ok=True)
console = Console()

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

def _clip(s: str, n: int) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    h, t = int(n * 0.7), int(n * 0.2)
    return s[:h] + f"\n...[TRUNCATED {len(s) - h - t} CHARS]...\n" + s[-t:]


def _atomic_write(path: Path, content: str) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        if tmp is not None:
            try: os.unlink(tmp)
            except OSError: pass
        raise

# ── DELEGATION ──────────────────────────────────────────────────────────────

async def delegate_to(agent_name: str = "", agent_id: str = "", task: str = "", context: str = "", mode: str = "ASK", memory: bool = True, agents: str = "") -> str:
    import inspect
    from src.skills_loader import role_exists

    frame = inspect.currentframe()
    orchestrator = None
    while frame:
        orchestrator = frame.f_globals.get("_orchestrator")
        if hasattr(orchestrator, '_handle_delegation'):
            break
        orchestrator = None
        frame = frame.f_back

    if orchestrator and hasattr(orchestrator, '_handle_delegation'):
        return await orchestrator._handle_delegation({
            "agent_name": agent_name, "agent_id": agent_id, "task": task, "context": context,
            "mode": mode, "memory": memory, "agents": agents
        })

    return "Error: delegation requires an orchestrator. Run via CLI, not standalone."

# ─── WEB ─────────────────────────────────────────────────────────────────────

async def web_search(query: str, limit: int = 5, region: str = "wt-wt", recency: str = "", **kwargs) -> str:
    if not query or not isinstance(query, str):
        return "Error: Invalid search query."
    try:
        limit = max(1, min(int(limit) if isinstance(limit, (int, float, str)) else 5, 10))
    except (ValueError, TypeError):
        limit = 5
    try:
        from ddgs import DDGS
        kw = {"max_results": limit, "region": region or "wt-wt"}
        with DDGS() as ddgs:
            try:
                results = list(ddgs.text(query.strip(), timelimit=recency, **kw)) if recency else list(ddgs.text(query.strip(), **kw))
            except TypeError:
                results = list(ddgs.text(query.strip(), **kw))
        if not results:
            return f"Error: No results for '{query}'."
        structured = [
            {
                "title": (r.get("title") or "").strip() or "No Title",
                "url": r.get("url") or r.get("href") or r.get("link") or "",
                "snippet": (r.get("body") or r.get("snippet") or "").strip()[:200],
            }
            for r in results
        ]
        return structured  
    except ImportError:
        return "Error: ddgs not installed. pip install duckduckgo-search"
    except Exception as e:
        return f"Error: Search error: {e}"


async def fetch_url(url: str, parse_html: bool = True, max_chars: int = 8000, timeout: float = 30.0, **kwargs) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Error: Invalid URL scheme: {parsed.scheme}"
        from src.core.provider import get_http_client
        if not hasattr(fetch_url, "_ua"):
            try:
                from fake_useragent import UserAgent
                fetch_url._ua = UserAgent().random
            except Exception:
                fetch_url._ua = "Mozilla/5.0"
        client = get_http_client()
        response = await client.get(url, headers={"User-Agent": fetch_url._ua}, follow_redirects=True, timeout=timeout)
        if response.status_code != 200:
            return f"Error: HTTP {response.status_code} for {url}"
        raw = response.text
        title, text = "", ""
        if parse_html:
            try:
                import trafilatura
                text = trafilatura.extract(raw, include_tables=True, include_links=False, favor_recall=True) or ""
            except Exception:
                text = ""
            soup = BeautifulSoup(raw, "html.parser")
            t = soup.find("title")
            title = t.get_text(strip=True) if t else ""
            if not text:
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
        else:
            text = raw
        text = _clip(text, max_chars)
        return {"url": url, "title": title, "text": text or "No readable content."}
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

async def shell(command: str, timeout: int = 60) -> str:
    if not command or not isinstance(command, str) or not command.strip():
        return "Error: Empty command."
    for pat, reason in _ANTI_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return f"BLOCKED: {reason}. Command: {command[:100]}"
    try:
        from src.core.memory import memory_core
        for block in memory_core.get_runtime_blocks():
            detect = block.get("detect", "")
            if detect and detect.lower() in command.lower():
                return f"BLOCKED: {block.get('reason', 'Known bad pattern')}. Fix: {block.get('fix', 'See error details')}"
    except Exception:
        pass
    try:
        process = await asyncio.create_subprocess_shell(command.strip(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=int(timeout) if isinstance(timeout, (int, float, str)) else 60)
        out, err = stdout.decode('utf-8', errors='replace').strip(), stderr.decode('utf-8', errors='replace').strip()
        return {
            "exit_code": process.returncode,
            "stdout": out,
            "stderr": err
        }
    except asyncio.TimeoutError:
        return f"Timeout after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# ─── FILESYSTEM ──────────────────────────────────────────────────────────────

async def list_files(path: str = ".", recursive: bool = False, exclude_cache: bool = True, ignore_dir: bool = True) -> str:
    target = Path(path)
    if not target.exists():
        return f"Error: Path does not exist: {path}"
    if not target.is_dir():
        return f"Error: Not a directory: {path}"
    if not _is_safe_path(str(target)):
        return f"Error: Access denied to {path}"
    if ignore_dir:
        ignore_dirs = {'.git', '__pycache__', 'venv', 'env', 'node_modules', '.idea', '.vscode', '.kilo', 'tmp'}
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


async def read_file(path: str, offset: int = 0, max_chars: int = 8000) -> str:
    if not _is_safe_path(path):
        return f"Error: Access denied: {path}"
    target = Path(path)
    if not target.exists():
        return f"Error: File not found: {path}"
    if not target.is_file():
        return f"Error: Not a file: {path}"
    try:
        # errors='replace' swaps bad bytes with  instead of crashing
        text = target.read_text(encoding='utf-8', errors='replace')
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except Exception as e:
        return f"Error: {e}"
    if offset:
        text = text[offset:]
    return _clip(text, max_chars)


async def write_file(path: str, content: str = "") -> str:
    if not path or not isinstance(path, str) or not _is_safe_path(path):
        return "Error: Invalid path or access denied."
    try:
        _atomic_write(Path(path), content)
        return f"Written to {path}"
    except Exception as e:
        return f"Error: {e}"


async def append_file(path: str, content: str = "") -> str:
    if not path or not isinstance(path, str) or not _is_safe_path(path):
        return "Error: Invalid path or access denied."
    try:
        target = Path(path)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        _atomic_write(target, existing + content)
        return f"Appended to {path}"
    except Exception as e:
        return f"Error: {e}"


async def edit_file(path: str, find: str, replace: str = "", replace_all: bool = False) -> str:
    if not path or not isinstance(path, str) or not _is_safe_path(path):
        return "Error: Invalid path or access denied."
    try:
        target = Path(path)
        if not target.exists() or not target.is_file():
            return f"Error: File not found or not a file: {path}"
        content = target.read_text(encoding="utf-8")
        count = content.count(find)
        if count == 0:
            return "Error: Text not found in file."
        if count > 1 and not replace_all:
            return f"Error: find matches {count} times. Pass replace_all=True or add surrounding context to make it unique."
        new = content.replace(find, replace) if replace_all else content.replace(find, replace, 1)
        _atomic_write(target, new)
        return f"Made {count if replace_all else 1} replacement(s) in {path}"
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

async def generate_image(prompt: str, width: int = None, height: int = None, model: str = "flux", seed: int = None) -> str:
    config = load_config()
    provider = None
    try:
        from src.core.provider import get_provider
        provider = get_provider(config.provider, config)
    except Exception:
        provider = None
    if provider is not None and hasattr(provider, "generate_image"):
        try:
            return await provider.generate_image(prompt, width=width, height=height)
        except Exception as e:
            return f"Error: provider image gen failed ({e}); falling back to pollinations."
    safe_prompt = quote(prompt)
    w = width if width is not None else config.pollinations.image_width
    h = height if height is not None else config.pollinations.image_height
    params = [f"width={w}", f"height={h}", "nologo=true", "enhance=true", f"model={quote(model)}"]
    if seed is not None:
        params.append(f"seed={int(seed)}")
    token = getattr(config.pollinations, "token", "") or getattr(config.pollinations, "api_key", "") or os.environ.get("POLLINATIONS_TOKEN", "")
    url = f"https://image.pollinations.ai/prompt/{safe_prompt}?{'&'.join(params)}"
    return await download_and_open_image(url, token=token)


async def download_and_open_image(url: str, token: str = "") -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Security Error: URL scheme '{parsed.scheme}' is not allowed."
        from src.core.provider import get_http_client
        client = get_http_client()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = None
        for attempt in range(2):
            resp = await client.get(url, headers=headers, follow_redirects=True)
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            if attempt == 0:
                await asyncio.sleep(2)
        if resp.status_code != 200:
            return (
                f"Image gen failed: HTTP {resp.status_code}. "
                "Pollinations unauthenticated is rate-limited in 2026. "
                "Set pollinations.token in config (or POLLINATIONS_TOKEN), "
                "or route via an OpenRouter image model (provider.generate_image)."
            )
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


# ─── AUTOMATION ─────────────────────────────────────────────────────

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

async def find_skill(tags) -> str:
    from src.skills_loader import skill_index
    if tags is None:
        return json.dumps([{"name": s} for s in skill_index.skills.keys()])
    if isinstance(tags, str):
        tags = [t.strip().lower() for t in re.split(r'[,\s]+', tags) if t.strip()]
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
    roles = ", ".join(get_all_roles().keys())
    skills = ", ".join(skill_index.skills.keys())
    out = []
    if roles:
        out.append(f"Agents:\n{roles}")
    if skills:
        out.append(f"Skills:\n{skills}")
    return "\n\n".join(out) if out else "No skills or roles available."


async def load_skill(name: str) -> str:
    """Read the full body of a skill by name."""
    from src.skills_loader import skill_index
    s = skill_index.get(name)
    return s.body if s else f"Error: skill '{name}' not found"


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
    return results if results else []


async def search_tgs(query: str, limit: int = 3, **kwargs) -> str:
    """Retrieve graph-enhanced, multi-hop context. Use for complex queries requiring relationship tracing, verification, or architecture understanding."""
    from src.core.memory import memory_core
    try:
        limit = max(1, min(int(limit) if isinstance(limit, (int, float, str)) else 3, 5))
    except (ValueError, TypeError):
        limit = 3
    return memory_core.search_tgs(query, limit=limit)


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

async def response(text: str = None) -> str:
    import inspect
    frame = inspect.currentframe()
    agent = None
    while frame:
        agent = frame.f_globals.get("_orchestrator")
        if hasattr(agent, '_response_called'):
            break
        agent = None
        frame = frame.f_back
    if agent and hasattr(agent, '_response_called'):
        agent._response_called = True
        agent._response_value = text if text is not None else ""
        return agent._response_value
    return text if text is not None else ""


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

async def add_mcp_server(name: str, command: str, args: str = "[]", env: str = "{}", default: bool = False) -> str:
    if not name or not command:
        return "Error: name and command are required."
    try:
        args_list = json.loads(args) if isinstance(args, str) else args
        env_dict = json.loads(env) if isinstance(env, str) else env
    except json.JSONDecodeError:
        return "Error: Invalid JSON for args or env."
    config = load_config()
    from src.config import MCPServerConfig
    config.mcp_servers[name] = MCPServerConfig(command=command, args=args_list, env=env_dict, enabled=True)
    config.save()
    if default:
        cfg_path = Path("src/config.py")
        text = cfg_path.read_text(encoding="utf-8")
        new_entry = f'    "{name}": MCPServerConfig(command="{command}", args={json.dumps(args_list)}, env={json.dumps(env_dict)}, enabled=True),\n'
        import re
        pattern = r'(DEFAULT_MCP_SERVERS\s*=\s*\{)(.*?)(\n\})'
        match = re.search(pattern, text, re.DOTALL)
        if match and name not in match.group(2):
            text = text[:match.end(2)] + "\n" + new_entry + text[match.start(3):]
            cfg_path.write_text(text, encoding="utf-8")
            return f"MCP server '{name}' added to config.json AND hardcoded into src/config.py."  
    return f"MCP server '{name}' added to config.json."


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


async def mcp_execute(server: str, tool: str = "", arguments: str = "{}", args: str = None) -> str:
    """Execute a tool on an MCP server, or discover its catalog when tool is empty/'list'."""
    from src.core.mcp_manager import MCPManager
    mgr = MCPManager()
    if args is not None and arguments == "{}":
        arguments = args
    if tool in ("", "list"):
        srv = mgr.servers.get(server)
        if not srv:
            return f"Error: server '{server}' not found."
        if not srv.connected:
            return f"Error: server '{server}' not connected."
        tools_list = []
        for name, tdef in srv.tools.items():
            schema = tdef if isinstance(tdef, dict) else getattr(tdef, "inputSchema", None) or {}
            keys = ", ".join((schema.get("properties") or {}).keys())
            tools_list.append(f"{name}({keys})")
        return f"Server '{server}' tools ({len(tools_list)}):\n" + "\n".join(tools_list)
    try:
        args_dict = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args_dict, dict):
            return "Error: arguments must be a JSON object."
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON arguments: {e}"
    return await mgr.execute_tool(tool_name=tool, arguments=args_dict, server=server)


# ─── DISCOVERY ───────────────────────────────────────────────────────────────

async def scan_tools() -> str:
    import inspect
    frame = inspect.currentframe()
    agent = None
    while frame:
        agent = frame.f_globals.get("_orchestrator")
        if agent is not None and hasattr(agent, "tools"):
            break
        agent = None
        frame = frame.f_back
    names = list(agent.tools.keys()) if agent else list(TOOL_REGISTRY.keys())
    return "Available tools:\n" + "\n".join(f"- {n}" for n in names)


# ─── REGISTRY ────────────────────────────────────────────────────────────────

def _generate_registry():
    current_module = sys.modules[__name__]
    registry = {}
    for name, func in inspect.getmembers(current_module, inspect.isfunction):
        if not name.startswith("_") and func.__module__ == __name__ and name not in ('_generate_registry', 'get_tools_schema', 'download_and_open_image', 'reload_allowed_roots'):
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
