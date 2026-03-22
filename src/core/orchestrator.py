import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Set
from rich.console import Console
from src.config import MODES, GlobalSettings, load_config
from src.core.base_agent import BaseAgent
from src.core.memory import memory_core
from src.core.provider import AIProvider
from src.skills_loader import load_role, role_exists

logger = logging.getLogger(__name__)
console = Console()


def _parse_history_role(history: List[Dict]) -> Optional[str]:
    for msg in reversed(history):
        content = msg.get("content", "")
        if isinstance(content, str):
            if content.startswith("AGENT:"):
                agent_id = content.split(":", 1)[1].strip()
                if agent_id and agent_id != "None":
                    return agent_id.split("_")[0].lower() if "_" in agent_id else agent_id
            elif content.startswith("ROLE:"):
                role = content.split(":", 1)[1].strip()
                if role and role != "system":
                    return role
    return None


class Orchestrator(BaseAgent):
    def __init__(self, provider: Optional[AIProvider] = None, settings: Optional[GlobalSettings] = None):
        if settings is None:
            settings = provider.settings if provider and hasattr(provider, 'settings') else load_config()
        self.settings = settings
        super().__init__(
            name="Orchestrator", provider=provider, skill_name="system",
            settings=self.settings, mode=self.settings.mode, memory=memory_core,
        )
        self.agents: Dict[str, BaseAgent] = {}
        self._last_agent_id: Optional[str] = None

        last_role = _parse_history_role(self.history)
        if last_role:
            self.skill_name = last_role

    def _log_state(self, key: str, value: str) -> None:
        if self.memory:
            self.memory.log_event_sync("system", f"{key}: {value}", "state", self.mode)

    def set_role(self, role: str) -> bool:
        if load_role(role):
            self.skill_name = role
            self._load_tools()
            self._log_state("ROLE", role)
            return True
        return False

    def set_mode(self, mode: str) -> bool:
        mode_upper = mode.upper()
        if mode_upper in MODES:
            self.mode = mode_upper
            self._log_state("MODE", mode_upper)
            return True
        return False

    def narrative_cast(self, from_agent: str, to_agent: str, log: list) -> str:
        key_points = []
        for msg in log:
            content = msg.get("content", "")
            if msg.get("role") == "system":
                key_points.append(content[:300])
            elif content.startswith("Error"):
                key_points.append(f"[ERROR] {content[:150]}")
        return f"Context from {from_agent}: No results." if not key_points else f"=== CONTEXT FROM {from_agent.upper()} ===\n" + "\n---\n".join(key_points[-5:])

    @property
    def auto_mode(self) -> bool:
        return self._auto_mode

    def toggle_auto(self, enabled: bool = None) -> bool:
        self._auto_mode = not self._auto_mode if enabled is None else enabled
        self.settings.auto_mode = self._auto_mode
        self.settings.save()
        return self._auto_mode

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
        except Exception as e:
            logger.error(f"Provider switch failed: {e}")
            return False

    def get_mode_status(self) -> Dict[str, Any]:
        return {"auto": self._auto_mode, "mode": self.mode, "role": self.skill_name}

    def stop_auto(self) -> str:
        self._auto_mode = False
        return "Auto mode stopped."

    async def _spawn_agent(self, role_name: str, context: Optional[Dict] = None, max_steps: Optional[int] = None) -> BaseAgent:
        target_role = role_name if role_name != "system" else self.skill_name
        initial_history = context.get("history", []) if context else []
        agent_id = f"{target_role}_{uuid.uuid4().hex[:8]}"
        target_name = target_role.capitalize()
        self._log(target_name, f"Spawning agent: {target_role} (id: {agent_id})", "agent_spawn")

        max_agents = self.settings.max_spawned_agents
        if max_agents > 0 and len(self.agents) >= max_agents:
            oldest_key = next(iter(self.agents))
            try:
                await self.agents[oldest_key].cleanup()
            except Exception as e:
                logger.debug(f"Agent cleanup error: {e}")
            del self.agents[oldest_key]

        agent = BaseAgent(
            name=target_role.capitalize(), provider=self.provider, skill_name=target_role,
            settings=self.settings, mode=self.mode, memory=self.memory,
            initial_history=initial_history, silent=False,
        )
        agent._is_delegated = True
        if max_steps is not None:
            agent._max_steps_override = max_steps
        if "delegate_to" in agent.tools:
            del agent.tools["delegate_to"]

        self.agents[agent_id] = agent
        self._last_agent_id = agent_id
        return agent

    async def _delegate_single(self, spec: Dict) -> tuple:
        agent_name = spec.get("name", "code")
        task = spec.get("task", "")
        max_steps = spec.get("max_steps")
        context = {"history": self.history.copy(), "parent_task": task}
        agent = await self._spawn_agent(agent_name, context, max_steps=max_steps)
        agent._log(agent.name, f"Delegating task: {task[:100]}...", "delegation_start")
        result = await agent.run(task)
        casted_context = self.narrative_cast(agent_name, self.name, agent.history)
        self._log(agent.name, casted_context, "context")
        self._last_agent_id = list(self.agents.keys())[-1] if self.agents else None
        self._log(agent.name, f"AGENT: {self._last_agent_id}", "state")
        self.history.append({"role": agent.name, "content": result})
        self.skill_name = agent_name
        return result, agent.history, agent_name

    def _compute_waves(self, agents_spec: List[Dict]) -> List[List[Dict]]:
        indexed = {i: spec for i, spec in enumerate(agents_spec)}
        deps = {i: set(spec.get("depends_on", [])) for i, spec in enumerate(agents_spec)}
        waves = []
        completed: Set[int] = set()
        remaining = set(indexed.keys())

        while remaining:
            ready = [i for i in remaining if deps[i].issubset(completed)]
            if not ready:
                ready = list(remaining)
            waves.append([indexed[i] for i in ready])
            completed.update(ready)
            remaining.discard(i for i in ready)

        return waves

    async def _handle_delegation(self, args: Dict[str, Any]) -> str:
        if "agents" in args:
            agents_spec = args["agents"]
            for spec in agents_spec:
                if not role_exists(spec.get("name", "code")):
                    return f"Error: Agent/Skill '{spec.get('name')}' does not exist"

            waves = self._compute_waves(agents_spec)
            summary_parts = []

            for wave_idx, wave in enumerate(waves):
                self._log("Wave", f"Wave {wave_idx + 1}/{len(waves)}: {len(wave)} agents", "wave")
                results = await asyncio.gather(*[self._delegate_single(s) for s in wave], return_exceptions=True)

                for i, r in enumerate(results):
                    agent_name = wave[i].get('name', 'agent')
                    if isinstance(r, Exception):
                        summary_parts.append(f"- {agent_name}: Error: {r}")
                    elif isinstance(r, tuple):
                        result, history, _ = r
                        summary_parts.append(f"{result[:200]}...")
                    else:
                        summary_parts.append(f"- {agent_name}: Unexpected result type")

            return "\n".join(summary_parts)

        sub_agent = args.get("agent_name", "code")
        if not role_exists(sub_agent):
            return f"Error: Agent/Skill '{sub_agent}' does not exist"

        spec = {
            "name": sub_agent,
            "task": args.get("task", ""),
            "max_steps": args.get("max_steps")
        }
        result, history, agent_name = await self._delegate_single(spec)
        return result

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name == "delegate_to":
            if self._status:
                self._status.__exit__(None, None, None)
                self._status = None
            return await self._handle_delegation(args)

        if tool_name == "manage_history":
            action = args.get("action")
            if action == "delete_last":
                removed = 0
                while len(self.history) > 1 and removed < 2:
                    self.history.pop()
                    removed += 1
                return f"History pruned ({removed} messages removed)"
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

    async def process(self, user_input: str) -> str:
        if not user_input or not isinstance(user_input, str):
            return "Error: Invalid input"
        user_input = user_input.strip()
        if not user_input:
            return "Error: Empty input"

        await self._init_mcp()

        last_role = _parse_history_role(self.history)
        if last_role:
            self.skill_name = last_role

        return await self.run(user_input)

    async def cleanup(self) -> None:
        await super().cleanup()
        for agent in self.agents.values():
            try:
                await agent.cleanup()
            except Exception as e:
                logger.debug(f"Agent cleanup error: {e}")
        self.agents.clear()
