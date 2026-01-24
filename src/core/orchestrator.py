import json
from pathlib import Path
from rich.prompt import Confirm
from rich.console import Console
from rich.panel import Panel
from src.core.provider import AIProvider
from src.config import GlobalSettings
from src.providers.pollinations import PollinationsProvider
from src.tools import TOOL_REGISTRY, get_tools_schema, download_and_open_image, extract_json_from_text
from src.utils import get_system_context
from src.core.mcp_manager import MCPManager
from src.core.memory import memory_core
from src.skills_loader import load_role, get_all_roles, get_roles_overview
from src.core.base_agent import BaseAgent

console = Console()

class Orchestrator:
    def __init__(self, provider: AIProvider, settings: GlobalSettings):
        self.brain = provider
        self.settings = settings
        self.history = []
        self.max_steps = self.settings.max_steps
        self.mcp = MCPManager(self.settings)
        self.mcp_initialized = False
        self.current_role = "system"
        self.current_mode = settings.mode

    async def _ensure_mcp(self):
        if not self.mcp_initialized and self.settings.mcp_enabled:
            try:
                await self.mcp.connect_all()
                self.mcp_initialized = True
            except Exception:
                pass

    def set_role(self, role: str) -> bool:
        if load_role(role):
            self.current_role = role
            return True
        return False

    def set_mode(self, mode: str) -> bool:
        from src.config import MODES
        if mode.upper() in MODES:
            self.current_mode = mode.upper()
            return True
        return False

    def _build_system_prompt(self, role_cfg) -> str:
        from src.config import MODES
        context = get_system_context()
        roles_info = get_roles_overview()
        memories = memory_core.get_recent_memories(limit=5)
        mode_def = MODES.get(self.current_mode, MODES["BUILD"])
        
        return (
            f"{role_cfg.prompt}\n\n"
            f"=== OP STATE ===\n"
            f"ROLE: {self.current_role}\n"
            f"BEHAVIOR: {mode_def['prompt']}\n\n"
            f"--- CONTEXT ---\n{context}\n"
            f"--- MEMORY ---\n{memories}\n"
            f"--- ROLES ---\n{roles_info}"
        )
        
    def _trim_history(self):
        limit = max(self.settings.history_limit, 50)
        if len(self.history) > limit:
            self.history = [self.history[0]] + self.history[-limit:]

    def _spawn_agent(self, role_name: str) -> BaseAgent:
        target_role = role_name if role_name != "system" else self.current_role
        
        role_config = load_role(target_role)
        if not role_config:
            role_config = load_role("system")
        
        worker = BaseAgent(
            name=role_config.name.capitalize(),
            provider=self.brain,
            skill_name=target_role,
            settings=self.settings
        )
        
        from src.config import MODES
        current_mode_def = MODES.get(self.current_mode, MODES["BUILD"])
        mode_prompt = current_mode_def["prompt"]
        
        worker.system_prompt = (
            f"{role_config.prompt}\n\n"
            f"=== OPERATIONAL MODE: {self.current_mode} ===\n"
            f"{mode_prompt}"
        )
        
        if role_config.tools:
            worker.tools = {
                k: v for k, v in TOOL_REGISTRY.items()
                if k in role_config.tools or k == "response"
            }
        else:
            worker.tools = TOOL_REGISTRY
        
        return worker


    async def process(self, user_input: str) -> str:
        self.history.append({"role": "user", "content": user_input})
        memory_core.log_event("user", user_input, "input")
        
        step = 0
        self.last_title = "Processing..."

        while step < self.max_steps:
            self._trim_history()
            
            role_cfg = load_role(self.current_role) or load_role("system")
            system_prompt = self._build_system_prompt(role_cfg)
            
            from src.config import MODES
            mode_def = MODES.get(self.current_mode, MODES["BUILD"])
            full_prompt = f"{system_prompt}\n\n=== CURRENT FOCUS: {mode_def['prompt']} ==="

            response_text = ""
            try:
                with console.status(f"[bold purple]{self.last_title}[/bold purple]", spinner="dots"):
                    response_text = await self.brain.generate_text(self.history, full_prompt)
                    from src.utils import print_token_usage
                    print_token_usage(self.history + [{"content": full_prompt}], response_text)
            except Exception as e:
                return f"Critical Brain Failure: {e}"

            json_str = extract_json_from_text(response_text)

            if not json_str:
                self.history.append({"role": "assistant", "content": response_text})
                return response_text

            try:
                data = json.loads(json_str)
                thoughts = data.get("thoughts", [])
                self.last_title = data.get("title", "Thinking...")
                tool_name = data.get("tool")
                args = data.get("args", {})

                if self.settings.debug_mode:
                    console.print(Panel("\n".join(thoughts), title=f"[dim]🧠 {self.last_title}[/dim]", border_style="dim cyan"))
                else:
                    console.print(f"[dim purple]→ {self.last_title}[/dim purple]")

                if tool_name == "set_state":
                    new_role = args.get("role")
                    new_mode = args.get("mode")
                    if new_role: self.set_role(new_role)
                    if new_mode: self.set_mode(new_mode)
                    
                    if hasattr(self, 'current_worker'): delattr(self, 'current_worker')
                    
                    self.history.append({"role": "system", "content": f"STATE UPDATED: Role={new_role}, Mode={new_mode}"})
                    continue

                if tool_name == "response" or tool_name is None:
                    final_text = args.get("text") or args.get("content") or response_text
                    self.history.append({"role": "assistant", "content": json_str})
                    return str(final_text)

                result = ""
                
                if not hasattr(self, 'current_worker'):
                    self.current_worker = self._spawn_agent(self.current_role)
                
                if tool_name in self.current_worker.tools:
                    func = self.current_worker.tools[tool_name]
                    import inspect
                    result = await func(**args) if inspect.iscoroutinefunction(func) else func(**args)
                elif self.settings.mcp_enabled and tool_name in self.mcp.tools_map:
                    result = await self.mcp.execute_tool(tool_name, args)
                else:
                    result = f"Error: Tool '{tool_name}' not found or permission denied for role '{self.current_role}'."

                self.history.append({"role": "assistant", "content": json_str})
                self.history.append({"role": "user", "content": f"OBSERVATION: {str(result)[:5000]}"})
                memory_core.log_event("system", {"tool": tool_name, "result": result}, "tool_execution")
                
                step += 1

            except Exception as e:
                self.history.append({"role": "user", "content": f"SYSTEM ERROR: {e}"})
                step += 1

        return "Max steps reached."