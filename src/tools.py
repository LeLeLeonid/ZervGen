import os
import subprocess
import platform
import httpx
import requests
import time
import sys
import inspect
import re
import shlex
import asyncio
from types import MappingProxyType
from typing import List
from bs4 import BeautifulSoup
from pathlib import Path
from urllib.parse import quote, urlparse
from ddgs import DDGS
from fake_useragent import UserAgent
from src.core.memory import memory_core
from src.utils import extract_json_from_text

# Command whitelist for execute_command security
ALLOWED_COMMANDS = {
    'git', 'python', 'pip', 'npm', 'pytest', 'ls', 'dir', 'cat', 'type',
    'mkdir', 'cd', 'pwd', 'echo', 'find', 'grep', 'curl', 'wget',
    'terraform', 'docker', 'kubectl', 'helm', 'npm', 'node', 'npx',
    'cargo', 'rustc', 'go', 'javac', 'java'
}

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
    from src.providers.openai import OpenAIProvider
    from src.providers.anthropic import AnthropicProvider
    from src.providers.groq import GroqProvider
    config = load_config()
    try:
        if config.provider == "gemini" and config.gemini.api_key: return GeminiProvider(config.gemini)
        elif config.provider == "openrouter" and config.openrouter.api_key: return OpenRouterProvider(config.openrouter)
        elif config.provider == "openai" and config.openai.api_key: return OpenAIProvider(config.openai)
        elif config.provider == "anthropic" and config.anthropic.api_key: return AnthropicProvider(config.anthropic)
        elif config.provider == "groq" and config.groq.api_key: return GroqProvider(config.groq)
        else: return PollinationsProvider(config.pollinations)
    except Exception as e:
        print(f"[Provider] Failed to load {config.provider}: {e}. Falling back to Pollinations.")
        return PollinationsProvider(config.pollinations)

async def download_and_open_image(url: str, **kwargs) -> str:
    """Download and open image with URL validation and non-blocking I/O."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return f"Security Error: URL scheme '{parsed.scheme}' is not allowed. Only http:// and https:// are permitted."

        filename = f"img_{int(time.time())}.jpg"
        path = TEMP_DIR / filename

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, follow_redirects=True, timeout=30)
            if resp.status_code != 200:
                return f"Download Failed: {resp.status_code}"

            import aiofiles
            async with aiofiles.open(path, "wb") as f:
                await f.write(resp.content)

        loop = asyncio.get_event_loop()
        if platform.system() == "Windows":
            await loop.run_in_executor(None, os.startfile, str(path))
        elif platform.system() == "Darwin":
            proc = await asyncio.create_subprocess_exec(
                "open", str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.wait()
        else:
            proc = await asyncio.create_subprocess_exec(
                "xdg-open", str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.wait()

        return str(path)
    except Exception as e:
        return f"Image Download Error: {e}"

async def generate_image(prompt: str, width: int = None, height: int = None, **kwargs) -> str:
    try:
        from src.config import load_config
        config = load_config()
        safe_prompt = quote(prompt)
        actual_width = width if width is not None else config.pollinations.image_width
        actual_height = height if height is not None else config.pollinations.image_height
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width={actual_width}&height={actual_height}&nologo=true&enhance=true"
        return await download_and_open_image(url)
    except Exception as e: return f"Generation Error: {e}"

async def web_search(query: str, **kwargs) -> str:
    """Search the web using DuckDuckGo (runs in thread executor to prevent blocking)."""
    try:
        if not query or not isinstance(query, str):
            return "Error: Invalid query."

        loop = asyncio.get_event_loop()
        def _search():
            return DDGS().text(query, max_results=5)

        results = await asyncio.wait_for(
            loop.run_in_executor(None, _search),
            timeout=30
        )

        if not results:
            return "No results found."
        return "\n".join([f"- {r['title']}: {r['href']}\n  Snippet: {r['body']}" for r in results])
    except asyncio.TimeoutError:
        return "Search Error: Request timed out."
    except Exception as e:
        return f"Search Error: {e}"

async def visit_page(url: str, **kwargs) -> str:
    """Visit a webpage and extract text content (runs parsing in thread executor)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return f"Security Error: URL scheme '{parsed.scheme}' is not allowed."

        ua = UserAgent()
        headers = {"User-Agent": ua.random, "Accept": "text/html"}

        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 403:
                return f"Error 403: Access Denied."
            resp.raise_for_status()
            html = resp.text

        def _parse_html(content):
            soup = BeautifulSoup(content, 'html.parser')
            for s in soup(["script", "style", "nav", "footer", "header", "form", "svg"]):
                s.decompose()
            text = ' '.join(soup.get_text(separator=' ', strip=True).split())
            return text[:14000] + ("..." if len(text) > 14000 else "")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _parse_html, html)
        return result

    except Exception as e:
        return f"Browsing Error: {e}"

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

async def read_files(paths: str, offset: int = 0, limit: int = 0, **kwargs) -> str:
    """Reads files with optional line range. Usage: 'file.py' or 'file.py' with offset=100, limit=50."""
    import aiofiles
    try:
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

                async with aiofiles.open(p, "r", encoding="utf-8") as f:
                    lines = await f.readlines()

                total_lines = len(lines)
                start = offset
                end = offset + limit if limit > 0 else total_lines

                if offset > 0 or limit > 0:
                    header = f"### {p} (lines {start+1}-{min(end, total_lines)} of {total_lines})\n"
                    content_lines = lines[start:end]
                    content = "".join(content_lines)
                    if end < total_lines:
                        content += f"\n... [{total_lines - end} more lines, use offset={end} to continue] ..."
                else:
                    header = f"### {p}\n"
                    content = "".join(lines)
                    if len(content) > 50000:
                        content = content[:50000] + f"\n... [TRUNCATED at 50KB, use offset and limit params to read more] ..."

                results.append(header + content)
            except Exception as e:
                results.append(f"### {p}\n[READ ERROR: {e}]")

        return "\n\n".join(results)
    except Exception as e:
        return f"Read Files Error: {e}"

async def grep_files(pattern: str, path: str = ".", **kwargs) -> str:
    """Search files for pattern using non-blocking I/O."""
    import aiofiles
    try:
        if not _is_safe_path(path):
            return "Access Denied."

        results = []
        root_path = Path(path)
        ignore_dirs = {'.git', '__pycache__', 'venv', 'node_modules', 'tmp'}
        loop = asyncio.get_event_loop()

        def _walk_files():
            file_list = []
            for root, dirs, files in os.walk(root_path):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]
                for file in files:
                    if not file.endswith(('.py', '.md', '.json', '.txt')):
                        continue
                    file_list.append(Path(root) / file)
            return file_list

        file_list = await loop.run_in_executor(None, _walk_files)

        for file_path in file_list:
            try:
                async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    for i, line in enumerate(content.splitlines(), 1):
                        if pattern in line:
                            results.append(f"{file_path}:{i}: {line.strip()}")

                if len(results) > 100:
                    results.append("... [Too many matches]")
                    return "\n".join(results)
            except Exception:
                pass

        return "\n".join(results) if results else "No matches found."
    except Exception as e:
        return f"Grep Error: {e}"

async def write_file(path: str, content: str, **kwargs) -> str:
    """Write file with non-blocking I/O and path validation."""
    import aiofiles
    try:
        if not _is_safe_path(path):
            return "Security Error: Path outside project."

        old_content = ""
        if os.path.exists(path):
            async with aiofiles.open(path, "r", encoding="utf-8", errors="ignore") as f:
                old_content = await f.read()

        if old_content != content:
            import difflib
            diff = difflib.unified_diff(
                old_content.splitlines(),
                content.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm=""
            )
            diff_text = "\n".join(list(diff))

            if diff_text:
                from rich.console import Console
                from rich.syntax import Syntax
                from rich.panel import Panel
                syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
                Console().print(Panel(syntax, title=f"[bold yellow]CHANGES: {path}[/bold yellow]", border_style="yellow"))

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        )

        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(content)

        return f"File written: {path}"
    except Exception as e:
        return f"Write Error: {e}"

async def append_file(path: str, content: str, **kwargs) -> str:
    """Append content to file with non-blocking I/O and path validation."""
    import aiofiles
    try:
        if not _is_safe_path(path):
            return "Security Error: Cannot write outside project directory."

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        )

        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            await f.write(content)
        return f"Appended to file: {path}"
    except Exception as e:
        return f"Append Error: {e}"

async def execute_command(command: str, **kwargs) -> str:
    """
    Execute a command safely with optional whitelist validation and shell=False.
    Validation only occurs when require_approval is True in config.
    """
    try:
        if not command or not isinstance(command, str):
            return "Error: Invalid command."

        try:
            cmd_parts = shlex.split(command)
        except ValueError as e:
            return f"Error: Invalid command syntax: {e}"

        if not cmd_parts:
            return "Error: Empty command."

        base_cmd = cmd_parts[0]
        base_cmd_name = os.path.basename(base_cmd).lower()

        # Only validate if require_approval is True
        from src.config import load_config
        config = load_config()
        if config.require_approval:
            if base_cmd_name not in ALLOWED_COMMANDS:
                return f"Security Error: Command '{base_cmd_name}' is not in the allowed command whitelist."

            dangerous_patterns = [';', '&&', '||', '`', '$(', '${', '|', '>', '<', '&']
            for pattern in dangerous_patterns:
                if pattern in command and pattern not in ['>', '<']:
                    return f"Security Error: Shell operators are not allowed."

        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=None
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=120
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "Error: Command timed out after 120 seconds."

        output = stdout.decode('utf-8', errors='replace') if stdout else ""
        if stderr:
            output += f"\nSTDERR:\n{stderr.decode('utf-8', errors='replace')}"

        return output.strip() or f"Executed successfully (exit code: {proc.returncode})."

    except Exception as e:
        return f"Execution Error: {e}"

async def list_dir(path: str = ".", **kwargs) -> str:
    """List directory contents with path traversal protection."""
    try:
        if not _is_safe_path(path):
            return "Security Error: Access denied - path outside project directory."
        return "\n".join(os.listdir(path))
    except Exception as e:
        return f"List Dir Error: {e}"

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

# Global delegation chain tracker to prevent loops
_DELEGATION_CHAIN = []
_MAX_DELEGATION_DEPTH = 5

async def delegate_to(agent_name: str, task: str, context: str = "", **kwargs) -> str:
    """
    Delegates to a specialized agent with full context passing.
    """
    try:
        from src.core.base_agent import BaseAgent
        from src.config import load_config
        from src.skills_loader import load_role
        
        config = load_config()
        provider = _get_active_provider()
        
        # Normalize agent name
        agent_name = agent_name.lower().strip()
        
        # Check delegation depth to prevent infinite loops
        current_chain = getattr(delegate_to, '_chain', [])
        if len(current_chain) >= _MAX_DELEGATION_DEPTH:
            return f"Delegation Error: Maximum delegation depth ({_MAX_DELEGATION_DEPTH}) reached. Chain: {' -> '.join(current_chain)}"
        
        # Check for circular delegation
        if agent_name in current_chain:
            return f"Delegation Error: Circular delegation detected. {agent_name} is already in chain: {' -> '.join(current_chain)}"
        
        # Load skill configuration for the target agent
        role_config = load_role(agent_name)
        if not role_config:
            return f"Delegation Error: Unknown agent '{agent_name}'. Available: code, researcher, architect, system, n8n_expert, memory_manager"
        
        # Create agent with proper configuration
        agent = BaseAgent(
            name=role_config.name.capitalize(), 
            provider=provider, 
            skill_name=agent_name, 
            settings=config
        )
        
        # Set system prompt from skill
        agent.system_prompt = role_config.prompt
        
        # Load appropriate tools for this agent
        if role_config.tools:
            agent.load_tools(role_config.tools)
        else:
            agent.tools = dict(TOOL_REGISTRY)
        
        # Build enhanced task with context
        enhanced_task = task
        if context:
            enhanced_task = f"""[DELEGATED TASK]
Original Task: {task}

[CONTEXT FROM DELEGATOR]
{context}

[DELEGATION CHAIN]
{' -> '.join(current_chain)} -> {agent_name}

Please complete this task and return results to the original requester."""
        
        # Track delegation
        new_chain = current_chain + [agent_name]
        original_run = agent.run
        
        async def tracked_run(task):
            """Wrapper to track delegation chain in recursive calls"""
            old_chain = getattr(delegate_to, '_chain', [])
            delegate_to._chain = new_chain
            try:
                result = await original_run(task)
                return result
            finally:
                delegate_to._chain = old_chain
        
        agent.run = tracked_run
        
        # Execute the task
        memory_core.log_event(f"delegate:{agent_name}", f"Task delegated: {task[:100]}...", "delegation_start")
        
        result = await agent.run(enhanced_task)
        
        memory_core.log_event(f"delegate:{agent_name}", f"Delegation complete: {result[:100]}...", "delegation_end")
        
        return f"[DELEGATION RESULT from {agent_name}]:\n{result}"
        
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        memory_core.log_event(f"delegate:{agent_name}", f"Delegation failed: {str(e)}", "delegation_error")
        return f"Delegation Error: {e}\n{error_detail}"

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

async def take_screenshot(filename: str = "screen.png", **kwargs) -> str:
    """Takes a screenshot and saves it to tmp/."""
    try:
        import mss
        path = TEMP_DIR / filename
        with mss.mss() as sct:
            sct.shot(mon=-1, output=str(path))
        return str(path)
    except Exception as e:
        raise RuntimeError(f"Screenshot Error: {e}")

async def analyze_screen(question: str, **kwargs) -> str:
    """Takes a screenshot and asks the Vision Model about it."""
    import pyautogui
    path_str = None
    try:
        path_str = await take_screenshot("vision_buffer.png")
        provider = _get_active_provider()
        
        if not hasattr(provider, 'analyze_image'):
             return "Error: Current provider does not support Vision."

        width, height = pyautogui.size()
        
        full_prompt = (
            f"{question}. The current screen resolution is {width}x{height}. "
            f"If referring to UI elements, provide approximate coordinates [x, y] "
            f"based on this {width}x{height} grid."
        )

        result = await provider.analyze_image(full_prompt, path_str)
        return f"Screen Analysis: {result}"
    except Exception as e:
        return f"Vision Error: {e}"
    finally:
        if path_str and os.path.exists(path_str):
            try:
                os.remove(path_str)
            except:
                pass

async def mouse_click(x: int, y: int, **kwargs) -> str:
    """Moves mouse to x,y and clicks."""
    try:
        import pyautogui
        pyautogui.click(x=int(x), y=int(y))
        return f"Clicked at {x}, {y}"
    except Exception as e:
        return f"Click Error: {e}"

async def type_text(text: str, **kwargs) -> str:
    """Types text at current cursor position."""
    try:
        import pyautogui
        pyautogui.write(text, interval=0.05)
        return f"Typed: {text}"
    except Exception as e:
        return f"Type Error: {e}"
    
# Whitelist of allowed imports for run_safe_code
SAFE_CODE_IMPORTS = {
    'math', 'random', 'datetime', 'json', 're', 'collections',
    'itertools', 'functools', 'statistics', 'decimal', 'fractions',
    'typing', 'hashlib', 'string', 'time', 'inspect', 'textwrap',
    'copy', 'pprint', 'enum', 'dataclasses', 'pathlib', 'uuid'
}

async def run_safe_code(code: str, **kwargs) -> str:
    """
    Executes Python code inside a restricted sandbox.
    Uses multiprocessing for isolation with import whitelist.
    """
    import multiprocessing
    import tempfile
    import io
    import contextlib

    import ast
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split('.')[0]
                    if module not in SAFE_CODE_IMPORTS:
                        return f"Security Error: Import '{module}' is not in the allowed whitelist."
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    module = node.module.split('.')[0]
                    if module not in SAFE_CODE_IMPORTS:
                        return f"Security Error: Import from '{module}' is not in the allowed whitelist."
    except SyntaxError as e:
        return f"Syntax Error: {e}"

    def _execute_in_process(code_str, return_dict):
        try:
            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()

            safe_globals = {
                '__builtins__': {
                    'print': print,
                    'len': len,
                    'range': range,
                    'enumerate': enumerate,
                    'zip': zip,
                    'map': map,
                    'filter': filter,
                    'sum': sum,
                    'min': min,
                    'max': max,
                    'abs': abs,
                    'round': round,
                    'pow': pow,
                    'divmod': divmod,
                    'chr': chr,
                    'ord': ord,
                    'bin': bin,
                    'hex': hex,
                    'oct': oct,
                    'format': format,
                    'repr': repr,
                    'str': str,
                    'int': int,
                    'float': float,
                    'bool': bool,
                    'list': list,
                    'dict': dict,
                    'tuple': tuple,
                    'set': set,
                    'frozenset': frozenset,
                    'sorted': sorted,
                    'reversed': reversed,
                    'hasattr': hasattr,
                    'getattr': getattr,
                    'isinstance': isinstance,
                    'type': type,
                    'Exception': Exception,
                    'ValueError': ValueError,
                    'TypeError': TypeError,
                    'KeyError': KeyError,
                    'IndexError': IndexError,
                    'AttributeError': AttributeError,
                    'StopIteration': StopIteration,
                    'RuntimeError': RuntimeError,
                }
            }

            import importlib
            for module_name in SAFE_CODE_IMPORTS:
                try:
                    module = importlib.import_module(module_name)
                    safe_globals[module_name] = module
                except ImportError:
                    pass

            with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
                exec(code_str, safe_globals, {})

            return_dict['stdout'] = stdout_capture.getvalue()
            return_dict['stderr'] = stderr_capture.getvalue()
            return_dict['success'] = True
        except Exception as e:
            return_dict['error'] = str(e)
            return_dict['success'] = False

    try:
        manager = multiprocessing.Manager()
        return_dict = manager.dict()

        process = multiprocessing.Process(
            target=_execute_in_process,
            args=(code, return_dict)
        )
        process.start()
        process.join(timeout=30)

        if process.is_alive():
            process.terminate()
            process.join()
            return "Error: Code execution timed out (30s limit)."

        if process.exitcode != 0:
            return f"Execution Error: Process exited with code {process.exitcode}"

        if return_dict.get('success'):
            output = return_dict.get('stdout', '')
            stderr = return_dict.get('stderr', '')
            if stderr:
                output += f"\nSTDERR:\n{stderr}"
            return output.strip() or "Success (No Output)"
        else:
            return f"Execution Error: {return_dict.get('error', 'Unknown error')}"

    except Exception as e:
        return f"Run Code Error: {e}"

async def get_code_skeleton(path: str, **kwargs) -> str:
    """Reads a Python file and returns ONLY the structure."""
    import ast
    import aiofiles
    try:
        if not _is_safe_path(path):
            return "Security Error: Access denied - path outside project directory."

        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            source = await f.read()

        tree = ast.parse(source)
        skeleton = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                skeleton.append(f"\n[Line {node.lineno}] class {node.name}:")
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        args = [a.arg for a in item.args.args]
                        skeleton.append(f"    [Line {item.lineno}] def {item.name}({', '.join(args)}): ...")
            elif isinstance(node, ast.FunctionDef):
                if not isinstance(getattr(node, "parent", None), ast.ClassDef):
                    if node.col_offset == 0:
                        args = [a.arg for a in node.args.args]
                        skeleton.append(f"[Line {node.lineno}] def {node.name}({', '.join(args)}): ...")
        return "\n".join(skeleton) if skeleton else "No structure found (script or empty)."
    except Exception as e:
        return f"Skeleton Error: {e}"

async def manage_todo(action: str, task: str = "", todo_id: str = "", **kwargs) -> str:
    """TODO manager. Actions: add, list, remove, clear."""
    import json
    todo_file = Path("tmp/todos.json")
    
    # Load existing
    todos = []
    if todo_file.exists():
        try:
            todos = json.loads(todo_file.read_text())
        except:
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
    """
    Allows the agent to delete useless messages from its own context window.
    Action: 'delete_last', 'delete_index'.
    """
    return f"SIGNAL_HISTORY_{action.upper()}_{index}"

def _generate_registry():
    current_module = sys.modules[__name__]
    registry = {}
    for name, func in inspect.getmembers(current_module, inspect.isfunction):
        if not name.startswith("_") and func.__module__ == __name__ and name != "get_tools_schema":
            registry[name] = func
    return registry

TOOL_REGISTRY = MappingProxyType(_generate_registry())

def get_tools_schema() -> str:
    schema = []
    for name, func in TOOL_REGISTRY.items():
        sig = inspect.signature(func)
        params = str(sig).replace(" -> str", "")
        doc = inspect.getdoc(func) or "Tool."
        schema.append(f"- {name}{params}: {doc}")
    return "\n".join(schema)