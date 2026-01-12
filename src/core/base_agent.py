from abc import ABC
from typing import List, Dict, Callable
import json
import re
from pathlib import Path
from src.core.provider import AIProvider
from src.tools import TOOL_REGISTRY, get_tools_schema
from src.config import GlobalSettings
from src.core.mcp_manager import MCPManager
from src.utils import get_system_context, extract_json_from_text
from src.core.memory import memory_core

class BaseAgent(ABC):
    def __init__(self, name: str, provider: AIProvider, skill_name: str, settings: GlobalSettings):
        self.name = name
        self.provider = provider
        self.skill_name = skill_name
        self.settings = settings
        self.system_prompt = self._load_skill()
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

    def _load_skill(self) -> str:
        try:
            from src.skills_loader import load_skill
            content = load_skill(self.skill_name)
            if content: return content
        except:
            pass
        return f"You are {self.name}."

    def load_tools(self, tool_names: List[str]):
        for name in tool_names:
            if name in TOOL_REGISTRY:
                self.tools[name] = TOOL_REGISTRY[name]

    def _trim_history(self):
            limit = max(self.settings.history_limit, 50)
            if len(self.history) > limit + 1:
                self.history = [self.history[0]] + self.history[-limit:]

    async def run(self, task: str) -> str:
        await self._ensure_mcp()

        if self.system_prompt.startswith("You are") and self.settings.debug_mode:
            console.print(f"[yellow]⚠️ Warning: Skill file '{self.skill_name}.md' not found. Using fallback.[/yellow]")

        self.history.append({"role": "user", "content": f"TASK: {task}"})
        memory_core.log_event(f"agent:{self.name}", f"Started task: {task}", "subtask_start")
        step = 0
        context = get_system_context()
        memories = memory_core.get_recent_memories(limit=5)
        
        local_desc = get_tools_schema()
        mcp_desc = self.mcp.get_tools_schema() if self.mcp_initialized else "None"
        all_tools = f"{local_desc}\n{mcp_desc}" if mcp_desc else local_desc

        full_prompt = (
            f"{context}\n\n"
            f"{self.system_prompt}\n\n"
            f"--- MEMORY ---\n{memories}\n\n"
            f"--- TOOLKIT ---\n{all_tools}\n\n"
            f"--- PROTOCOL ---\n"
            f"1. Output ONLY JSON for actions.\n"
            f"2. Format: {{\"tool\": \"name\", \"args\": {{...}}}}\n"
            f"3. Output text to answer, summarize, or delegate."
        )

        for _ in range(20):
            self._trim_history()
            
            try:
                response = await self.provider.generate_text(self.history, full_prompt)
            except Exception as e:
                error_msg = f"Agent Brain Error: {e}"
                memory_core.log_event(f"agent:{self.name}", error_msg, "error")
                return error_msg

            json_str = extract_json_from_text(response)
            if self.settings.debug_mode and json_str:
                console.print(f"\n[dim cyan][{self.name}] THOUGHT: {json_str}[/dim cyan]")

            if not json_str:
                self.history.append({"role": "assistant", "content": response})
                memory_core.log_event(f"agent:{self.name}", response, "final_answer")
                return response

            try:
                data = json.loads(json_str)
                tool_name = data.get("tool")
                args = data.get("args", {})
                
                result = ""
                
                # --- TOOL DISPATCHER ---
                if tool_name in TOOL_REGISTRY:
                    import inspect
                    func = TOOL_REGISTRY[tool_name]
                    if inspect.iscoroutinefunction(func):
                        result = await func(**args)
                    else:
                        result = func(**args)
                
                elif self.mcp_initialized and tool_name in self.mcp.tools_map:
                    result = await self.mcp.execute_tool(tool_name, args)
                
                else:
                    result = f"Error: Tool {tool_name} not found."

                if len(str(result)) > 4000:
                    result = str(result)[:4000] + "... [TRUNCATED]"

                if not self.settings.debug_mode:
                    print(f"\n[Step {step+1}] Executed {tool_name}...")

                self.history.append({"role": "assistant", "content": json_str})
                self.history.append({"role": "user", "content": f"TOOL RESULT: {result}"})
                
                memory_core.log_event(f"agent:{self.name}", {"tool": tool_name, "result": result}, "tool_execution")
                
            except Exception as e:
                self.history.append({"role": "user", "content": f"Error: {e}"})
                memory_core.log_event(f"agent:{self.name}", str(e), "tool_error")

        return "Agent stopped: Max steps reached."
