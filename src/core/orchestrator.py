import asyncio
import logging
import uuid
from collections import deque
from typing import Any, Dict, Optional
from rich.console import Console
from src.config import MODES, GlobalSettings, load_config
from src.core.base_agent import BaseAgent
from src.core.memory import memory_core
from src.core.provider import AIProvider
from src.skills_loader import load_role, role_exists

logger = logging.getLogger(__name__)
console = Console()


class Orchestrator(BaseAgent):
    def __init__(self, provider: Optional[AIProvider] = None, settings: Optional[GlobalSettings] = None):
        if provider is not None and settings is None:
            settings = provider.settings if hasattr(provider, 'settings') else load_config()
        self.settings = settings or load_config()
        super().__init__(
            name="Orchestrator",
            provider=provider,
            skill_name="system",
            settings=self.settings,
            mode=self.settings.mode,
            memory=memory_core,
        )
        self.agents: Dict[str, BaseAgent] = {}
        self._auto_mode = True
        max_pending = self.settings.max_pending_results if self.settings.max_pending_results > 0 else None
        self._pending_results: deque = deque(maxlen=max_pending)

    def set_role(self, role: str) -> bool:
        if load_role(role):
            self.skill_name = role
            self._load_tools()
            return True
        return False

    def set_mode(self, mode: str) -> bool:
        mode_upper = mode.upper()
        if mode_upper in MODES:
            self.mode = mode_upper
            return True
        return False

    @property
    def auto_mode(self) -> bool:
        return self._auto_mode

    def toggle_auto(self, enabled: bool) -> None:
        self._auto_mode = enabled
        self.settings.save()

    def switch_provider(self, provider_name: str) -> bool:
        from src.core.provider import get_provider, list_providers
        valid = [p.name for p in list_providers()]
        if provider_name.lower() not in valid:
            return False
        try:
            self.provider = get_provider(provider_name.lower(), self.settings)
            self.settings.provider = provider_name.lower()
            self.settings.save()
            return True
        except Exception:
            return False

    def get_mode_status(self) -> Dict[str, Any]:
        return {"auto": self._auto_mode, "mode": self.mode, "role": self.skill_name}

    def get_auto_status(self) -> Dict[str, Any]:
        return {"status": "auto" if self._auto_mode else "manual", "mode": self.mode, "role": self.skill_name}

    def stop_auto(self) -> str:
        self._auto_mode = False
        return "Auto mode stopped."

    async def _spawn_agent(self, role_name: str, context: Optional[Dict] = None, max_steps: Optional[int] = None) -> BaseAgent:
        target_role = role_name if role_name != "system" else self.skill_name
        role_config = load_role(target_role) or load_role("system")
        initial_history = context.get("history", []) if context else []
        
        agent_id = f"{target_role}_{uuid.uuid4().hex[:8]}"
        
        max_agents = self.settings.max_spawned_agents
        if max_agents > 0 and len(self.agents) >= max_agents:
            oldest_key = next(iter(self.agents))
            try:
                await self.agents[oldest_key].cleanup()
            except Exception:
                pass
            del self.agents[oldest_key]
        
        agent = BaseAgent(
            name=target_role.capitalize(),
            provider=self.provider,
            skill_name=target_role,
            settings=self.settings,
            mode=self.mode,
            memory=self.memory,
            initial_history=initial_history,
            silent=False,
        )
        agent._is_delegated = True
        
        if max_steps is not None:
            agent._max_steps_override = max_steps
        
        agent.tools.pop("delegate_to", None)
        self.agents[agent_id] = agent
        return agent

    async def _handle_delegation(self, args: Dict[str, Any]) -> str:
        if "agents" in args:
            agents_spec = args["agents"]
            for spec in agents_spec:
                if not role_exists(spec.get("name", "code")):
                    return f"Error: Agent/Skill '{spec.get('name')}' does not exist"
            
            async def delegate_single(spec: Dict) -> tuple:
                agent_name = spec.get("name", "code")
                task = spec.get("task", "")
                max_steps = spec.get("max_steps")
                context = {"history": self.history.copy(), "parent_task": task}
                worker = await self._spawn_agent(agent_name, context, max_steps=max_steps)
                result = await worker.run(task)
                return result, worker.history, agent_name
            
            results = await asyncio.gather(*[delegate_single(s) for s in agents_spec], return_exceptions=True)
            
            summary_parts = []
            for i, r in enumerate(results):
                agent_name = agents_spec[i].get('name', 'agent')
                if isinstance(r, Exception):
                    summary_parts.append(f"- {agent_name}: Error: {r}")
                else:
                    result, history, _ = r
                    summary_parts.append(f"- {agent_name}: {result[:200]}...")
                    self._pending_results.append({"agent": agent_name, "result": result})
            
            return "DELEGATION RESULTS:\n" + "\n".join(summary_parts)
        
        sub_agent = args.get("agent_name", "code")
        sub_task = args.get("task", "")
        max_steps = args.get("max_steps")
        
        if not role_exists(sub_agent):
            return f"Error: Agent/Skill '{sub_agent}' does not exist"
        
        context = {"history": self.history.copy(), "parent_task": sub_task}
        worker = await self._spawn_agent(sub_agent, context, max_steps=max_steps)
        result = await worker.run(sub_task)
        
        self._pending_results.append({"agent": sub_agent, "result": result})
        self.history.append({"role": "assistant", "content": f"[{sub_agent}] {result}"})
        
        return f"[{sub_agent}] COMPLETED: {result}"

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name == "delegate_to":
            result = await self._handle_delegation(args)
            if self.memory:
                try:
                    self.memory.log_full(
                        role=self.name,
                        content=result,
                        tool="delegate_to",
                        args=args,
                        title="Delegation Complete"
                    )
                except Exception:
                    pass
            return result
        
        if tool_name == "manage_history":
            action = args.get("action")
            if action == "delete_last":
                for _ in range(2):
                    if len(self.history) > 1:
                        self.history.pop()
                return "History pruned"
            elif action == "clear":
                self.history.clear()
                return "History cleared"
            return f"Unknown action: {action}"
        
        if tool_name == "set_state":
            if args.get("role"):
                self.set_role(args["role"])
            if args.get("mode"):
                self.set_mode(args["mode"])
            return "STATE UPDATED"
        
        return await super()._execute_tool(tool_name, args)
    
    async def _handle_step_result(self, tool_name: str, args: Dict[str, Any], result: str, json_str: str) -> None:
        if tool_name == "delegate_to":
            self.history.append({"role": "assistant", "content": json_str})
            self.history.append({"role": "user", "content": result})
            return
        await super()._handle_step_result(tool_name, args, result, json_str)

    async def process(self, user_input: str) -> str:
        if not user_input or not isinstance(user_input, str):
            return "Error: Invalid input"
        user_input = user_input.strip()
        if not user_input:
            return "Error: Empty input"
        await self._init_mcp()
        return await self.run(user_input)
    
    async def run(self, task: str) -> str:
        if self._pending_results:
            pending_msg = "RECEIVED WHILE BUSY:\n" + "\n".join(
                f"- [{r['agent']}]: {r['result'][:150]}..." for r in list(self._pending_results)
            )
            self.history.append({"role": "user", "content": pending_msg})
            self._pending_results.clear()
        return await super().run(task)

    async def cleanup(self) -> None:
        await super().cleanup()
        for agent in self.agents.values():
            try:
                await agent.cleanup()
            except Exception:
                pass
        self.agents.clear()
