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
            # await self._ensure_mcp()
            
            self.history.append({"role": "user", "content": user_input})
            memory_core.log_event("user", user_input, "input")
            
            step = 0
            self.last_title = "Thinking..." # Default start status
            last_json = None
            
            while step < self.max_steps:
                self._trim_history()
                system_prompt = self._build_system_prompt()
                response_text = ""
                try:
                    with console.status(f"[bold purple]{self.last_title}[/bold purple]", spinner="dots"):
                        response_text = await self.brain.generate_text(self.history, system_prompt)
                        from src.utils import print_token_usage
                        print_token_usage(self.history + [{"content": system_prompt}], response_text)
                except Exception as e:
                    return f"Critical Brain Failure: {e}"

                json_str = extract_json_from_text(response_text)

                if json_str and json_str == last_json:
                    self.history.append({"role": "user", "content": "SYSTEM: Loop detected. Change arguments."})
                    step += 1
                    continue
                if json_str: last_json = json_str

                if not json_str:
                    self.history.append({"role": "assistant", "content": response_text})
                    memory_core.log_event("assistant", response_text, "text_response")
                    return response_text

                try:
                    data = json.loads(json_str)
                    thoughts = data.get("thoughts", [])
                    title = data.get("title", "Processing...")
                    tool_name = data.get("tool")
                    args = data.get("args", {})
                    
                    self.last_title = title

                    if self.settings.debug_mode:
                        thought_text = "\n".join([f"- {t}" for t in thoughts])
                        console.print(Panel(thought_text, title=f"[dim]🧠 THOUGHTS: {title}[/dim]", border_style="dim cyan"))
                    else:
                        console.print(f"[dim purple]→ {title}[/dim purple]")

                    if tool_name == "response":
                        final_text = args.get("text", "")
                        self.history.append({"role": "assistant", "content": json_str})
                        memory_core.log_event("assistant", final_text, "response")
                        return final_text

                    if self.settings.require_approval:
                        console.print(f"[bold yellow]✋ Action: {tool_name}[/bold yellow]")
                        if not Confirm.ask("Execute?"):
                            self.history.append({"role": "user", "content": f"SYSTEM: User denied {tool_name}"})
                            continue

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

                    observation = f"TOOL_OUTPUT ({tool_name}): {result}"
                    
                    self.history.append({"role": "assistant", "content": json_str})
                    self.history.append({"role": "user", "content": observation})
                    
                    memory_core.log_event("system", {"tool": tool_name, "result": result}, "tool_execution")
                    step += 1

                except json.JSONDecodeError:
                    self.history.append({"role": "user", "content": "SYSTEM ERROR: Invalid JSON."})
                except Exception as e:
                    self.history.append({"role": "user", "content": f"SYSTEM ERROR: {e}"})

            return "Max steps reached."