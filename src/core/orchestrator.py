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

    def _build_system_prompt(self) -> str:
        base_prompt = load_role("system")
        if not base_prompt:
            base_prompt = "You are ZervGen Supervisor. Route tasks via JSON."

        context = get_system_context()
        skills_info = get_roles_overview()
        
        memories = memory_core.get_recent_memories(limit=5)

        full_prompt = (
            f"{context}\n\n"
            f"{base_prompt}\n\n"
            f"--- MEMORY ---\n{memories}\n\n"
            f"--- AVAILABLE SPECIALISTS ---\n{skills_info}\n\n"
            f"--- INSTRUCTIONS ---\n"
            f"If the user asks for a specific task (Code, Research), the Auto-Router will handle it.\n"
            f"If it is a general chat, use your internal knowledge."
        )
        return full_prompt
        
    def _trim_history(self):
        limit = max(self.settings.history_limit, 50)
        if len(self.history) > limit:
            self.history = [self.history[0]] + self.history[-limit:]

    def _spawn_agent(self, role_name: str, task_context: str = "") -> BaseAgent:
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
        mode_prompt = MODES.get(self.current_mode, MODES["BUILD"])["prompt"]
        
        worker.system_prompt = (
            f"{role_config.prompt}\n\n"
            f"=== MODE: {self.current_mode} ===\n"
            f"{mode_prompt}\n\n"
            f"MISSION:\n{task_context}"
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

        if not hasattr(self, 'current_worker'):
            self.current_worker = self._spawn_agent(self.current_role)

        from src.config import MODES
        mode_def = MODES.get(self.current_mode, MODES["BUILD"])
        return await self.current_worker.run(user_input)