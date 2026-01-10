import asyncio
import shutil
import os
from typing import Dict, Any, List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from src.config import GlobalSettings
from src.core.memory import memory_core

class MCPManager:
    def __init__(self, settings: GlobalSettings):
        self.settings = settings
        self.sessions: Dict[str, ClientSession] = {}
        self.tools_map: Dict[str, Any] = {} 
        self.exit_stack = None
        self.failed_servers: Dict[str, str] = {}
        self.enabled = settings.mcp_enabled

    async def connect_all(self):
        if not self.enabled:
            return
            
        from contextlib import AsyncExitStack
        self.exit_stack = AsyncExitStack()

        for name, cfg in self.settings.mcp_servers.items():
            if not cfg.enabled: 
                continue 
            
            exe = shutil.which(cfg.command)
            if not exe:
                msg = f"Command '{cfg.command}' not found in PATH"
                self.failed_servers[name] = msg
                memory_core.log_event("system", {"server": name, "error": msg}, "mcp_error")
                print(f"[MCP] Warning: {msg}")
                continue

            try:
                env = os.environ.copy()
                env.update(cfg.env)

                params = StdioServerParameters(
                    command=cfg.command, 
                    args=cfg.args, 
                    env=env
                )
                
                read, write = await self.exit_stack.enter_async_context(stdio_client(params))
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                
                self.sessions[name] = session
                
                tools = await session.list_tools()
                for tool in tools.tools:
                    scoped_name = f"{name}_{tool.name}" 
                    self.tools_map[scoped_name] = {"session": session, "def": tool}
                
                memory_core.log_event("system", {"server": name, "tools": len(tools.tools)}, "mcp_connected")
                    
            except Exception as e:
                error_msg = str(e)
                self.failed_servers[name] = error_msg
                memory_core.log_event("system", {"server": name, "error": error_msg}, "mcp_fail")
                print(f"[MCP] Failed to connect to '{name}': {error_msg}")

    async def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if not self.enabled:
            return "MCP Error: MCP is globally disabled."
        if tool_name not in self.tools_map:
            return f"MCP Error: Tool '{tool_name}' not found."
        
        mapping = self.tools_map[tool_name]
        session = mapping["session"]
        original_name = mapping["def"].name
        
        try:
            result = await session.call_tool(original_name, arguments=arguments)
            output = []
            for content in result.content:
                if content.type == "text":
                    output.append(content.text)
                elif content.type == "image":
                    output.append("[Image Data]")
            return "\n".join(output)
        except Exception as e:
            memory_core.log_event("system", {"tool": tool_name, "error": str(e)}, "mcp_exec_fail")
            return f"MCP Execution Error: {e}"

    def get_tools_schema(self) -> str:
        if not self.enabled:
            return "MCP disabled"
        schemas = []
        for name, mapping in self.tools_map.items():
            tool_def = mapping["def"]
            props = list(tool_def.inputSchema.get('properties', {}).keys())
            schemas.append(f"- {name}({', '.join(props)})")
        return "\n".join(schemas)

    async def cleanup(self):
        if self.exit_stack:
            try:
                await self.exit_stack.aclose()
            except Exception:
                pass