"""MCP client: stdio, streamable HTTP and SSE servers behind one tool namespace."""

import asyncio
import fnmatch
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.config import GlobalSettings, MCPServerConfig, load_config

logger = logging.getLogger(__name__)

MCP_SDK_AVAILABLE = False
MCP_MODERN_CLIENT = None
_HTTP_CLIENT = None
try:
    from mcp import Client, ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    MCP_SDK_AVAILABLE = True
    MCP_MODERN_CLIENT = Client
    try:
        import httpx2 as _HTTP_CLIENT
    except ImportError:
        import httpx as _HTTP_CLIENT
except ImportError:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        MCP_SDK_AVAILABLE = True
    except ImportError:
        pass

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_MAX_TOOL_NAME = 64
BACKOFF_START = 30.0
BACKOFF_MAX = 600.0
DEAD_AFTER = 5

MCP_STORE = Path("mcp.json")
MCP_HOME_STORE = Path.home() / ".zervgen" / "mcp.json"


def store_paths() -> List[Path]:
    """User file first, project file last; the project wins on a name clash."""
    return [path for path in (MCP_HOME_STORE, MCP_STORE) if path.exists()]


@dataclass(frozen=True)
class CatalogEntry:
    """One installable server: transport plus the credentials it needs."""

    name: str
    description: str
    command: str = ""
    args: Tuple[str, ...] = ()
    url: str = ""
    env: Tuple[Tuple[str, str], ...] = ()

    @property
    def kind(self) -> str:
        return "stdio" if self.command else "http"

    def to_config(self, secrets: Optional[Dict[str, str]] = None) -> MCPServerConfig:
        """Catalog entry as a config object; secrets stay as ``${VAR}`` refs."""
        env = {key: f"${{{key}}}" for key, _ in self.env if (secrets or {}).get(key)}
        return MCPServerConfig(
            command=self.command,
            args=list(self.args),
            url=self.url,
            env=env,
            transport=self.kind,
            enabled=True,
        )


MCP_CATALOG: Dict[str, CatalogEntry] = {
    entry.name: entry for entry in (
        CatalogEntry(
            name="filesystem",
            description="Read, write and search files under an allowed directory.",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-filesystem", "."),
        ),
        CatalogEntry(
            name="git",
            description="Inspect and operate a git repository: status, log, diff, commit.",
            command="uvx",
            args=("mcp-server-git",),
        ),
        CatalogEntry(
            name="fetch",
            description="Fetch a URL and convert it to readable markdown.",
            command="uvx",
            args=("mcp-server-fetch",),
        ),
        CatalogEntry(
            name="memory",
            description="Persistent knowledge graph memory across sessions.",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-memory"),
        ),
        CatalogEntry(
            name="time",
            description="Current time and timezone conversion.",
            command="uvx",
            args=("mcp-server-time",),
        ),
        CatalogEntry(
            name="sequential-thinking",
            description="Structured step-by-step reasoning scratchpad.",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-sequential-thinking"),
        ),
        CatalogEntry(
            name="context7",
            description="Up-to-date, version-specific library documentation.",
            url="https://mcp.context7.com/mcp",
        ),
        CatalogEntry(
            name="deepwiki",
            description="Ask questions about any public GitHub repository.",
            url="https://mcp.deepwiki.com/mcp",
        ),
        CatalogEntry(
            name="github",
            description="Issues, pull requests, code search and repository operations.",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-github"),
            env=(("GITHUB_TOKEN", "GitHub personal access token"),),
        ),
        CatalogEntry(
            name="playwright",
            description="Drive a real browser: navigate, click, fill, screenshot.",
            command="npx",
            args=("-y", "@playwright/mcp@latest"),
        ),
        CatalogEntry(
            name="puppeteer",
            description="Headless Chrome automation and page scraping.",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-puppeteer"),
        ),
        CatalogEntry(
            name="everything",
            description="Reference server exposing every MCP feature; for diagnostics.",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-everything"),
        ),
    )
}


def catalog_entries() -> List[CatalogEntry]:
    return sorted(MCP_CATALOG.values(), key=lambda entry: entry.name)


def catalog_entry(name: str) -> Optional[CatalogEntry]:
    return MCP_CATALOG.get((name or "").strip().lower())


def search_catalog(query: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    """Catalog rows whose name contains the query; empty query lists everything."""
    needle = (query or "").strip().casefold()
    installed = load_store()
    rows = [entry for entry in catalog_entries() if needle in entry.name.casefold()]
    rows.sort(key=lambda entry: entry.name)
    return [
        {
            "name": entry.name,
            "description": entry.description,
            "transport": entry.kind,
            "target": entry.url or f"{entry.command} {' '.join(entry.args)}".strip(),
            "env": [key for key, _ in entry.env],
            "installed": entry.name in installed,
        }
        for entry in rows[:max(1, int(limit))]
    ]


def _read_store(path: Path) -> Dict[str, Dict[str, Any]]:
    """The ``mcpServers`` table of one file, or {} when it is missing or broken."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {str(name): cfg for name, cfg in servers.items() if isinstance(cfg, dict)}


def project_store() -> Dict[str, Dict[str, Any]]:
    """Only the project ``mcp.json``; this is what writes mutate."""
    return _read_store(MCP_STORE)


def load_store() -> Dict[str, Dict[str, Any]]:
    """Merged ``mcpServers`` from the user file and the project file."""
    merged: Dict[str, Dict[str, Any]] = {}
    for path in store_paths():
        merged.update(_read_store(path))
    return merged


def save_store(servers: Dict[str, Dict[str, Any]]) -> None:
    """Write the project ``mcp.json`` atomically, with no resolved secret."""
    MCP_STORE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mcpServers": {name: cfg for name, cfg in sorted(servers.items())}}
    tmp = MCP_STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, MCP_STORE)


def ensure_store(defaults: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Create the project ``mcp.json`` on first run; stdio needs its command."""
    if MCP_STORE.exists():
        return load_store()
    configured = _read_store(MCP_HOME_STORE)
    seed: Dict[str, Dict[str, Any]] = {}
    for name, cfg in (defaults or {}).items():
        if name in configured:
            continue
        data = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg)
        entry = _to_store_entry(data)
        if entry.get("type") == "stdio":
            entry["enabled"] = bool(shutil.which(str(entry.get("command") or "")))
        else:
            entry["enabled"] = True
            entry["lazy"] = True
        seed[name] = entry
    save_store(seed)
    return load_store()


def _to_store_entry(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Config dict → mcp.json entry, dropping defaults and resolved secrets."""
    entry: Dict[str, Any] = {}
    if cfg.get("url"):
        entry["type"] = "http" if (cfg.get("transport") or "auto") in ("auto", "http", "streamable-http") else cfg["transport"]
        entry["url"] = cfg["url"]
    else:
        entry["type"] = "stdio"
        entry["command"] = cfg.get("command", "")
        if cfg.get("args"):
            entry["args"] = list(cfg["args"])
    for key in ("env", "headers"):
        values = {k: v for k, v in (cfg.get(key) or {}).items() if "${" in str(v)}
        if values:
            entry[key] = values
    if cfg.get("enabled"):
        entry["enabled"] = True
    if cfg.get("lazy"):
        entry["lazy"] = True
    if float(cfg.get("timeout") or 0) not in (0.0, 300.0):
        entry["timeout"] = cfg["timeout"]
    if float(cfg.get("connect_timeout") or 0) not in (0.0, 60.0):
        entry["connect_timeout"] = cfg["connect_timeout"]
    for key in ("tools_include", "tools_exclude"):
        if cfg.get(key):
            entry[key] = list(cfg[key])
    return entry


def _from_store_entry(name: str, cfg: Dict[str, Any]) -> MCPServerConfig:
    """mcp.json entry → config object; ``type`` is the transport hint."""
    return MCPServerConfig(
        command=str(cfg.get("command") or ""),
        args=[str(a) for a in (cfg.get("args") or [])],
        url=str(cfg.get("url") or ""),
        env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
        headers={str(k): str(v) for k, v in (cfg.get("headers") or {}).items()},
        transport=str(cfg.get("type") or "auto"),
        enabled=bool(cfg.get("enabled", False)),
        lazy=bool(cfg.get("lazy", False)),
        timeout=float(cfg.get("timeout", 300.0)),
        connect_timeout=float(cfg.get("connect_timeout", 60.0)),
        tools_include=[str(p) for p in (cfg.get("tools_include") or [])],
        tools_exclude=[str(p) for p in (cfg.get("tools_exclude") or [])],
    )


def store_configs() -> Dict[str, MCPServerConfig]:
    """Every server in ``mcp.json``, keyed by name."""
    return {name: _from_store_entry(name, cfg) for name, cfg in load_store().items()}


def install_from_catalog(name: str, secrets: Optional[Dict[str, str]] = None,
                         enable: bool = True) -> str:
    """Install one catalog entry into ``config.json``; secrets go to the env file."""
    entry = catalog_entry(name)
    if entry is None:
        near = ", ".join(e.name for e in search_catalog(name, limit=3))
        return f"Error: '{name}' is not in the MCP catalog." + (f" Closest: {near}." if near else "")
    from src.config import save_env_var

    secrets = {k: v for k, v in (secrets or {}).items() if v}
    missing = [key for key, _ in entry.env if not secrets.get(key)]
    if missing:
        return (f"Error: '{entry.name}' needs {', '.join(missing)}. "
                f"Call mcp_install(name='{entry.name}', env={{'{missing[0]}': '...'}}).")
    written = [key for key, value in secrets.items() if save_env_var(key, value)]
    config = load_config()
    servers = dict(getattr(config, "mcp_servers", {}) or {})
    servers[entry.name] = entry.to_config(secrets).model_copy(update={"enabled": enable})
    config.mcp_servers = servers
    config.save()
    note = f"; secrets stored in .env: {', '.join(written)}" if written else ""
    return f"MCP server '{entry.name}' installed ({entry.kind}){note}."


def uninstall_from_catalog(name: str) -> str:
    """Remove one server from ``mcp.json`` and ``config.json``; env vars stay."""
    store = project_store()
    removed = []
    if name in store:
        store.pop(name)
        save_store(store)
        removed.append(str(MCP_STORE))
    config = load_config()
    servers = dict(getattr(config, "mcp_servers", {}) or {})
    if name in servers:
        servers.pop(name)
        config.mcp_servers = servers
        config.save()
        removed.append("config.json")
    if not removed:
        return f"Error: '{name}' is not installed."
    return f"MCP server '{name}' removed from {', '.join(removed)}."


def _unwrap(exc: BaseException) -> BaseException:
    """Innermost exception of an exception group."""
    while getattr(exc, "exceptions", None):
        exc = exc.exceptions[0]
    return exc


def _expand(value: str, where: str) -> str:
    """Resolve ${VAR} references; an unset variable is a hard error."""
    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if not resolved:
            raise RuntimeError(f"{where}: environment variable '{name}' is not set")
        return resolved

    return _ENV_REF.sub(_replace, str(value or ""))


class MCPServer:
    """One MCP connection; its lifecycle task owns the transport."""

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        self.name = name
        self.config = config
        self.tools: Dict[str, Any] = {}
        self.public_names: Dict[str, str] = {}
        self.state = "idle"
        self._session: Optional[ClientSession] = None
        self._client = None
        self._http = None
        self._connected = False
        self._bg_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._last_error: Optional[str] = None
        self._errlog = None
        self._failures = 0
        self._retry_at = 0.0

    @property
    def in_backoff(self) -> bool:
        return bool(self._retry_at) and time.monotonic() < self._retry_at

    @property
    def retry_in(self) -> float:
        return round(max(0.0, self._retry_at - time.monotonic()), 1) if self._retry_at else 0.0

    def _stderr_tail(self, prefix: str) -> str:
        """Attach the child's last stderr lines to a failure message."""
        if self._errlog is None:
            return prefix
        try:
            tail = self._errlog.getvalue()[-400:].strip()
        except Exception:
            tail = ""
        return f"{prefix} | stderr: {tail}" if tail else prefix

    def _stdio_params(self) -> "StdioServerParameters":
        """Process parameters, every ${VAR} reference resolved."""
        env = dict(os.environ)
        for key, value in self.config.env.items():
            env[key] = _expand(value, f"{self.name}.env.{key}")
        env.update({"NODE_ENV": "production", "LOG_LEVEL": "silent", "FORCE_COLOR": "0"})
        command = _expand(self.config.command, f"{self.name}.command")
        args = [_expand(arg, f"{self.name}.args") for arg in self.config.args]
        if os.name == "nt":
            found = shutil.which(command) or shutil.which(f"{command}.cmd") or shutil.which(f"{command}.exe")
            if found and found.lower().endswith((".cmd", ".bat")):
                command, args = "cmd", ["/c", found, *args]
            elif found:
                command = found
        return StdioServerParameters(command=command, args=args, env=env)

    def _remote_transport(self):
        """HTTP transport with resolved headers and the server's own timeout."""
        url = _expand(self.config.url, f"{self.name}.url")
        headers = {key: _expand(value, f"{self.name}.headers.{key}") for key, value in self.config.headers.items()}
        timeout = max(5.0, float(self.config.timeout))
        if self.config.kind == "sse":
            return sse_client(url, headers=headers or None, timeout=min(timeout, 60.0), sse_read_timeout=timeout)
        if _HTTP_CLIENT is None:
            raise RuntimeError(f"{self.name}: httpx is required for streamable HTTP")
        self._http = _HTTP_CLIENT.AsyncClient(
            headers=headers or None,
            timeout=_HTTP_CLIENT.Timeout(timeout, read=timeout),
            follow_redirects=False,
        )
        return streamable_http_client(url, http_client=self._http, terminate_on_close=True)

    def _transport(self, errlog):
        """Context manager for the transport this server is configured for."""
        if self.config.kind == "stdio":
            return stdio_client(self._stdio_params(), errlog=errlog)
        return self._remote_transport()

    def _wanted(self, tool_name: str) -> bool:
        """Apply this server's include/exclude filters."""
        def _hit(pattern: str) -> bool:
            return fnmatch.fnmatch(tool_name, pattern) or fnmatch.fnmatch(tool_name, f"*{pattern}*")

        include = [p for p in self.config.tools_include if p]
        exclude = [p for p in self.config.tools_exclude if p]
        if include and not any(_hit(p) for p in include):
            return False
        return not any(_hit(p) for p in exclude)

    async def _list_tools(self, target) -> None:
        """Page through tools/list, keeping the tools the filters admit."""
        self.tools = {}
        cursor = None
        while True:
            page = await target.list_tools(cursor=cursor) if cursor else await target.list_tools()
            self.tools.update({tool.name: tool for tool in page.tools if self._wanted(tool.name)})
            cursor = getattr(page, "nextCursor", None) or getattr(page, "next_cursor", None)
            if not cursor:
                break

    async def _run_lifecycle(self) -> None:
        """Hold the connection open until cancelled."""
        errlog = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False, suffix=".log")
        self._errlog = errlog
        try:
            transport = self._transport(errlog)
            if MCP_MODERN_CLIENT is not None:
                rounds = max(1, int(self.config.input_required_max_rounds))
                async with MCP_MODERN_CLIENT(transport, input_required_max_rounds=rounds) as client:
                    await self._list_tools(client)
                    self._client = client
                    self._mark_ready()
                    await asyncio.Event().wait()
            else:
                async with transport as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        await self._list_tools(session)
                        self._session = session
                        self._mark_ready()
                        await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._last_error = self._stderr_tail("cancelled")
            raise
        except Exception as exc:
            detail = _unwrap(exc)
            self._last_error = self._stderr_tail(f"{type(detail).__name__}: {detail}")
            self._record_failure()
            logger.error("MCP server '%s' lifecycle error: %s", self.name, self._last_error)
        finally:
            await self._release()

    def _record_failure(self) -> None:
        """Exponential backoff; a dead server keeps its last catalog."""
        self._failures += 1
        delay = min(BACKOFF_MAX, BACKOFF_START * (2 ** (self._failures - 1)))
        self._retry_at = time.monotonic() + delay
        self.state = "dead" if self._failures >= DEAD_AFTER else "degraded"

    def _mark_ready(self) -> None:
        self._connected = True
        self._last_error = None
        self._failures = 0
        self._retry_at = 0.0
        self.state = "ready"
        self._ready.set()

    async def _release(self) -> None:
        """Drop every handle this lifecycle owned."""
        if self._connected:
            self.state = "idle"
        self._connected = False
        self._session = None
        self._client = None
        self._ready.set()
        client, self._http = self._http, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        if self._errlog:
            try:
                self._errlog.close()
                os.unlink(self._errlog.name)
            except Exception:
                pass
            self._errlog = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self, force: bool = False) -> bool:
        """Connect once and wait for the handshake, bounded by connect_timeout."""
        if not self.config.enabled:
            return False
        if self.config.command == "internal":
            from src.tools import TOOL_REGISTRY

            self.tools = dict(TOOL_REGISTRY)
            self._mark_ready()
            return True
        if not MCP_SDK_AVAILABLE:
            self._last_error = "MCP SDK is not installed"
            self._record_failure()
            return False
        if not force and self._retry_at and time.monotonic() < self._retry_at:
            return False
        self.state = "starting"
        self._ready.clear()
        self._bg_task = asyncio.create_task(self._run_lifecycle())
        timeout = max(5.0, float(self.config.connect_timeout))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._last_error = self._stderr_tail(f"startup timeout after {timeout:.0f}s")
            self._record_failure()
            await self.stop(timeout=5.0)
            return False
        return self._connected

    async def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Run one tool and flatten its text content."""
        if not self._connected:
            raise RuntimeError(f"{self.name} not connected")
        if tool_name not in self.tools:
            raise ValueError(f"Tool {tool_name} not found")
        if self.config.command == "internal":
            func = self.tools[tool_name]
            return str(await func(**arguments) if inspect.iscoroutinefunction(func) else func(**arguments))
        target = self._client or self._session
        if target is None:
            raise RuntimeError(f"{self.name} not connected")
        timeout = max(5.0, float(self.config.timeout))
        try:
            result = await asyncio.wait_for(target.call_tool(tool_name, arguments or {}), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"tool '{tool_name}' timed out after {timeout:.0f}s") from exc
        except Exception as exc:
            raise RuntimeError(f"tool '{tool_name}' failed: {_unwrap(exc)}") from exc
        text = self._content_text(result)
        if getattr(result, "isError", False):
            raise RuntimeError(f"tool '{tool_name}' returned an error: {text or 'no detail'}")
        if text.startswith(";:stderr:"):
            cleaned = re.sub(r"(?m)^;:stderr:.*\n?", "", text).strip()
            return cleaned or "Error: MCP tool crashed or timed out."
        return text

    @staticmethod
    def _content_text(result: Any) -> str:
        """Join the text blocks of a result; non-text media is named, not inlined."""
        parts = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
                continue
            kind = getattr(item, "type", None)
            if kind and kind != "text":
                parts.append(f"[{kind} content omitted]")
        return "\n".join(parts).strip()

    async def stop(self, timeout: float = 10.0) -> None:
        task, self._bg_task = self._bg_task, None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=max(1.0, timeout))
            except BaseException:
                pass
        self._connected = False
        self._session = None
        self._client = None
        if not self._failures:
            self.state = "idle"


class MCPManager:
    """Process-wide MCP client; one connection per configured server."""

    _instance: Optional["MCPManager"] = None

    def __new__(cls, settings: Optional[GlobalSettings] = None) -> "MCPManager":
        """Singleton: repeated construction never drops live connections."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, settings: Optional[GlobalSettings] = None) -> None:
        if not getattr(self, "_initialized", False):
            self._initialized = True
            self.settings = settings or load_config()
            self.servers: Dict[str, MCPServer] = {}
            self.tools_map: Dict[str, str] = {}
            self._calls: Dict[str, tuple] = {}
            self._connect_lock = asyncio.Lock()
            self._server_signature = self._signature(self.settings)
            self._build_servers()
        elif settings is not None and self._signature(settings) != self._server_signature:
            self.reload(settings)

    @staticmethod
    def _signature(settings: GlobalSettings) -> str:
        servers = getattr(settings, "mcp_servers", {}) or {}
        return json.dumps({name: cfg.model_dump() for name, cfg in servers.items()}, sort_keys=True, default=str)

    def _build_servers(self) -> None:
        self.servers.clear()
        self.tools_map.clear()
        self._calls.clear()
        for name, config in (getattr(self.settings, "mcp_servers", {}) or {}).items():
            if config.enabled:
                self.servers[name] = MCPServer(name, config)

    def _public_name(self, server: str, tool: str) -> str:
        """`mcp__<server>__<tool>`, hashed when the joined name exceeds 64 chars."""
        clean = _UNSAFE.sub("_", tool)
        name = f"mcp__{_UNSAFE.sub('_', server)}__{clean}"
        if len(name) > _MAX_TOOL_NAME:
            digest = hashlib.sha1(f"{server}\0{tool}".encode()).hexdigest()[:8]
            name = f"{name[: _MAX_TOOL_NAME - 9]}_{digest}"
        return name

    @staticmethod
    def _serves(server: MCPServer) -> bool:
        """Live, or degraded with a catalog worth keeping while it recovers."""
        return server.connected or (server.state in ("degraded", "dead") and bool(server.tools))

    def _reindex(self) -> None:
        """Recompute public tool names across every live connection."""
        counts: Dict[str, int] = {}
        for server in self.servers.values():
            if self._serves(server):
                for tool in server.tools:
                    counts[tool] = counts.get(tool, 0) + 1
        self.tools_map.clear()
        self._calls.clear()
        for server in self.servers.values():
            if not self._serves(server):
                server.public_names = {}
                continue
            names = {}
            for tool in server.tools:
                public = self._public_name(server.name, tool)
                names[tool] = public
                self.tools_map[public] = server.name
                self._calls[public] = (server.name, tool)
                if counts[tool] == 1:
                    legacy = f"mcp_{_UNSAFE.sub('_', tool)}"
                    self._calls.setdefault(legacy, (server.name, tool))
            server.public_names = names

    def reload(self, settings: Optional[GlobalSettings] = None) -> None:
        """Rebuild the server table; only called when the config actually changed."""
        settings = settings or self.settings
        old = list(self.servers.values())
        self.settings = settings
        self._server_signature = self._signature(settings)
        self._build_servers()
        for server in old:
            task = server._bg_task
            if task and not task.done():
                task.cancel()

    def reload_from_store(self) -> None:
        """Rebuild the server table from ``mcp.json`` after an install or removal."""
        from src.config import CONFIG_PATH, _merge_mcp_servers

        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            configured = raw.get("mcp_servers") or {}
        except (OSError, json.JSONDecodeError):
            configured = {}
        self.settings.mcp_servers = _merge_mcp_servers(configured)
        self.reload()

    def list_servers(self) -> List[str]:
        return list(self.servers.keys())

    def list_tools(self) -> Dict[str, str]:
        return dict(self.tools_map)

    def server_status(self) -> Dict[str, Dict[str, Any]]:
        """Health snapshot of every configured server, backoff included."""
        return {
            name: {
                "state": server.state,
                "connected": server.connected,
                "tools": len(server.tools),
                "attempts": server._failures,
                "retry_in": server.retry_in,
                "error": server._last_error,
            }
            for name, server in self.servers.items()
        }

    async def ensure_server(self, name: str, force: bool = False) -> bool:
        """Connect a server on demand; a dropped connection is reopened lazily."""
        config = (getattr(self.settings, "mcp_servers", {}) or {}).get(name)
        if config is None or not config.enabled:
            return False
        server = self.servers.get(name)
        if server and server.connected:
            return True
        if not force and server and server.in_backoff:
            return False
        async with self._connect_lock:
            server = self.servers.get(name)
            if server and server._bg_task and not server._bg_task.done():
                await self._await_ready(server, config)
                return server.connected
            if server is None:
                server = MCPServer(name, config)
                self.servers[name] = server
            try:
                await server.start(force=force)
            except Exception as exc:
                server._last_error = str(_unwrap(exc))
                server._record_failure()
            self._reindex()
            return server.connected

    async def doctor(self, force: bool = True) -> List[Dict[str, Any]]:
        """Probe every configured server and report a per-server diagnosis."""
        report = []
        for name, config in (getattr(self.settings, "mcp_servers", {}) or {}).items():
            server = self.servers.get(name)
            if not config.enabled:
                report.append({"server": name, "state": "off", "detail": "disabled in config"})
                continue
            if config.kind == "stdio" and config.command != "internal" and not shutil.which(config.command):
                report.append({"server": name, "state": "dead",
                               "detail": f"command '{config.command}' not found in PATH"})
                continue
            ok = await self.ensure_server(name, force=force)
            live = self.servers.get(name)
            state = "ready" if ok else (live.state if live else "dead")
            detail = f"{len(live.tools)} tools" if ok else (live._last_error if live and live._last_error else "not connected")
            report.append({"server": name, "state": state, "detail": detail})
        self._reindex()
        return report

    @staticmethod
    async def _await_ready(server: MCPServer, config: MCPServerConfig) -> None:
        try:
            await asyncio.wait_for(server._ready.wait(), timeout=max(5.0, float(config.connect_timeout)))
        except asyncio.TimeoutError:
            pass

    async def connect_all(self) -> Dict[str, bool]:
        """Start every enabled, non-lazy server; failures are retried next call."""
        results: Dict[str, bool] = {}
        pending = []
        for name, config in (getattr(self.settings, "mcp_servers", {}) or {}).items():
            if not config.enabled or config.lazy:
                continue
            if config.kind == "stdio" and config.command != "internal" and not shutil.which(config.command):
                server = self.servers.get(name)
                if server:
                    server._last_error = f"command '{config.command}' not found in PATH"
                results[name] = False
                continue
            pending.append((name, config))

        async def _start(name: str, config: MCPServerConfig) -> bool:
            await self.ensure_server(name)
            server = self.servers.get(name)
            return bool(server and server.connected)

        outcomes = await asyncio.gather(*[_start(name, config) for name, config in pending])
        for (name, _), ok in zip(pending, outcomes):
            results[name] = ok
            if not ok:
                server = self.servers.get(name)
                if server and server._last_error:
                    logger.warning("MCP server '%s' unavailable: %s", name, server._last_error)
        self._reindex()
        return results

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any], server: Optional[str] = None) -> str:
        """Call a tool by public name, or by explicit server and tool name."""
        target, tool = self._calls.get(tool_name, (None, None))
        if target is None:
            target, tool = server, tool_name
        if not target:
            await self.connect_all()
            target, tool = self._calls.get(tool_name, (None, None))
        if not target:
            return f"Error: no MCP server provides tool '{tool_name}'."
        if not await self.ensure_server(target):
            state = self.servers.get(target)
            return f"Error: server '{target}' unavailable: {state._last_error if state else 'not configured'}"
        live = self.servers[target]
        if not live.connected:
            return f"Error: server '{target}' not connected: {live._last_error or 'unknown error'}"
        if tool not in live.tools:
            return f"Error: tool '{tool}' not found on '{target}'. Available: {', '.join(live.tools) or 'none'}."
        try:
            return await live.execute(tool, arguments or {})
        except Exception as exc:
            return f"Error executing tool '{tool}': {_unwrap(exc)}"

    async def restart_server(self, name: str) -> bool:
        old = self.servers.get(name)
        if old:
            await old.stop(timeout=5.0)
        self.servers.pop(name, None)
        self._reindex()
        return await self.ensure_server(name)

    async def start_server(self, name: str) -> bool:
        if name not in (getattr(self.settings, "mcp_servers", {}) or {}):
            return False
        return await self.ensure_server(name)

    async def cleanup(self) -> None:
        """Stop every server and drop the singleton."""
        servers = list(self.servers.values())
        self.servers.clear()
        self.tools_map.clear()
        self._calls.clear()
        for server in servers:
            await server.stop(timeout=5.0)
        MCPManager._instance = None
