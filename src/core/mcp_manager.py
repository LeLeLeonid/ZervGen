import asyncio
import json
import logging
import os
import shutil
import sys
from typing import Any, Dict, List, Optional
from src.config import GlobalSettings, MCPServerConfig

logger = logging.getLogger(__name__)


class MCPServer:
    def __init__(self, name: str, config: MCPServerConfig, startup_delay: float = 2.0):
        self.name = name
        self.config = config
        self.startup_delay = startup_delay
        self.process: Optional[asyncio.subprocess.Process] = None
        self.tools: Dict[str, Dict] = {}
        self._request_id = 0
        self._connected = False
        self._buffer = ""
    
    async def start(self) -> bool:
        if not self.config.enabled:
            return False
        
        cmd = self.config.command
        
        if cmd == "internal":
            self._connected = True
            logger.info(f"MCP {self.name}: Internal tools available")
            return True
        
        cmd_path = shutil.which(cmd) or shutil.which(f"{cmd}.exe") or shutil.which(f"{cmd}.cmd")
        if not cmd_path:
            logger.warning(f"MCP {self.name}: {cmd} not found")
            return False
        
        try:
            full_cmd = [cmd] + self.config.args
            
            self.process = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**dict(os.environ), **self.config.env}
            )
            
            await asyncio.sleep(self.startup_delay)
            
            if self.process.returncode is not None:
                logger.error(f"MCP {self.name}: Process exited")
                return False
            
            result = await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ZervGen", "version": "1.0.0"}
            })
            
            if not result:
                logger.error(f"MCP {self.name}: Init failed")
                return False
            
            await self._send_notification("notifications/initialized", {})
            
            tools_result = await self._send_request("tools/list", {})
            if tools_result and "tools" in tools_result:
                for tool in tools_result["tools"]:
                    self.tools[tool["name"]] = tool
                logger.info(f"MCP {self.name}: {len(self.tools)} tools")
            
            self._connected = True
            return True
            
        except Exception as e:
            logger.error(f"MCP {self.name}: Start error - {e}")
            return False
    
    def _encode_message(self, data: Dict) -> bytes:
        content = json.dumps(data, separators=(',', ':'))
        return f"Content-Length: {len(content)}\r\n\r\n{content}".encode()
    
    async def _read_response(self) -> Optional[Dict]:
        if not self.process or not self.process.stdout:
            return None
        
        try:
            content_length = 0
            while True:
                line = await asyncio.wait_for(
                    self.process.stdout.readline(),
                    timeout=60.0
                )
                line = line.decode().strip()
                if not line:
                    break
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
            
            if content_length == 0:
                return None
            
            content = await asyncio.wait_for(
                self.process.stdout.read(content_length),
                timeout=60.0
            )
            
            return json.loads(content.decode())
            
        except asyncio.TimeoutError:
            self._buffer = ""
            return None
        except Exception as e:
            logger.error(f"MCP {self.name}: Read error - {e}")
            self._buffer = ""
            return None
    
    async def _send_request(self, method: str, params: Dict) -> Optional[Dict]:
        if not self.process or not self.process.stdin:
            return None
        
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params
        }
        
        try:
            self.process.stdin.write(self._encode_message(request))
            await self.process.stdin.drain()
            
            response = await self._read_response()
            if response and "result" in response:
                return response["result"]
            if response and "error" in response:
                logger.error(f"MCP {self.name}: Error - {response['error']}")
            return None
            
        except Exception as e:
            logger.error(f"MCP {self.name}: Request error - {e}")
            return None
    
    async def _send_notification(self, method: str, params: Dict) -> None:
        if not self.process or not self.process.stdin:
            return
        
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        
        try:
            self.process.stdin.write(self._encode_message(notification))
            await self.process.stdin.drain()
        except Exception:
            pass
    
    async def execute(self, tool_name: str, arguments: Dict) -> str:
        if not self._connected:
            return f"Error: {self.name} not connected"
        
        if tool_name not in self.tools:
            return f"Error: Tool {tool_name} not found"
        
        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })
        
        if result and "content" in result:
            for item in result["content"]:
                if item.get("type") == "text":
                    return item.get("text", "")
            return str(result["content"])
        return "Error: No response"
    
    async def stop(self) -> None:
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except Exception:
                self.process.kill()
            self.process = None
        self._connected = False


class MCPManager:
    def __init__(self, settings: GlobalSettings):
        self.settings = settings
        self.servers: Dict[str, MCPServer] = {}
        self.tools_map: Dict[str, str] = {}
    
    async def connect_all(self) -> Dict[str, bool]:
        results = {}
        
        for name, config in self.settings.mcp_servers.items():
            if not config.enabled:
                continue
            
            server = MCPServer(name, config, startup_delay=self.settings.mcp_startup_delay)
            success = await server.start()
            results[name] = success
            
            if success:
                self.servers[name] = server
                for tool_name in server.tools:
                    self.tools_map[tool_name] = name
        
        return results
    
    async def execute_tool(self, tool_name: str, arguments: Dict) -> str:
        if tool_name not in self.tools_map:
            return f"Error: Unknown tool {tool_name}"
        
        server_name = self.tools_map[tool_name]
        server = self.servers.get(server_name)
        
        if not server:
            return f"Error: Server {server_name} not connected"
        
        return await server.execute(tool_name, arguments)
    
    def get_available_tools(self) -> List[str]:
        return list(self.tools_map.keys())
    
    async def cleanup(self) -> None:
        for server in self.servers.values():
            await server.stop()
        self.servers.clear()
        self.tools_map.clear()
