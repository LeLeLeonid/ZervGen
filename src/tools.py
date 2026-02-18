import os
import sys
import json
import ast
import asyncio
import io
import subprocess
import platform
import time
import inspect
import re
import logging
import multiprocessing
import uuid
import base64
import hashlib
import secrets
import smtplib
import ssl
from datetime import datetime
from typing import List, Dict, Any, Optional
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

logger = logging.getLogger(__name__)
console = Console()

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Drizzle: Light", 53: "Drizzle: Moderate", 55: "Drizzle: Dense",
    61: "Rain: Slight", 63: "Rain: Moderate", 65: "Rain: Heavy",
    71: "Snow: Slight", 73: "Snow: Moderate", 75: "Snow: Heavy",
    95: "Thunderstorm: Slight or moderate", 99: "Thunderstorm with hail"
}

SAFE_CODE_IMPORTS = {
    'math', 'random', 'statistics', 'itertools', 'collections', 'functools',
    'datetime', 'time', 'json', 're', 'string', 'typing', 'decimal', 'fractions',
    'hashlib', 'uuid', 'base64', 'binascii', 'copy', 'numbers', 'enum'
}
ALLOWED_PATHS = [Path.cwd()]
_timer_start: Optional[float] = None


def _is_safe_path(path: str) -> bool:
    """Check if path is within allowed directories. Allows creating new files."""
    try:
        target = Path(path).resolve()
        # Check if path is within cwd (allows subdirectories that don't exist yet)
        return target.is_relative_to(Path.cwd())
    except (OSError, ValueError):
        return False


async def delegate_to(agent_name: str, task: str, context: str = "", mode: str = "ASK", **kwargs) -> str:
    """Delegate task to a specialized agent. Returns result string."""
    from src.core.base_agent import BaseAgent
    from src.skills_loader import role_exists
    from src.config import load_config
    
    if not role_exists(agent_name):
        return f"Error: Agent/Skill '{agent_name}' does not exist"
    
    config = load_config()
    full_task = f"{context}\n\n{task}" if context else task
    agent = await BaseAgent.create(role=agent_name, mode=mode, settings=config)
    agent._is_delegated = True  # Mark as delegated for logging purposes
    agent.tools.pop("delegate_to", None)
    result = await agent.run(full_task)
    return result


async def get_code_skeleton(file_path: str, **kwargs) -> str:
    try:
        if not _is_safe_path(file_path):
            return "Security Error: Access Denied."

        target_path = Path(file_path).resolve()
        if not target_path.exists():
            return f"Error: File not found: {file_path}"
        if not target_path.is_file():
            return f"Error: Not a file: {file_path}"

        with open(target_path, "r", encoding="utf-8") as f:
            source = f.read()

        try:
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
        output.append("\n" + "\n".join(lines))
        return "\n".join(output)

    except Exception as e:
        return f"Error: {e}"


async def run_safe_code(code: str, timeout: int = 30, **kwargs) -> str:
    """Execute Python code in sandbox. Limited imports."""
    if not code or not isinstance(code, str):
        return "Error: No code."
    code = code.strip()
    if not code:
        return "Error: Empty code."
    timeout = int(timeout) if isinstance(timeout, (int, float, str)) else 30
    
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax Error: {e}"
    
    # Security checks
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in SAFE_CODE_IMPORTS:
                    return f"Security: Import '{alias.name}' not allowed."
        elif isinstance(node, ast.ImportFrom):
            if node.module not in SAFE_CODE_IMPORTS:
                return f"Security: Module '{node.module}' not allowed."
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ['eval', 'exec', 'compile', '__import__', 'open']:
                return f"Security: '{node.func.id}' not allowed."
    
    def run(code_str, q):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            safe = {name: __builtins__[name] for name in dir(__builtins__) 
                    if name not in ['eval', 'exec', 'compile', '__import__', 'open', 'input', 'exit', 'quit']}
            for m in SAFE_CODE_IMPORTS:
                try: safe[m] = __import__(m)
                except: pass
            exec(code_str, {'__builtins__': safe})
            q.put({'ok': True, 'out': sys.stdout.getvalue(), 'err': sys.stderr.getvalue()})
        except Exception as e:
            q.put({'ok': False, 'error': str(e), 'out': sys.stdout.getvalue(), 'err': sys.stderr.getvalue()})
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    
    ctx = multiprocessing.get_context('spawn')
    q = ctx.Queue()
    p = ctx.Process(target=run, args=(code, q))
    p.start()
    p.join(timeout=timeout)
    
    if p.is_alive():
        p.terminate()
        p.join()
        return f"Timeout after {timeout}s"
    
    if not q.empty():
        r = q.get()
        result = r['out'] or ""
        if r['err']: result += f"\nSTDERR: {r['err']}"
        if not r['ok']: result = f"Error: {r['error']}\n{result}"
        return result.strip() or "OK (no output)"
    return "Error: No result"


async def web_search(query: str, limit: int = 5, **kwargs) -> str:
    """Search the web using DuckDuckGo."""
    try:
        if not query or not isinstance(query, str):
            return "Error: Invalid search query."

        query = query.strip()
        if not query:
            return "Error: Empty query."

        try:
            limit = int(limit) if isinstance(limit, (int, float, str)) else 5
            limit = max(1, min(limit, 10))  # Clamp 1-10
        except (ValueError, TypeError):
            limit = 5

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit))

        if not results:
            return f"No results found for '{query}'."

        output = [f"Search Results for '{query}':\n"]
        for i, r in enumerate(results, 1):
            output.append(f"[{i}] {r.get('title', 'No Title')}\n    {r.get('href', '')}\n    {str(r.get('body', ''))[:200]}...\n")
        return "\n".join(output)

    except ImportError:
        return "Error: ddgs not installed. Run: pip install ddgs"
    except Exception as e:
        return f"Search error: {e}"


async def fetch_url(url: str, parse_html: bool = False, **kwargs) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return f"Error: Invalid URL scheme: {parsed.scheme}"

        from src.core.provider import get_http_client
        headers = {"User-Agent": UserAgent().random}
        client = get_http_client()
        response = await client.get(url, headers=headers, follow_redirects=True)

        if response.status_code != 200:
            return f"Error: HTTP {response.status_code}"

        if parse_html:
            soup = BeautifulSoup(response.text, 'html.parser')
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()
            text = soup.get_text(separator='\n', strip=True)
            lines = [line for line in text.split('\n') if line.strip()][:100]
            return '\n'.join(lines) if lines else "No readable content found."
        return response.text[:10000]

    except httpx.TimeoutException:
        return f"Error: Request timed out for {url}"
    except Exception as e:
        return f"Error: {e}"


async def get_weather(location: str, forecast: bool = False, **kwargs) -> str:
    """Get weather from wttr.in (primary) or Open-Meteo (fallback)."""
    try:
        if not location:
            return "Error: Location is required."

        from src.core.provider import get_http_client
        from urllib.parse import quote
        
        client = get_http_client()
        safe_loc = quote(location.strip())
        
        # Try wttr.in first
        try:
            if forecast:
                # Full forecast
                resp = await client.get(f"https://wttr.in/{safe_loc}?T", timeout=10.0)
                if resp.status_code == 200 and resp.text and "Unknown location" not in resp.text:
                    return resp.text.strip()
            else:
                # Compact current weather
                resp = await client.get(f"https://wttr.in/{safe_loc}?format=%l:+%c+%t+%h+%w", timeout=10.0)
                if resp.status_code == 200 and resp.text:
                    return f"🌤️ {resp.text.strip()}"
        except Exception:
            pass  # Fall through to Open-Meteo

        # Fallback: Open-Meteo (JSON API)
        geocode_resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"}
        )
        if geocode_resp.status_code != 200:
            return f"Error: Geocoding failed"

        geocode_data = geocode_resp.json()
        if not geocode_data.get('results'):
            return f"Error: Location not found: {location}"

        result = geocode_data['results'][0]
        lat, lon = result['latitude'], result['longitude']
        location_name = result['name']
        country = result.get('country', '')

        params = {
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        }
        if forecast:
            params["daily"] = "weather_code,temperature_2m_max,temperature_2m_min"
            params["forecast_days"] = 3

        weather_resp = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
        if weather_resp.status_code != 200:
            return f"Error: Weather API failed"

        data = weather_resp.json()
        current = data.get('current', {})
        if not current:
            return f"Error: No weather data for {location_name}"

        wmo_code = current.get('weather_code', 0)
        weather_desc = WMO_CODES.get(wmo_code, f"Code {wmo_code}")

        lines = [
            f"📍 {location_name}, {country}" if country else f"📍 {location_name}",
            f"🌡️ {current.get('temperature_2m', 'N/A')}°C",
            f"☁️ {weather_desc}",
            f"💨 {current.get('wind_speed_10m', 'N/A')} km/h | 💧 {current.get('relative_humidity_2m', 'N/A')}%"
        ]

        if forecast and 'daily' in data:
            daily = data['daily']
            lines.append("📅 Forecast:")
            for i in range(min(3, len(daily.get('time', [])))):
                d = daily['time'][i]
                max_t = daily.get('temperature_2m_max', [])[i] if i < len(daily.get('temperature_2m_max', [])) else 'N/A'
                min_t = daily.get('temperature_2m_min', [])[i] if i < len(daily.get('temperature_2m_min', [])) else 'N/A'
                code = daily.get('weather_code', [])[i] if i < len(daily.get('weather_code', [])) else 0
                desc = WMO_CODES.get(code, "Unknown")
                lines.append(f"  {d}: {min_t}°-{max_t}°C, {desc}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


async def run_shell(command: str, timeout: int = 60, **kwargs) -> str:
    """Execute shell command. Returns stdout/stderr."""
    if not command or not isinstance(command, str):
        return "Error: No command."
    command = command.strip()
    if not command:
        return "Error: Empty command."
    timeout = int(timeout) if isinstance(timeout, (int, float, str)) else 60
    
    try:
        process = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        out = stdout.decode('utf-8', errors='replace').strip()
        err = stderr.decode('utf-8', errors='replace').strip()
        result = f"Exit: {process.returncode}"
        if out: result += f"\nSTDOUT:\n{out}"
        if err: result += f"\nSTDERR:\n{err}"
        return result
    except asyncio.TimeoutError:
        return f"Timeout after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


async def list_files(path: str = ".", recursive: bool = False, **kwargs) -> str:
    try:
        target = Path(path)
        if not target.exists():
            return f"Error: Path does not exist: {path}"
        if not target.is_dir():
            return f"Error: Not a directory: {path}"
        if not _is_safe_path(str(target)):
            return f"Error: Access denied to {path}"

        if recursive:
            results = []
            ignore_dirs = {'.git', '__pycache__', 'venv', 'env', 'node_modules', '.idea', '.vscode', 'tmp'}
            for root, dirs, files in os.walk(target):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                for file in files:
                    full_path = Path(root) / file
                    try:
                        results.append(str(full_path.relative_to(target)).replace("\\", "/"))
                    except Exception:
                        results.append(str(full_path).replace("\\", "/"))
            return "\n".join(results) if results else "Directory is empty."
        else:
            items = [f"{'[📁]' if item.is_dir() else '[📄]'} {item.name}" for item in sorted(target.iterdir())]
            return "\n".join(items) if items else f"(empty directory: {path})"

    except PermissionError:
        return f"Error: Permission denied: {path}"
    except Exception as e:
        return f"Error: {e}"


async def read_file(path: str, **kwargs) -> str:
    try:
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


async def write_file(path: str, content: str = "", **kwargs) -> str:
    try:
        if not path or not isinstance(path, str):
            return "Error: Invalid path."
        if not _is_safe_path(path):
            return f"Error: Access denied: {path}"
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return f"✓ Written to {path}"
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except Exception as e:
        return f"Error: {e}"


async def append_file(path: str, content: str = "", **kwargs) -> str:
    try:
        if not path or not isinstance(path, str):
            return "Error: Invalid path."
        if not _is_safe_path(path):
            return f"Error: Access denied: {path}"
        target = Path(path)
        existing = target.read_text(encoding='utf-8') if target.exists() else ""
        target.write_text(existing + content, encoding='utf-8')
        return f"✓ Appended to {path}"
    except Exception as e:
        return f"Error: {e}"


async def edit_file(path: str, find: str, replace: str = "", **kwargs) -> str:
    try:
        if not path or not isinstance(path, str):
            return "Error: Invalid path."
        if not _is_safe_path(path):
            return f"Error: Access denied: {path}"
        target = Path(path)
        if not target.exists():
            return f"Error: File not found: {path}"
        if not target.is_file():
            return f"Error: Not a file: {path}"

        content = target.read_text(encoding='utf-8')
        if find not in content:
            return f"Error: Text not found in file."

        new_content = content.replace(find, replace)
        target.write_text(new_content, encoding='utf-8')
        return f"✓ Made {content.count(find)} replacement(s) in {path}"
    except Exception as e:
        return f"Error: {e}"


async def glob_files(pattern: str = "**/*", path: str = ".", **kwargs) -> str:
    try:
        base = Path(path)
        if not base.exists():
            return f"Error: Base path does not exist: {path}"
        if not _is_safe_path(str(base)):
            return f"Error: Access denied: {path}"

        matches = list(base.glob(pattern))
        if not matches:
            return f"No files match: {pattern}"

        output = [f"Files matching '{pattern}' in {path}:\n"]
        for match in sorted(matches)[:50]:
            output.append(f"  {match}")
        if len(matches) > 50:
            output.append(f"  ... and {len(matches) - 50} more")
        return "\n".join(output)
    except Exception as e:
        return f"Error: {e}"


async def grep_files(pattern: str, path: str = ".", file_type: str = "", use_regex: bool = False, **kwargs) -> str:
    try:
        if not pattern or not isinstance(pattern, str):
            return "Error: No search pattern provided."

        base = Path(path)
        if not base.exists():
            return f"Error: Base path does not exist: {path}"
        if not _is_safe_path(str(base)):
            return f"Error: Access denied: {path}"

        matches = []
        search_pattern = re.compile(pattern) if use_regex else re.compile(re.escape(pattern))

        for file_path in base.rglob(f"*{file_type}" if file_type else "*"):
            if file_path.is_file():
                try:
                    if file_path.stat().st_size > 1000000:
                        continue
                    content = file_path.read_text(encoding='utf-8', errors='ignore')
                    for i, line in enumerate(content.split('\n'), 1):
                        if search_pattern.search(line):
                            matches.append(f"{file_path}:{i}: {line.strip()}")
                except Exception:
                    pass

        if not matches:
            return f"No matches for '{pattern}' in {path}"

        output = [f"Matches for '{pattern}' in {path}:\n"]
        output.extend(matches[:100])
        if len(matches) > 100:
            output.append(f"  ... and {len(matches) - 100} more")
        return "\n".join(output)
    except Exception as e:
        return f"Error: {e}"


async def view_image(path: str, **kwargs) -> str:
    try:
        from PIL import Image
        if not _is_safe_path(path):
            return f"Error: Access denied: {path}"
        img_path = Path(path)
        if not img_path.exists():
            return f"Error: Image not found: {path}"
        img = Image.open(img_path)
        return f"Image: {path}\n  Size: {img.size[0]}x{img.size[1]} pixels\n  Format: {img.format}\n  Mode: {img.mode}"
    except ImportError:
        return "Error: PIL required. Run: pip install Pillow"
    except Exception as e:
        return f"Error: {e}"


async def view_pdf(path: str, page: int = 1, **kwargs) -> str:
    """View PDF file info and render page."""
    try:
        from pdf2image import convert_from_path
        if not _is_safe_path(path):
            return f"Error: Access denied: {path}"
        pdf_path = Path(path)
        if not pdf_path.exists():
            return f"Error: PDF not found: {path}"
        page = int(page) if isinstance(page, (int, float, str)) else 1
        images = convert_from_path(str(pdf_path), first_page=page, last_page=page)
        if images:
            return f"PDF: {path}\n  Page {page}: Rendered successfully"
        return f"Error: Could not render page {page}"
    except ImportError:
        return "Error: pdf2image required. Run: pip install pdf2image"
    except Exception as e:
        return f"Error: {e}"


async def analyze_image(prompt: str, image_path: str, **kwargs) -> str:
    try:
        from src.providers.anthropic import AnthropicProvider
        if not _is_safe_path(image_path):
            return f"Error: Access denied: {image_path}"
        config = load_config()
        provider = AnthropicProvider(config.anthropic)
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode('utf-8')
        return await provider.analyze_image(prompt, f"data:image/jpeg;base64,{image_data}")
    except ImportError as e:
        return f"Error: Missing package - {e}"
    except Exception as e:
        return f"Error: {e}"


async def send_email(to: str, subject: str = "ZervGen Notification", body: str = "", **kwargs) -> str:
    try:
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER")
        smtp_password = os.environ.get("SMTP_PASSWORD")
        from_address = os.environ.get("SMTP_FROM", smtp_user)
        
        if not all([smtp_host, smtp_user, smtp_password]):
            return "Error: Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD env vars."
        
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(smtp_user, smtp_password)
            server.sendmail(from_address, to, f"Subject: {subject}\n\n{body}")
        return f"✓ Email sent to {to}"
    except Exception as e:
        return f"Error: {e}"


async def clipboard(action: str, content: str = "", **kwargs) -> str:
    try:
        import pyperclip
        if action == "get":
            return pyperclip.paste()
        elif action == "set":
            pyperclip.copy(content)
            return "✓ Copied to clipboard"
        return "Error: action must be 'get' or 'set'"
    except ImportError:
        return "Error: pyperclip required. Run: pip install pyperclip"
    except Exception as e:
        return f"Error: {e}"


async def generate_uuid(**kwargs) -> str:
    return str(uuid.uuid4())


async def generate_random_string(length: int = 16, **kwargs) -> str:
    """Generate random alphanumeric string."""
    try:
        length = int(length) if isinstance(length, (int, float, str)) else 16
        length = max(1, min(64, length))  # Clamp 1-64
    except (ValueError, TypeError):
        length = 16
    return secrets.token_urlsafe(length)[:length]


async def hash_string(text: str, algorithm: str = "sha256", **kwargs) -> str:
    try:
        hash_func = hashlib.new(algorithm)
        hash_func.update(text.encode('utf-8'))
        return hash_func.hexdigest()
    except Exception as e:
        return f"Error: {e}"


async def base64_encode(text: str, **kwargs) -> str:
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


async def base64_decode(encoded: str, **kwargs) -> str:
    try:
        return base64.b64decode(encoded.encode('ascii')).decode('utf-8')
    except Exception as e:
        return f"Error: {e}"


async def format_json(data: str, mode: str = "pretty", **kwargs) -> str:
    try:
        parsed = json.loads(data) if isinstance(data, str) else data
        if mode == "minify":
            return json.dumps(parsed, separators=(',', ':'))
        return json.dumps(parsed, indent=2)
    except Exception as e:
        return f"Error: {e}"


async def validate_json(data: str, **kwargs) -> str:
    try:
        parsed = json.loads(data) if isinstance(data, str) else data
        return f"✓ Valid JSON ({len(str(parsed))} chars)"
    except json.JSONDecodeError as e:
        return f"✗ Invalid JSON: {e}"


async def convert_csv_to_json(csv_data: str, **kwargs) -> str:
    try:
        lines = csv_data.strip().split('\n')
        if len(lines) < 2:
            return "Error: Need header and data rows"
        headers = lines[0].split(',')
        json_list = []
        for line in lines[1:]:
            values = line.split(',')
            if len(values) == len(headers):
                json_list.append(dict(zip(headers, values)))
        return json.dumps(json_list, indent=2)
    except Exception as e:
        return f"Error: {e}"


async def convert_json_to_csv(json_data: str, **kwargs) -> str:
    try:
        data = json.loads(json_data) if isinstance(json_data, str) else json_data
        if isinstance(data, list) and data:
            headers = list(data[0].keys())
            lines = [','.join(headers)]
            for obj in data:
                lines.append(','.join(str(obj.get(h, '')) for h in headers))
            return '\n'.join(lines)
        return "Error: Need JSON array"
    except Exception as e:
        return f"Error: {e}"


async def yaml_tool(action: str, data: str, **kwargs) -> str:
    try:
        import yaml
        if action == "parse":
            parsed = yaml.safe_load(data) if isinstance(data, str) else data
            return json.dumps(parsed, indent=2)
        elif action == "create":
            if isinstance(data, str):
                data = json.loads(data)
            return yaml.dump(data, default_flow_style=False)
        return "Error: action must be 'parse' or 'create'"
    except ImportError:
        return "Error: PyYAML required. Run: pip install pyyaml"
    except Exception as e:
        return f"Error: {e}"


async def convert_timestamp(value: str, direction: str = "to_datetime", **kwargs) -> str:
    try:
        if direction == "to_datetime":
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        elif direction == "to_iso":
            dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            return dt.isoformat()
        return "Error: direction must be 'to_datetime' or 'to_iso'"
    except Exception as e:
        return f"Error: {e}"


async def timer(action: str = "start", **kwargs) -> str:
    global _timer_start
    if action == "start":
        _timer_start = time.time()
        return "Timer started"
    elif action == "stop":
        if _timer_start is not None:
            elapsed = time.time() - _timer_start
            _timer_start = None
            return f"Elapsed: {elapsed:.2f}s"
        return "No timer started"
    return "Error: action must be 'start' or 'stop'"


async def countdown(seconds: int, **kwargs) -> str:
    """Countdown timer. Waits specified seconds then returns."""
    try:
        seconds = int(seconds) if isinstance(seconds, (int, float, str)) else 5
    except (ValueError, TypeError):
        return "Error: seconds must be a number"
    
    for i in range(seconds, 0, -1):
        await asyncio.sleep(1)
    return f"Countdown complete ({seconds}s)"


async def generate_image(prompt: str, width: int = None, height: int = None, **kwargs) -> str:
    """Generate image from prompt using Pollinations."""
    try:
        config = load_config()
        safe_prompt = quote(prompt)
        # Handle string inputs from LLM
        if width is not None:
            try:
                width = int(width) if isinstance(width, (int, float, str)) else None
            except (ValueError, TypeError):
                width = None
        if height is not None:
            try:
                height = int(height) if isinstance(height, (int, float, str)) else None
            except (ValueError, TypeError):
                height = None
        
        actual_width = width if width is not None else config.pollinations.image_width
        actual_height = height if height is not None else config.pollinations.image_height
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width={actual_width}&height={actual_height}&nologo=true&enhance=true"
        return await download_and_open_image(url)
    except Exception as e:
        return f"Generation Error: {e}"


async def download_and_open_image(url: str, **kwargs) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return f"Security Error: URL scheme '{parsed.scheme}' is not allowed."
        filename = f"img_{int(time.time())}.jpg"
        path = TEMP_DIR / filename
        from src.core.provider import get_http_client
        client = get_http_client()
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            return f"Download Failed: {resp.status_code}"
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


async def take_screenshot(filename: str = "screen.png", **kwargs) -> str:
    try:
        import mss
        path = TEMP_DIR / filename
        with mss.mss() as sct:
            sct.shot(mon=-1, output=str(path))
        return str(path)
    except Exception as e:
        return f"Screenshot Error: {e}"


async def analyze_screen(question: str, **kwargs) -> str:
    try:
        import pyautogui
        path_str = await take_screenshot("vision_buffer.png")
        from src.providers.pollinations import PollinationsProvider
        provider = PollinationsProvider(load_config().pollinations)
        if not hasattr(provider, 'analyze_image'):
            return "Error: Current provider does not support Vision."
        width, height = pyautogui.size()
        result = await provider.analyze_image(f"{question}. Screen: {width}x{height}.", path_str)
        return f"Screen Analysis: {result}"
    except Exception as e:
        return f"Vision Error: {e}"


async def mouse_click(x: int, y: int, **kwargs) -> str:
    """Click at screen coordinates."""
    try:
        import pyautogui
        x = int(x) if isinstance(x, (int, float, str)) else 0
        y = int(y) if isinstance(y, (int, float, str)) else 0
        pyautogui.click(x=x, y=y)
        return f"Clicked at {x}, {y}"
    except Exception as e:
        return f"Click Error: {e}"


async def type_text(text: str, **kwargs) -> str:
    try:
        import pyautogui
        pyautogui.write(text, interval=0.05)
        return f"Typed: {text}"
    except Exception as e:
        return f"Type Error: {e}"


async def manage_todo(action: str, task: str = "", todo_id: str = "", **kwargs) -> str:
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
        if not todos:
            return "No TODOs."
        return "\n".join([f"{t['id']}. {'[x]' if t['done'] else '[ ]'} {t['task']}" for t in todos])
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


async def manage_history(action: str, index: int = -1, **kwargs) -> str:
    """Manage conversation history. Actions: delete_last, clear."""
    return f"SIGNAL_HISTORY_{action.upper()}_{index}"


async def response(text: str = "", **kwargs) -> str:
    return text or kwargs.get("content", "") or kwargs.get("message", "")


async def calc(expression: str, **kwargs) -> str:
    try:
        allowed = set('0123456789+-*/.()% ')
        if not all(c in allowed for c in expression): return "Error: Invalid characters."
        result = eval(expression)
        return str(result)
    except Exception as e: return f"Error: {e}"


async def scan_tools(**kwargs) -> str:
    return "\n".join([f"- {name}" for name in TOOL_REGISTRY.keys()])


async def find_skill(tags, agent=None, **kwargs) -> str:
    """Find skill by tags and inject tools into calling agent."""
    from src.skills_loader import skill_index
    
    if tags is None:
        return "Error: No tags provided"
    
    if isinstance(tags, str):
        tags = [t.strip().lower() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, (list, tuple)):
        tags = [str(t).strip().lower() for t in tags if t]
    else:
        tags = [str(tags).lower()]
    
    if not tags:
        return "Error: No valid tags provided"
    
    skill = skill_index.find_by_tags(tags)
    if not skill:
        return f"No skill found for tags: {tags}"
    
    # Extract tool names from skill context
    import re
    tool_pattern = r'(\w+)\s*\('
    mentioned_tools = set(re.findall(tool_pattern, skill.context))
    
    # Inject tools into agent if provided
    injected = []
    if agent and hasattr(agent, 'tools'):
        for tool_name in mentioned_tools:
            if tool_name in TOOL_REGISTRY and tool_name not in agent.tools:
                agent.tools[tool_name] = TOOL_REGISTRY[tool_name]
                injected.append(tool_name)
    
    result = {
        "skill": skill.name,
        "description": skill.description,
        "context": skill.context,
        "tools_injected": injected
    }
    return json.dumps(result, ensure_ascii=False)


async def add_memory(content: str, category: str = "general", **kwargs) -> str:
    """Store fact to Knowledge Graph and Vector Store."""
    from src.core.memory import memory_core
    return memory_core.add_memory(content, category)


async def search_memory(query: str, limit: int = 5, **kwargs) -> str:
    """Search Knowledge Graph with optional semantic search."""
    from src.core.memory import memory_core
    try:
        limit = int(limit) if isinstance(limit, (int, float, str)) else 5
        limit = max(1, min(20, limit))  # Clamp 1-20
    except (ValueError, TypeError):
        limit = 5
    results = memory_core.search_memory(query, limit)
    if not results:
        return "No memories found."
    return "\n".join(f"- {r.get('content', r)}" for r in results)


def _generate_registry():
    current_module = sys.modules[__name__]
    registry = {}
    for name, func in inspect.getmembers(current_module, inspect.isfunction):
        if not name.startswith("_") and func.__module__ == __name__ and name not in ('_generate_registry', 'get_tools_schema', 'download_and_open_image'):
            registry[name] = func
    return registry


def get_tools_schema() -> str:
    schema = []
    for name, func in TOOL_REGISTRY.items():
        sig = inspect.signature(func)
        params = str(sig).replace(" -> str", "").replace("**kwargs", "")
        doc = inspect.getdoc(func) or "Tool."
        schema.append(f"- {name}{params}: {doc}")
    return "\n".join(schema)


TOOL_REGISTRY = MappingProxyType(_generate_registry())

