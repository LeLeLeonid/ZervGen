import asyncio
import shutil
import os
from typing import Dict, Any, List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from src.config import GlobalSettings
from src.utils import sys_logger

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
            sys_logger.log("system", "mcp_disabled_global", {})
            print("[MCP] Globally disabled, skipping all MCP servers.")
            return
        from contextlib import AsyncExitStack
        self.exit_stack = AsyncExitStack()

        for name, cfg in self.settings.mcp_servers.items():
            if not cfg.enabled: 
                sys_logger.log("system", "mcp_disabled", {"server": name})
                continue 
            
            exe = shutil.which(cfg.command)
            if not exe:
                msg = f"Command '{cfg.command}' not found in PATH"
                self.failed_servers[name] = msg
                sys_logger.log("system", "mcp_missing_exe", {"server": name, "command": cfg.command})
                print(f"[MCP] Warning: {msg}. Install Node.js/Docker.")
                continue

            try:
                env = os.environ.copy()
                env.update(cfg.env)

                params = StdioServerParameters(
                    command=cfg.command, 
                    args=cfg.args, 
                    env=env
                )
                
                sys_logger.log("system", "mcp_connecting", {"server": name, "command": cfg.command})
                
                read, write = await self.exit_stack.enter_async_context(stdio_client(params))
                session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                
                self.sessions[name] = session
                
                tools = await session.list_tools()
                for tool in tools.tools:
                    scoped_name = f"{name}_{tool.name}" 
                    self.tools_map[scoped_name] = {"session": session, "def": tool}
                
                sys_logger.log("system", "mcp_connected", {"server": name, "tools_count": len(tools.tools)})
                print(f"[MCP] Connected to '{name}' with {len(tools.tools)} tools.")
                    
            except Exception as e:
                error_msg = str(e)
                self.failed_servers[name] = error_msg
                
                if "Connection closed" in error_msg:
                    sys_logger.log("system", "mcp_connection_closed", {"server": name, "error": error_msg})
                    print(f"[MCP] Failed to connect to '{name}': Connection closed (server may have crashed or timed out)")
                else:
                    sys_logger.log("system", "mcp_connect_fail", {"server": name, "error": error_msg})
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
            sys_logger.log("system", "mcp_tool_execution_fail", {"tool": tool_name, "error": str(e)})
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
                sys_logger.log("system", "mcp_cleanup", {"status": "success"})
            except Exception as e:
                sys_logger.log("system", "mcp_cleanup_fail", {"error": str(e)})
                pass