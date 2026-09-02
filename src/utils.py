import asyncio
import functools
import hashlib
import os
import platform
import re
import secrets
import string
import subprocess as _subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Any
from rich.console import Console
import httpx
import json
import logging
import logging.handlers

from src.config import _REDACT_PATTERNS, CONTEXT_PRIORITY, CONTEXT_MAX_CHARS, _INJECT_RE, _TRACE_STYLE, load_config

console = Console()

def trace_line(role: str, event_type: str, content: str) -> None:
    """TUI-safe dim one-liner for live agent visibility."""
    if not load_config().trace_enabled:
        return
    from rich.markup import escape
    icon, style = _TRACE_STYLE.get(event_type, ("·", "dim"))
    snippet = escape(" ".join(content.split())[:90])
    console.print(
        f"[dim]{datetime.now():%H:%M:%S}[/dim] [{style}]{icon} {role}[/{style}] [dim]{snippet}[/dim]",
        soft_wrap=True,
    )

class TokenTracker:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._total = 0
            cls._instance._usage = {}
            cls._instance._lock = threading.Lock()
        return cls._instance

    def get_total(self) -> int:
        return self._total

    def add_tokens(self, tokens: int) -> int:
        with self._lock:
            self._total += tokens
            return self._total

    def get_provider(self, provider: str) -> Dict[str, int]:
        return self._usage.get(provider, {"input": 0, "output": 0})

    def add_provider(self, provider: str, input_tokens: int, output_tokens: int) -> Dict[str, int]:
        with self._lock:
            if provider not in self._usage:
                self._usage[provider] = {"input": 0, "output": 0}
            self._usage[provider]["input"] += input_tokens
            self._usage[provider]["output"] += output_tokens
            return self._usage[provider]

    def get_all(self) -> Dict[str, Dict[str, int]]:
        return dict(self._usage)

    def reset_total(self) -> None:
        with self._lock:
            self._total = 0

    def reset_provider(self, provider: Optional[str] = None) -> None:
        with self._lock:
            if provider:
                self._usage.pop(provider, None)
            else:
                self._usage.clear()


_tracker = TokenTracker()

get_global_tokens = _tracker.get_total
add_global_tokens = _tracker.add_tokens
get_provider_tokens = _tracker.get_provider
add_provider_tokens = _tracker.add_provider
get_all_provider_tokens = _tracker.get_all
reset_global_tokens = _tracker.reset_total
reset_provider_tokens = _tracker.reset_provider


def count_tokens(text_or_history, response: str = "") -> tuple:
    if isinstance(text_or_history, list):
        input_tokens = estimate_messages_tokens(text_or_history)
    else:
        input_text = str(text_or_history)
        input_tokens = count_tokens_tiktoken(input_text)

    output_tokens = count_tokens_tiktoken(response) if response else 0
    return input_tokens, output_tokens


def async_retry(retries=5, delays=(2, 5, 10, 25, 60, 120)):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for i in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "rate limit" in err_str.lower() or "CircuitBreaker" in type(e).__name__:
                        raise e
                    if i == retries:
                        msg = err_str or f"{type(e).__name__} (no message)"
                        raise Exception(f"Failed after {retries+1} attempts: {msg}") from e
                    wait_time = delays[i] if i < len(delays) else delays[-1]
                    console.print(f"[yellow]Retry {i+1}/{retries} in {wait_time}s...[/yellow]")
                    logging.getLogger(__name__).warning(f"RETRY {i+1}/{retries} {func.__name__} in {wait_time}s: {err_str[:150]}")
                    await asyncio.sleep(wait_time)
        return wrapper
    return decorator

def get_system_context() -> str:
    now = datetime.now()
    tz = now.astimezone().tzname() or "UTC"
    shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "Unknown"))
    branch = "none"
    try:
        branch = _subprocess.check_output(
            ["git", "branch", "--show-current"],
            stderr=_subprocess.DEVNULL, text=True, timeout=2,
        ).strip() or "none"
    except Exception:
        pass
    return (f"[Time: {now:%Y-%m-%d %H:%M} {tz}] [OS: {platform.system()} {platform.release()}] [Shell: {shell}] [Project: {Path.cwd()}] [Git: {branch}]")

def redact_fast(text: str) -> str:
    if not text or len(text) < 20:
        return text
    for pat, repl in _REDACT_PATTERNS:
        text = re.sub(pat, repl, text)
    return text


def discover_context_files(cwd: str = ".") -> str:
    base = Path(cwd).resolve()
    search = [base]
    cur = base.parent
    while cur != cur.parent and cur.exists():
        if (cur / ".git").exists():
            search.insert(0, cur)
            break
        search.append(cur)
        cur = cur.parent
    for sp in search:
        for name in CONTEXT_PRIORITY:
            t = sp / name
            if not t.exists():
                continue
            try:
                raw = t.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                logging.getLogger(__name__).warning(f"discover_context_files: cannot read {t}: {e}")
                continue
            if any(p.search(raw) for p in _INJECT_RE):
                return f"[BLOCKED] {t} contains injection patterns. Skipped for safety."
            if raw.lstrip().startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    raw = parts[2]
            if len(raw) > CONTEXT_MAX_CHARS:
                h, tl = int(CONTEXT_MAX_CHARS * 0.7), int(CONTEXT_MAX_CHARS * 0.2)
                raw = raw[:h] + "\n\n...[truncated]...\n\n" + raw[-tl:]
            return f"--- PROJECT RULES ({name}) ---\n{raw}\n"
    logging.getLogger(__name__).info("discover_context_files: no project context files found")
    return ""


def count_tokens_tiktoken(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        return int(len(text.split()) * 1.3)


def estimate_messages_tokens(messages: List[Dict]) -> int:
    total = 4
    for m in messages:
        total += 4
        for v in m.values():
            total += count_tokens_tiktoken(str(v))
    return total


def generate_random_delimiter(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def sanitize_for_prompt(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    replacements = [
        (r'===', '\\==='), (r'---', '\\---'), (r'```', '\\`\\`\\`'),
        (r'<\|', '\\<\\|'), (r'\|>', '\\|\\>'),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


class ContextCompressor:
    def __init__(self, provider):
        self.provider = provider
        self._prev = None

    async def compress(self, msgs, keep_tokens: int = 20000):
        head = [m for m in msgs if m.get("role") == "system"]
        body = msgs[len(head):]
        tail, acc = [], 0
        for m in reversed(body):
            acc += int(len(str(m.get("content", ""))) / 4) + 4
            if acc > keep_tokens and tail:
                break
            tail.insert(0, m)
        middle = body[:len(body) - len(tail)]
        if len(middle) < 4:
            return msgs
        transcript = "\n".join(f"[{m.get('role','?')}]: {m.get('content','')[:500]}" for m in middle)
        prompt = (f"Update previous summary with new messages.\n\nPrevious:\n{self._prev}\n\nNew:\n{transcript}" if self._prev
                  else f"Summarize this conversation segment in 2-4 sentences. Preserve facts, decisions, and tool results. Do not add new information.\n\n{transcript}")
        sys_prompt = "You are a precise summarizer. Output ONLY the summary text. No preamble, no commentary. Preserve key facts and decisions exactly. Never invent information."
        try:
            r = await self.provider.generate_text([{"role":"user","content":prompt}], sys_prompt)
            s = (r.get("content","") if isinstance(r,dict) else str(r)).strip()
            self._prev = s[:4000]
        except Exception as e:
            logging.getLogger(__name__).error(f"[{e} and {len(middle)} messages trimmed]")
            return head + tail


class ModelCatalog:
    CACHE = Path("tmp") / "models_dev.json"
    URL = "https://models.dev/api.json"
    TTL = 3600
    _data = {}
    _time = 0

    @classmethod
    def _fetch(cls, force=False):
        if not force and cls._data and (time.time() - cls._time) < cls.TTL:
            return cls._data
        try:
            r = httpx.get(cls.URL, timeout=5)
            r.raise_for_status()
            d = r.json()
            if d:
                cls._data = d
                cls._time = time.time()
                cls.CACHE.parent.mkdir(parents=True, exist_ok=True)
                cls.CACHE.write_text(json.dumps(d, separators=(",", ":")))
                return d
        except Exception as e:
            logging.getLogger(__name__).warning(f"ModelCatalog._fetch network error: {e}")
        if not cls._data and cls.CACHE.exists():
            try:
                cls._data = json.loads(cls.CACHE.read_text())
                cls._time = time.time() - cls.TTL + 300
            except: pass
        return cls._data

    @classmethod
    def list_models(cls, provider: str) -> list[str]:
        d = cls._fetch()
        pid = {"gemini": "google"}.get(provider, provider)
        m = d.get(pid, {}).get("models", {})
        return list(m.keys()) if isinstance(m, dict) else []

    @classmethod
    def get_info(cls, provider: str, model_id: str) -> dict | None:
        d = cls._fetch()
        pid = {"gemini": "google"}.get(provider, provider)
        models = d.get(pid, {}).get("models", {})
        if not isinstance(models, dict): return None
        e = models.get(model_id)
        if e: return e
        mi = model_id.lower()
        for mid, md in models.items():
            if mid.lower() == mi:
                return md
        return None

    @classmethod
    def get_context(cls, provider: str, model_id: str) -> int | None:
        i = cls.get_info(provider, model_id)
        if i:
            c = i.get("limit", {}).get("context")
            if isinstance(c, (int, float)) and c > 0:
                return int(c)
        return None


def safe_run(cmd: list, cwd=None, timeout=30, **kwargs):
    merged = dict(capture_output=True, text=True, timeout=timeout)
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        si = _subprocess.STARTUPINFO()
        si.dwFlags |= _subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        merged.update(creationflags=CREATE_NO_WINDOW, startupinfo=si)
    merged.update(kwargs)
    return _subprocess.run(cmd, cwd=cwd, **merged)

_logging_configured = False

def setup_logging(log_dir: Path = Path("tmp"), level: int = logging.INFO) -> None:
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            return
    root.setLevel(logging.DEBUG)
    afh = logging.handlers.RotatingFileHandler(
        log_dir / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    afh.setLevel(level)
    afh.setFormatter(fmt)
    root.addHandler(afh)
    efh = logging.handlers.RotatingFileHandler(
        log_dir / "errors.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    efh.setLevel(logging.WARNING)
    efh.setFormatter(fmt)
    root.addHandler(efh)


class GitManager:
    """Shadow git for BUILD-mode rollback. Uses GIT_WORK_TREE for zero-copy snapshots."""
    def __init__(self, cwd: Path, max_snapshots: int = 30, max_total_mb: int = 500):
        self.cwd = cwd.resolve()
        self.max_snapshots = max_snapshots
        self.max_total_mb = max_total_mb
        salt = hashlib.sha256(str(self.cwd).encode()).hexdigest()[:16]
        self.repo_dir = Path.home() / ".zervgen" / "checkpoints" / salt
        self._ensure_repo()
        self._ignore = {'.git','tmp','node_modules','__pycache__','.venv','venv','.env'}

    def _ensure_repo(self):
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if not (self.repo_dir / ".git").exists():
            safe_run(["git", "init"], cwd=self.repo_dir, check=True)
            safe_run(["git", "config", "user.email", "zervgen@local"], cwd=self.repo_dir, check=True)
            safe_run(["git", "config", "user.name", "ZervGen"], cwd=self.repo_dir, check=True)
            exclude = self.repo_dir / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            patterns = ["/tmp/", "/node_modules/", "/__pycache__/", "/.venv/", "/venv/", "/.env"]
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            missing = [p for p in patterns if p not in current.splitlines()]
            if missing:
                exclude.write_text(current.rstrip() + "\n" + "\n".join(missing) + "\n", encoding="utf-8")

    def _git_env(self):
        return {**os.environ, "GIT_DIR": str(self.repo_dir / ".git"), "GIT_WORK_TREE": str(self.cwd)}

    def _repo_mb(self) -> float:
        total = sum(f.stat().st_size for f in self.repo_dir.rglob("*") if f.is_file())
        return total / (1024 * 1024)

    def snapshot(self, session_id: str, state: Optional[dict] = None) -> str:
        env = self._git_env()
        safe_run(["git", "add", "-A"], cwd=self.repo_dir, env=env)
        for path in ("tmp", "node_modules", "__pycache__", ".venv", "venv", ".env"):
            safe_run(["git", "reset", "--", path], cwd=self.repo_dir, env=env)
        r = safe_run(["git", "diff", "--cached", "--quiet"], cwd=self.repo_dir, env=env)
        if r.returncode == 0:
            return None
        r = safe_run(["git", "commit", "-m", f"turn {session_id}"], cwd=self.repo_dir, env=env, text=True)
        sha = None
        if r.returncode == 0:
            rev = safe_run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, env=env, text=True)
            sha = rev.stdout.strip() if rev.returncode == 0 else None
        if state is not None:
            state_dir = self.repo_dir / "run_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            key = sha or "HEAD"
            state_dir.joinpath(f"{key}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        self._prune()
        return sha

    def checkpoint(self, run_id: str, state: Optional[dict] = None) -> Optional[str]:
        revision = self.snapshot(run_id, state=None)
        checkpoint_id = f"{run_id}_{int(time.time() * 1000)}"
        state_dir = self.repo_dir / "run_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"id": checkpoint_id, "revision": revision or self.latest_revision(), "state": state or {}}
        state_dir.joinpath(f"{checkpoint_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return checkpoint_id

    def load_state(self, checkpoint_id: str) -> Optional[dict]:
        path = self.repo_dir / "run_state" / f"{checkpoint_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def latest_revision(self) -> Optional[str]:
        env = self._git_env()
        r = safe_run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, env=env, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def restore(self, revision: str) -> bool:
        if not revision:
            return False
        env = self._git_env()
        r = safe_run(["git", "checkout", revision, "--", "."], cwd=self.repo_dir, env=env, text=True)
        return r.returncode == 0
    
    def checkpoints(self, limit: int = 10) -> List[dict]:
        state_dir = self.repo_dir / "run_state"
        files = sorted(state_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if state_dir.exists() else []
        out = []
        for path in files[:max(1, limit)]:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def restore_checkpoint(self, checkpoint_id: str = "") -> Optional[dict]:
        state = self.load_state(checkpoint_id) if checkpoint_id else (self.checkpoints(1) or [None])[0]
        if not state:
            return None
        revision = state.get("revision")
        if revision and not self.restore(revision):
            return None
        return state
    
    def _prune(self):
        env = self._git_env()
        r = safe_run(["git", "log", "--oneline"], cwd=self.repo_dir, env=env)
        lines = [l for l in r.stdout.split('\n') if l.strip()]
        if len(lines) > self.max_snapshots:
            keep = lines[self.max_snapshots - 1].split()[0]
            safe_run(["git", "reset", "--hard", keep], cwd=self.repo_dir, env=env)
            safe_run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=self.repo_dir, env=env)
            safe_run(["git", "gc", "--prune=now", "--aggressive"], cwd=self.repo_dir, env=env)
            lines = lines[:self.max_snapshots]
        if self.max_total_mb > 0:
            while self._repo_mb() > self.max_total_mb and len(lines) > 1:
                safe_run(["git", "reset", "--hard", "HEAD~1"], cwd=self.repo_dir, env=env)
                safe_run(["git", "gc", "--prune=now"], cwd=self.repo_dir, env=env)
                lines = lines[1:]
                state_dir = self.repo_dir / "run_state"
                if state_dir.exists():
                    files = sorted(state_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                    for path in files[self.max_snapshots:]:
                        try:
                            path.unlink()
                        except OSError:
                            pass

    def rollback(self, steps: int = 1) -> str:
        env = self._git_env()
        r = safe_run(["git","log","--oneline",f"-{steps+1}"], cwd=self.repo_dir, env=env, text=True)
        shas = [l.split()[0] for l in r.stdout.strip().split('\n') if l.strip()]
        if len(shas) <= steps: return None
        target = shas[steps]
        safe_run(["git","checkout",target,"--","."], cwd=self.repo_dir, env=env)
        return target

    def history_depth(self) -> int:
        env = self._git_env()
        r = safe_run(["git","log","--oneline"], cwd=self.repo_dir, env=env, text=True)
        return len([l for l in r.stdout.split('\n') if l.strip()])