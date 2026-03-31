import asyncio
import json
import logging
import os
import shutil
from typing import Any, Dict, Optional
from src.config import GlobalSettings, MCPServerConfig, load_config

logger = logging.getLogger(__name__)

MCP_SDK_AVAILABLE = False
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_SDK_AVAILABLE = True
except ImportError:
    pass


class MCPServer:
    def __init__(self, name: str, config: MCPServerConfig):
        self.name = name
        self.config = config
        self.tools: Dict[str, Any] = {}
        self._session: Optional[ClientSession] = None
        self._connected = False
        self._bg_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def _run_lifecycle(self, params):
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    self.tools = {t.name: t for t in tools_result.tools}
                    self._session = session
                    self._connected = True
                    self._ready.set()
                    await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self._connected = False
            self._session = None

    async def start(self) -> bool:
        if not self.config.enabled:
            return False
        if self.config.command == "internal":
            from src.tools import TOOL_REGISTRY
            self.tools = dict(TOOL_REGISTRY)
            self._connected = True
            return True
        if not MCP_SDK_AVAILABLE:
            return False
        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env={**dict(os.environ), **self.config.env} if self.config.env else None,
        )
        self._bg_task = asyncio.create_task(self._run_lifecycle(params))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15.0)
            return self._connected
        except asyncio.TimeoutError:
            self._bg_task.cancel()
            try:
                await asyncio.wait_for(self._bg_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            return False

    async def execute(self, tool_name: str, arguments: Dict) -> str:
        if not self._connected:
            return f"Error: {self.name} not connected"
        if tool_name not in self.tools:
            return f"Error: Tool {tool_name} not found"
        # Internal server: call tool function directly
        if self.config.command == "internal":
            import inspect
            func = self.tools[tool_name]
            try:
                if inspect.iscoroutinefunction(func):
                    return str(await func(**arguments))
                return str(func(**arguments))
            except Exception as e:
                return f"Error: {e}"
        # External server: JSON-RPC via session
        if not self._session:
            return f"Error: {self.name} not connected"
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=30.0
            )
            if result and result.content:
                for item in result.content:
                    if hasattr(item, 'text'):
                        return item.text
            return "Error: No response"
        except Exception as e:
            return f"Error: {e}"

    async def stop(self) -> None:
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            try:
                await asyncio.wait_for(self._bg_task, timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        self._connected = False
        self._session = None
        self._bg_task = None


class MCPManager:
    _instance = None

    def __new__(cls, settings: GlobalSettings = None):
        if cls._instance is None:
            if settings is None:
                settings = load_config()
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        elif settings is not None and cls._instance._initialized:
            cls._instance.settings = settings
        return cls._instance

    def __init__(self, settings: GlobalSettings = None):
        if getattr(self, '_initialized', False):
            return
        if settings is None:
            settings = load_config()
        self._initialized = True
        self.settings = settings
        self.servers: Dict[str, MCPServer] = {}
        self.tools_map: Dict[str, str] = {}

    def get_tools_schema(self, registered_tools=None) -> str:
        import inspect
        lines = []
        for server in self.servers.values():
            if not server._connected:
                continue
            is_internal = server.config.command == "internal"
            for tool_name, tool_def in server.tools.items():
                mcp_name = f"mcp_{tool_name}"
                if registered_tools is not None:
                    check_name = tool_name if is_internal else mcp_name
                    if check_name not in registered_tools:
                        continue
                desc = getattr(tool_def, "description", None)
                if not desc:
                    if is_internal:
                        desc = getattr(tool_def, "__doc__", None) or "Tool."
                    else:
                        desc = "Tool."
                desc = desc.split('\n')[0][:100]
                if is_internal:
                    try:
                        sig = inspect.signature(tool_def)
                        params = [p for p in sig.parameters if p != 'self']
                        if params:
                            desc += f" Params: {', '.join(params)}"
                    except (ValueError, TypeError):
                        pass
                else:
                    schema = getattr(tool_def, 'inputSchema', None)
                    if schema and isinstance(schema, dict):
                        props = schema.get('properties', {})
                        if props:
                            desc += f" Params: {', '.join(props.keys())}"
                lines.append(f"- {mcp_name}: {desc}")
        return "\n".join(lines) if lines else ""

    async def connect_all(self) -> Dict[str, bool]:
        results = {}
        servers_by_type = {"internal": [], "external": []}
        for name, config in self.settings.mcp_servers.items():
            if not config.enabled:
                continue
            if config.command == "internal":
                servers_by_type["internal"].append((name, config))
            else:
                cmd_path = shutil.which(config.command) or shutil.which(f"{config.command}.exe")
                if not cmd_path:
                    pkg = config.args[0] if config.args else name
                    logger.warning(f"MCP '{name}': '{config.command}' not found. Try: pip install {pkg}")
                    results[name] = False
                    continue
                servers_by_type["external"].append((name, config))
        for name, config in servers_by_type["internal"]:
            server = MCPServer(name, config)
            try:
                success = await asyncio.wait_for(server.start(), timeout=10.0)
            except asyncio.TimeoutError:
                success = False
            results[name] = success
            if success:
                self.servers[name] = server
                for tool_name in server.tools:
                    self.tools_map[tool_name] = name
        for name, config in servers_by_type["external"]:
            server = MCPServer(name, config)
            try:
                success = await asyncio.wait_for(server.start(), timeout=15.0)
            except asyncio.TimeoutError:
                success = False
            results[name] = success
            if success:
                self.servers[name] = server
                for tool_name in server.tools:
                    self.tools_map[tool_name] = name
        return results

    async def execute_tool(self, tool_name: str, arguments: Dict) -> str:
        if tool_name not in self.tools_map:
            return f"Error: Unknown tool {tool_name}"
        server = self.servers.get(self.tools_map[tool_name])
        if not server:
            return f"Error: Server not found"
        return await server.execute(tool_name, arguments)

    async def cleanup(self) -> None:
        for server in self.servers.values():
            await server.stop()
        self.servers.clear()
        self.tools_map.clear()
