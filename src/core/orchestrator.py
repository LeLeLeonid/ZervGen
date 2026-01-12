import json
import re
import platform
import subprocess
import time
import random
from pathlib import Path
from rich.prompt import Confirm
from rich.console import Console
from src.core.provider import AIProvider
from src.config import GlobalSettings
from src.providers.pollinations import PollinationsProvider
from src.tools import TOOL_REGISTRY, get_tools_schema, download_and_open_image, extract_json_from_text
from src.utils import get_system_context
from src.core.mcp_manager import MCPManager
from src.core.memory import memory_core
from src.skills_loader import load_skill, get_skills_schema

console = Console()

class Orchestrator:
    def __init__(self, provider: AIProvider, settings: GlobalSettings):
        self.brain = provider
        self.settings = settings
        self.history = []
        self.max_steps = self.settings.max_steps
        self.mcp = MCPManager(self.settings)
        self.mcp_initialized = False

    async def _ensure_mcp(self):
        if not self.mcp_initialized and self.settings.mcp_enabled:
            try:
                await self.mcp.connect_all()
                self.mcp_initialized = True
            except Exception:
                pass

    def set_mode(self, mode: str) -> bool:
        path = Path(f"src/skills/{mode}.md")
        if path.exists():
            self.current_mode = mode
            return True
        return False

    def _build_system_prompt(self) -> str:
        base_prompt = load_skill("system")
        if not base_prompt:
            base_prompt = "You are ZervGen Supervisor. Route tasks via JSON."

        context = get_system_context()
        skills_info = get_skills_schema()
        
        local_tools = get_tools_schema()
        mcp_tools = self.mcp.get_tools_schema() if self.settings.mcp_enabled else ""
        memories = memory_core.get_recent_memories(limit=5)
        
        all_tools = f"{local_tools}\n{mcp_tools}" if mcp_tools else local_tools

        full_prompt = (
            f"{context}\n\n"
            f"{base_prompt}\n\n"
            f"--- MEMORY ---\n{memories}\n\n"
            f"--- SKILLS & AGENTS ---\n{skills_info}\n\n"
            f"--- TOOLKIT ---\n{all_tools}"
        )
        return full_prompt
        
    def _trim_history(self):
        limit = max(self.settings.history_limit, 50)
        if len(self.history) > limit:
            self.history = [self.history[0]] + self.history[-limit:]

    async def process(self, user_input: str) -> str:
            await self._ensure_mcp()
            
            self.history.append({"role": "user", "content": user_input})
            memory_core.log_event("user", user_input, "input")
            
            step = 0
            last_json = None
            
            while step < self.max_steps:
                self._trim_history()
                system_prompt = self._build_system_prompt()

                response = ""
                try:
                    from src.cli import LOADING_PHRASES
                    with console.status(f"[bold purple]{random.choice(LOADING_PHRASES)}[/bold purple]", spinner="dots"):
                        response = await self.brain.generate_text(self.history, system_prompt)
                except Exception as e:
                    return f"Critical Brain Failure: {e}"

                json_str = extract_json_from_text(response)
                
                if json_str and json_str == last_json:
                    self.history.append({"role": "user", "content": "SYSTEM: Loop detected. Change arguments."})
                    step += 1
                    continue
                
                if json_str: last_json = json_str

                if self.settings.debug_mode and json_str:
                    console.print(f"\n[dim cyan]THOUGHT: {json_str}[/dim cyan]")

                text_part = re.sub(r'```json.*?```', '', response, flags=re.DOTALL).strip()
                if json_str:
                    text_part = text_part.replace(json_str, "").strip()

                if json_str:
                    try:
                        action_data = json.loads(json_str)
                        tool_name = action_data.get("tool")
                        args = action_data.get("args", {})
                        
                        if self.settings.require_approval:
                            console.print(f"\n[bold yellow]✋ APPROVAL REQUIRED[/bold yellow]")
                            console.print(f"Tool: [cyan]{tool_name}[/cyan]")
                            console.print(f"Args: [dim]{args}[/dim]")

                            if not Confirm.ask("Execute?"):
                                self.history.append({"role": "user", "content": f"SYSTEM: User denied {tool_name}"})
                                console.print("[red]Action Denied.[/red]")
                                continue # Skip execution, let model retry

                        result = ""

                        # --- TOOL DISPATCHER ---
                        if tool_name in TOOL_REGISTRY:
                            func = TOOL_REGISTRY[tool_name]
                            import inspect
                            if inspect.iscoroutinefunction(func):
                                result = await func(**args)
                            else:
                                result = func(**args)
                        
                        elif self.settings.mcp_enabled and tool_name in self.mcp.tools_map:
                            result = await self.mcp.execute_tool(tool_name, args)
                        
                        else:
                            result = f"Error: Tool '{tool_name}' not found."

                        if len(str(result)) > 5000:
                            result = str(result)[:5000] + "... [TRUNCATED]"

                        observation = f"\nTool: {tool_name}\nResult: {result}"
                        
                        if not self.settings.debug_mode:
                            print(f"\n[Step {step+1}] Executed {tool_name}...")
                        
                        self.history.append({"role": "assistant", "content": response})
                        self.history.append({"role": "user", "content": observation})
                        
                        memory_core.log_event("system", {"tool": tool_name, "args": args, "result": result}, "tool_execution")

                    except json.JSONDecodeError:
                        self.history.append({"role": "user", "content": "SYSTEM: Invalid JSON."})
                        memory_core.log_event("system", "Invalid JSON", "error")
                    except Exception as e:
                        self.history.append({"role": "user", "content": f"SYSTEM: Tool Error: {e}"})
                        memory_core.log_event("system", str(e), "error")

                if not json_str:
                    self.history.append({"role": "assistant", "content": response})
                    memory_core.log_event("assistant", response, "final_answer")
                    return response

                step += 1

            return "Max steps reached."