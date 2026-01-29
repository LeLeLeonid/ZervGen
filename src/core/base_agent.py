from abc import ABC
from typing import List, Dict, Callable
import json
import re
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from src.core.provider import AIProvider
from src.tools import TOOL_REGISTRY, get_tools_schema
from src.config import GlobalSettings
from src.core.mcp_manager import MCPManager
from src.utils import get_system_context, extract_json_from_text, sanitize_for_prompt
from src.core.memory import memory_core

console = Console()

class BaseAgent(ABC):
    def __init__(self, name: str, provider: AIProvider, skill_name: str, settings: GlobalSettings):
        self.name = name
        self.provider = provider
        self.skill_name = skill_name
        self.settings = settings
        self.system_prompt = "You are a ZervGen Agent."
        self.tools: Dict[str, Callable] = {}
        self.history: List[Dict] = []
        self.mcp = MCPManager(self.settings) if self.settings.mcp_enabled else None
        self.mcp_initialized = False

    async def _ensure_mcp(self):
        if self.mcp and not self.mcp_initialized:
            try:
                await self.mcp.connect_all()
                self.mcp_initialized = True
            except:
                pass

    def load_tools(self, tool_names: List[str]):
        for name in tool_names:
            if name in TOOL_REGISTRY:
                self.tools[name] = TOOL_REGISTRY[name]

    def _trim_history(self):
            limit = max(self.settings.history_limit, 50)
            if len(self.history) > limit + 1:
                self.history = [self.history[0]] + self.history[-limit:]

    async def run(self, task: str) -> str:
        if self.system_prompt == "You are a ZervGen Agent." and self.settings.debug_mode:
            console.print(f"[yellow]⚠️ Warning: Agent {self.name} has default prompt.[/yellow]")

        sanitized_task = sanitize_for_prompt(task)
        self.history.append({"role": "user", "content": f"TASK: {sanitized_task}"})
        memory_core.log_event(f"agent:{self.name}", f"Started task: {sanitized_task}", "subtask_start")
        step = 0
        last_json = None
        context = get_system_context()
        memories = memory_core.get_recent_memories(limit=5)
        
        allowed_tools_schema = []
        for name, func in self.tools.items():
            import inspect
            sig = str(inspect.signature(func)).replace(" -> str", "")
            doc = inspect.getdoc(func) or "Tool."
            allowed_tools_schema.append(f"- {name}{sig}: {doc}")
        
        local_desc = "\n".join(allowed_tools_schema)
        mcp_desc = ""
        if self.mcp and self.mcp_initialized:
            try:
                mcp_desc = self.mcp.get_tools_schema()
            except:
                mcp_desc = "MCP tools unavailable"
        all_tools = f"{local_desc}\n\n--- MCP TOOLS ---\n{mcp_desc}" if mcp_desc else local_desc

        full_prompt = (
            f"{context}\n\n"
            f"{self.system_prompt}\n\n"
            f"--- MEMORY ---\n{memories}\n\n"
            f"--- TOOLKIT ---\n{all_tools}\n\n"
            f"--- PROTOCOL ---\n"
            f"1. THOUGHT: Plan step-by-step in 'thoughts' array.\n"
            f"2. ACTION: Output strict JSON object.\n"
            f"3. SCHEMA: {{\n"
            f"  \"thoughts\": [\"...\", \"...\"],\n"
            f"  \"title\": \"Short status...\",\n"
            f"  \"tool\": \"tool_name\",\n"
            f"  \"args\": {{ \"arg\": \"val\" }}\n"
            f"}}\n"
            f"4. FINAL: Use 'response' tool to finish."
        )

        for _ in range(20):
            self._trim_history()
            
            # Output full prompt for inspection
            if self.settings.debug_mode or getattr(self.settings, 'show_prompts', False):
                from rich.panel import Panel
                from rich.syntax import Syntax
                console.print(Panel(
                    Syntax(full_prompt, "markdown", theme="monokai", line_numbers=True),
                    title=f"[bold cyan]FULL PROMPT SENT TO LLM ({self.name})[/bold cyan]",
                    border_style="cyan"
                ))
            
            try:
                response_text = await self.provider.generate_text(self.history, full_prompt)
                from src.utils import print_token_usage
                print_token_usage(self.history + [{"content": full_prompt}], response_text)
                
                # Output LLM response in debug mode
                if self.settings.debug_mode or getattr(self.settings, 'show_prompts', False):
                    console.print(Panel(
                        Syntax(response_text, "json", theme="monokai", line_numbers=False),
                        title=f"[bold green]LLM OUTPUT ({self.name})[/bold green]",
                        border_style="green"
                    ))
            except Exception as e:
                error_msg = f"Agent Brain Error: {e}"
                memory_core.log_event(f"agent:{self.name}", error_msg, "error")
                return error_msg

            json_str = extract_json_from_text(response_text)

            if json_str and json_str == last_json:
                self.history.append({"role": "user", "content": "SYSTEM: Loop detected. Change arguments."})
                step += 1
                continue
            if json_str: last_json = json_str

            if not json_str:
                self.history.append({"role": "assistant", "content": response_text})
                memory_core.log_event(f"agent:{self.name}", response_text, "final_answer")
                return response_text

            try:
                data = json.loads(json_str)
                thoughts = data.get("thoughts", [])
                title = data.get("title", "Working...")
                tool_name = data.get("tool")
                args = data.get("args", {})

                if self.settings.debug_mode:
                    thought_text = "\n".join([f"- {t}" for t in thoughts])
                    console.print(Panel(thought_text, title=f"[dim]🧠 [{self.name}] Step {step}/20: {title}[/dim]", border_style="dim magenta"))
                else:
                    console.print(f"[dim magenta]  ↳ [{self.name}] Step {step}/20: {title}[/dim magenta]")
                
                if tool_name == "response" or tool_name is None:
                    final_text = args.get("text") or args.get("content") or args.get("answer") or args.get("response")
                    if not final_text and isinstance(data.get("args"), str):
                        final_text = data["args"]
                    if not final_text:
                        final_text = ". ".join(thoughts) if thoughts else response_text
                    self.history.append({"role": "assistant", "content": json_str})
                    return str(final_text)
                
                step += 1
                result = ""

                if tool_name in TOOL_REGISTRY:
                    import inspect
                    func = TOOL_REGISTRY[tool_name]
                    if inspect.iscoroutinefunction(func):
                        result = await func(**args)
                    else:
                        result = func(**args)
                elif self.mcp and self.mcp_initialized and tool_name in self.mcp.tools_map:
                    result = await self.mcp.execute_tool(tool_name, args)
                else:
                    result = f"Error: Tool {tool_name} not found."

                if len(str(result)) > 4000:
                    result = str(result)[:4000] + "... [TRUNCATED]"

                self.history.append({"role": "assistant", "content": json_str})
                self.history.append({"role": "user", "content": f"TOOL RESULT: {result}"})
                
                memory_core.log_event(f"agent:{self.name}", {"tool": tool_name, "result": result}, "tool_execution")
                
            except Exception as e:
                self.history.append({"role": "user", "content": f"Error: {e}"})
                memory_core.log_event(f"agent:{self.name}", str(e), "tool_error")
                step += 1

        return "Agent stopped: Max steps reached."