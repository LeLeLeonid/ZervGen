import asyncio
import inspect
import json
import logging
import os
import re
import io
import shutil
import tempfile
from typing import Any, Dict, Optional, List
from src.config import GlobalSettings, MCPServerConfig, load_config

logger = logging.getLogger(__name__)

MCP_SDK_AVAILABLE = False
MCP_MODERN_CLIENT = None
try:
    from mcp import Client, ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_SDK_AVAILABLE = True
    MCP_MODERN_CLIENT = Client
except ImportError:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        MCP_SDK_AVAILABLE = True
    except ImportError:
        pass


def _unwrap(e: BaseException) -> BaseException:
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    return e


class MCPServer:
    def __init__(self, name: str, config: MCPServerConfig):
        self.name = name
        self.config = config
        self.tools: Dict[str, Any] = {}
        self._session: Optional[ClientSession] = None
        self._client = None
        self._connected = False
        self._bg_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._last_error = None
        self._errlog = None

    def _stderr_tail(self, prefix: str) -> str:
        if self._errlog is None:
            return prefix
        tail = ""
        try:
            if hasattr(self._errlog, "getvalue"):
                tail = self._errlog.getvalue()[-400:].strip()
            else:
                self._errlog.seek(0)
                tail = self._errlog.read()[-400:].strip()
        except Exception:
            tail = ""
        return f"{prefix} | stderr: {tail}" if tail else prefix

    async def _run_lifecycle(self, params):
        errlog = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False, suffix=".log")
        self._errlog = errlog
        try:
            if MCP_MODERN_CLIENT is not None:
                transport = stdio_client(params, errlog=errlog)
                async with MCP_MODERN_CLIENT(transport, input_required_max_rounds=max(1, int(getattr(self.config, "mcp_input_required_max_rounds", 10)))) as client:
                    self._client = client
                    self.tools = {}
                    cursor = None
                    while True:
                        tools_result = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
                        self.tools.update({t.name: t for t in tools_result.tools})
                        cursor = getattr(tools_result, "nextCursor", None) or getattr(tools_result, "next_cursor", None)
                        if not cursor:
                            break
                    self._connected = True
                    self._ready.set()
                    self._last_error = None
                    await asyncio.Event().wait()
            else:
                async with stdio_client(params, errlog=errlog) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        self.tools = {}
                        cursor = None
                        while True:
                            tools_result = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                            self.tools.update({t.name: t for t in tools_result.tools})
                            cursor = getattr(tools_result, "nextCursor", None) or getattr(tools_result, "next_cursor", None)
                            if not cursor:
                                break
                        self._session = session
                        self._connected = True
                        self._ready.set()
                        self._last_error = None
                        await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._last_error = self._stderr_tail("cancelled")
            raise
        except Exception as e:
            detail = _unwrap(e)
            self._last_error = self._stderr_tail(f"{type(detail).__name__}: {detail}")
            logger.error("MCP server '%s' lifecycle error: %s", self.name, self._last_error)
        finally:
            self._connected = False
            self._session = None
            self._client = None
            self._ready.set()
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

    async def start(self) -> bool:
        if not self.config.enabled:
            return False
        if self.config.command == "internal":
            from src.tools import TOOL_REGISTRY
            self.tools = dict(TOOL_REGISTRY)
            self._connected = True
            self._ready.set()
            self._last_error = None
            return True
        if not MCP_SDK_AVAILABLE:
            self._last_error = "MCP SDK is not installed"
            return False

        env = {**dict(os.environ), **self.config.env} if self.config.env else dict(os.environ)
        env["NODE_ENV"] = "production"
        env["LOG_LEVEL"] = "silent"
        env["FORCE_COLOR"] = "0"
        cmd = self.config.command
        sargs = list(self.config.args)
        if os.name == "nt":
            resolved = shutil.which(cmd) or shutil.which(f"{cmd}.cmd") or shutil.which(f"{cmd}.exe")
            if resolved and resolved.lower().endswith((".cmd", ".bat")):
                sargs = ["/c", resolved, *sargs]
                cmd = "cmd"
            elif resolved:
                cmd = resolved
        params = StdioServerParameters(command=cmd, args=sargs, env=env)
        self._ready.clear()
        self._bg_task = asyncio.create_task(self._run_lifecycle(params))
        timeout = max(5.0, float(getattr(self.config, "startup_timeout", 15.0)))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return self._connected
        except asyncio.TimeoutError:
            self._last_error = self._stderr_tail(f"startup timeout after {timeout:.1f}s")
            await self.stop(timeout=5.0)
            return False

    async def execute(self, tool_name: str, arguments: Dict) -> str:
        if not self._connected:
            raise RuntimeError(f"{self.name} not connected")
        if tool_name not in self.tools:
            raise ValueError(f"Tool {tool_name} not found")
        if self.config.command == "internal":
            func = self.tools[tool_name]
            if inspect.iscoroutinefunction(func):
                return str(await func(**arguments))
            return str(func(**arguments))
        target = self._client or self._session
        if not target:
            raise RuntimeError(f"{self.name} not connected")
        try:
            result = await asyncio.wait_for(target.call_tool(tool_name, arguments), timeout=30.0)
            if result and result.content:
                for item in result.content:
                    if hasattr(item, "text"):
                        if item.text.startswith(";:stderr:"):
                            cleaned = re.sub(r"(?m)^;:stderr:.*\n?", "", item.text).strip()
                            return cleaned or "Error: MCP tool crashed or timed out."
                        return item.text
            return ""
        except Exception as e:
            raise RuntimeError(f"MCP tool '{tool_name}' failed: {e}") from e

    async def stop(self, timeout: float = 10.0) -> None:
        task = self._bg_task
        self._bg_task = None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=max(1.0, timeout))
            except BaseException:
                pass
        self._connected = False
        self._session = None
        self._client = None


class MCPManager:
    _instance: Optional["MCPManager"] = None

    def __new__(cls, settings: Optional[GlobalSettings] = None) -> "MCPManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        elif settings is not None and cls._instance._initialized:
            cls._instance.settings = settings
        return cls._instance

    def __init__(self, settings: Optional[GlobalSettings] = None) -> None:
        if not getattr(self, "_initialized", False):
            self._initialized = True
            self.settings = settings or load_config()
            self.servers: Dict[str, MCPServer] = {}
            self.tools_map: Dict[str, str] = {}
            self._connect_attempted = False
            self._build_servers()
        elif settings is not None:
            self.reload(settings)

    def _build_servers(self) -> None:
        self.servers.clear()
        self.tools_map.clear()
        self._connect_attempted = False
        servers = getattr(self.settings, "mcp_servers", {})
        for name, cfg in servers.items():
            if cfg.enabled:
                self.servers[name] = MCPServer(name, cfg)

    def reload(self, settings: Optional[GlobalSettings] = None) -> None:
        settings = settings or self.settings
        old_servers = list(self.servers.values())
        self.settings = settings
        self._build_servers()
        for server in old_servers:
            task = server._bg_task
            if task and not task.done():
                task.cancel()

    def list_servers(self) -> List[str]:
        return list(self.servers.keys())

    def list_tools(self) -> Dict[str, str]:
        return dict(self.tools_map)

    def server_status(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: {
                "connected": server.connected,
                "tools": len(server.tools),
                "error": server._last_error,
            }
            for name, server in self.servers.items()
        }

    async def connect_all(self) -> Dict[str, bool]:
        if self._connect_attempted:
            return {name: server.connected for name, server in self.servers.items()}
        self._connect_attempted = True
        results: Dict[str, bool] = {}
        pairs: list[tuple[str, MCPServerConfig]] = []
        for name, config in self.settings.mcp_servers.items():
            if not config.enabled:
                continue
            if config.command != "internal":
                cmd_path = shutil.which(config.command)
                if os.name == "nt":
                    cmd_path = cmd_path or shutil.which(f"{config.command}.exe") or shutil.which(f"{config.command}.cmd")
                if not cmd_path:
                    stub = self.servers.get(name)
                    if stub:
                        stub._last_error = f"command '{config.command}' not found in PATH"
                    results[name] = False
                    continue
            pairs.append((name, config))

        async def _start_one(name: str, config: MCPServerConfig):
            server = self.servers.get(name) or MCPServer(name, config)
            try:
                ok = await server.start()
            except Exception as e:
                ok = False
                server._last_error = str(_unwrap(e))
            self.servers[name] = server
            return name, server, ok

        started = await asyncio.gather(*[_start_one(name, config) for name, config in pairs], return_exceptions=False)
        for name, server, ok in started:
            results[name] = ok
            if ok:
                for tool_name in server.tools:
                    self.tools_map[tool_name] = name
            elif server._last_error:
                logger.warning("MCP server '%s' unavailable: %s", name, server._last_error)
        return results

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any], server: Optional[str] = None) -> str:
        if not self._connect_attempted:
            await self.connect_all()
        if server:
            srv = self.servers.get(server)
        else:
            srv_name = self.tools_map.get(tool_name)
            srv = self.servers.get(srv_name) if srv_name else None
        if not srv:
            return f"Error: server not found for tool '{tool_name}'."
        if not srv.connected:
            return f"Error: server '{srv.name}' not connected: {srv._last_error or 'unknown error'}"
        if tool_name not in srv.tools:
            available = ", ".join(srv.tools.keys()) if srv.tools else "none"
            return f"Error: tool '{tool_name}' not found on '{srv.name}'. Available: {available}."
        try:
            result = await srv.execute(tool_name, arguments)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return f"Error executing tool '{tool_name}': {e}"

    async def start_server(self, name: str) -> bool:
        if name not in self.settings.mcp_servers:
            return False
        config = self.settings.mcp_servers[name]
        if not config.enabled:
            return False
        server = MCPServer(name, config)
        try:
            success = await server.start()
        except Exception as e:
            server._last_error = str(_unwrap(e))
            success = False
        if success:
            self.servers[name] = server
            for tool_name in server.tools:
                self.tools_map[tool_name] = name
        else:
            self.servers[name] = server
        return success

    async def cleanup(self) -> None:
        servers = list(self.servers.values())
        self.servers.clear()
        self.tools_map.clear()
        self._connect_attempted = False
        for server in servers:
            await server.stop(timeout=5.0)
        MCPManager._instance = None
