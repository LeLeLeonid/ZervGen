import json
import asyncio
from pathlib import Path
from rich.prompt import Confirm
from rich.console import Console
from rich.panel import Panel
from src.core.provider import AIProvider
from src.config import GlobalSettings
from src.providers.pollinations import PollinationsProvider
from src.tools import TOOL_REGISTRY, get_tools_schema, download_and_open_image, extract_json_from_text
from src.utils import get_system_context, sanitize_for_prompt, generate_random_delimiter
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
        self._mcp_lock = asyncio.Lock()
        self.current_role = "system"
        self.current_mode = settings.mode
        self._current_worker = None
        self.interrupt_event = asyncio.Event()

    def request_interrupt(self):
        """Signal a graceful interrupt request."""
        self.interrupt_event.set()

    def _cleanup_worker(self):
        """Centralized cleanup of current worker to prevent state leakage."""
        if self._current_worker is not None:
            self._current_worker = None

    async def _ensure_mcp(self):
        """Initialize MCP with lock to prevent race conditions."""
        if self.mcp_initialized or not self.settings.mcp_enabled:
            return

        async with self._mcp_lock:
            if self.mcp_initialized:
                return
            try:
                await self.mcp.connect_all()
                self.mcp_initialized = True
            except Exception:
                pass

    def set_role(self, role: str) -> bool:
        if load_role(role):
            self._cleanup_worker()
            self.current_role = role
            return True
        return False

    def set_mode(self, mode: str) -> bool:
        from src.config import MODES
        if mode.upper() in MODES:
            self._cleanup_worker()
            self.current_mode = mode.upper()
            return True
        return False

    def _build_system_prompt(self, role_cfg) -> str:
        from src.config import MODES
        from src.core.memory import todo_manager
        from src.tools import TOOL_REGISTRY
        import inspect
        
        context = get_system_context()
        roles_info = get_roles_overview()
        memories = memory_core.get_recent_memories(limit=5)
        mode_def = MODES.get(self.current_mode, MODES["BUILD"])
        todo_overview = todo_manager.get_orchestrator_view()
        
        # Filter tools based on YAML frontmatter (like BaseAgent does)
        if role_cfg.tools:
            filtered_tools = {
                k: v for k, v in TOOL_REGISTRY.items()
                if k in role_cfg.tools or k == "response"
            }
        else:
            filtered_tools = TOOL_REGISTRY
        
        # Build tools schema for prompt
        tools_schema = []
        for name, func in filtered_tools.items():
            sig = str(inspect.signature(func)).replace(" -> str", "")
            doc = inspect.getdoc(func) or "Tool."
            # Get first line of doc only for brevity
            doc_short = doc.split('\n')[0][:80]
            tools_schema.append(f"- {name}{sig}: {doc_short}")
        
        tools_section = "\n".join(tools_schema)
        
        return (
            f"{role_cfg.prompt}\n\n"
            f"=== OP STATE ===\n"
            f"ROLE: {self.current_role}\n"
            f"--- CONTEXT ---\n{context}\n"
            f"--- MEMORY ---\n{memories}\n"
            f"--- TODO ---\n{todo_overview}\n"
            f"--- ROLES ---\n{roles_info}\n"
            f"--- AVAILABLE TOOLS ---\n{tools_section}"
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
        sanitized_input = sanitize_for_prompt(user_input)
        self.history.append({"role": "user", "content": sanitized_input})
        memory_core.log_event("user", sanitized_input, "input")
        
        step = 0
        self.last_title = "Processing..."

        while step < self.max_steps:
            if self.interrupt_event.is_set():
                self.interrupt_event.clear()
                return "⏹️ Operation interrupted by user"

            self._trim_history()
            role_cfg = load_role(self.current_role) or load_role("system")
            system_prompt = self._build_system_prompt(role_cfg)

            from src.config import MODES
            mode_def = MODES.get(self.current_mode, MODES["BUILD"])

            delim_focus = generate_random_delimiter(16)
            full_prompt = f"{system_prompt}\n\n{delim_focus}\nCURRENT FOCUS: {mode_def['prompt']}\n{delim_focus}"

            response_text = ""
            try:
                # Output full prompt for inspection
                if self.settings.debug_mode or getattr(self.settings, 'show_prompts', False):
                    from rich.panel import Panel
                    from rich.syntax import Syntax
                    console.print(Panel(
                        Syntax(full_prompt, "markdown", theme="monokai", line_numbers=True),
                        title="[bold cyan]FULL PROMPT SENT TO LLM[/bold cyan]",
                        border_style="cyan"
                    ))
                with console.status(f"[bold purple]Step {step}/{self.max_steps}: {self.last_title}[/bold purple]", spinner="dots"):
                    response_text = await self.brain.generate_text(self.history, full_prompt)
                    from src.utils import print_token_usage
                    print_token_usage(self.history + [{"content": full_prompt}], response_text)
                
                # Output LLM response in debug mode
                if self.settings.debug_mode or getattr(self.settings, 'show_prompts', False):
                    console.print(Panel(
                        Syntax(response_text, "json", theme="monokai", line_numbers=False),
                        title="[bold green]LLM OUTPUT[/bold green]",
                        border_style="green"
                    ))
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
                    console.print(Panel("\n".join(thoughts), title=f"[dim]🧠 Step {step}/{self.max_steps}: {self.last_title}[/dim]", border_style="dim cyan"))
                else:
                    console.print(f"[dim purple]→ Step {step}/{self.max_steps}: {self.last_title}[/dim purple]")

                if tool_name == "delegate_to":
                    sub_agent_name = args.get("agent_name")
                    sub_task = args.get("task")
                    console.print(f"[bold yellow]➜ DELEGATING to {sub_agent_name}: {sub_task}[/bold yellow]")
                    sub_worker = self._spawn_agent(sub_agent_name.lower())
                    sub_result = await sub_worker.run(sub_task)
                    self.history.append({"role": "assistant", "content": json_str})
                    self.history.append({"role": "user", "content": f"SUB-AGENT RESULT: {sub_result}"})
                    step += 1
                    continue

                if tool_name == "manage_history":
                    action = args.get("action")
                    if action == "delete_last":
                        # Safely remove last 2 entries
                        for _ in range(2):
                            if len(self.history) > 1:
                                self.history.pop()
                        console.print("[dim]Context Pruned by Agent[/dim]")
                    continue

                if tool_name == "set_state":
                    if args.get("role"): self.set_role(args["role"])
                    if args.get("mode"): self.set_mode(args["mode"])
                    self._cleanup_worker()
                    self.history.append({"role": "system", "content": f"STATE UPDATED"})
                    continue

                if tool_name == "response" or tool_name is None:
                    final_text = args.get("text") or args.get("content") or response_text
                    self.history.append({"role": "assistant", "content": json_str})
                    return str(final_text)

                result = ""
                if self._current_worker is None:
                    self._current_worker = self._spawn_agent(self.current_role)

                console.print(f"[dim cyan]  Step {step}/{self.max_steps}: Executing tool '{tool_name}'...[/dim cyan]")

                if tool_name in self._current_worker.tools:
                    func = self._current_worker.tools[tool_name]
                    import inspect
                    result = await func(**args) if inspect.iscoroutinefunction(func) else func(**args)
                elif self.settings.mcp_enabled and tool_name in self.mcp.tools_map:
                    result = await self.mcp.execute_tool(tool_name, args)
                else:
                    result = f"Error: Tool '{tool_name}' not found."

                self.history.append({"role": "assistant", "content": json_str})
                self.history.append({"role": "user", "content": f"OBSERVATION: {str(result)[:5000]}"})
                memory_core.log_event("system", {"tool": tool_name, "result": result}, "tool_execution")
                step += 1

            except Exception as e:
                self.history.append({"role": "user", "content": f"SYSTEM ERROR: {e}"})
                step += 1

        return "Max steps reached."