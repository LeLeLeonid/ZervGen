import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from rich.console import Console
from src.config import MODES, GlobalSettings, load_config
from src.core.base_agent import StemAgent
from src.core.memory import memory_core
from src.core.provider import AIProvider, get_provider
from src.skills_loader import load_role, role_exists, skill_index, SkillEngine
from src.tools import TOOL_REGISTRY, get_tools_schema
from src.utils import get_system_context, sanitize_for_prompt, generate_random_delimiter, count_tokens, add_global_tokens, add_provider_tokens, GitManager
from src.core.runtime import CheckpointRef, Plan, Task, TaskStatus

logger = logging.getLogger(__name__)
console = Console()


class Orchestrator(StemAgent):
    """Coordinates agents, plans and verification without a second task-state model."""

    def __init__(
        self,
        provider: Optional[AIProvider] = None,
        settings: Optional[GlobalSettings] = None,
        mode: str = "BUILD",
        memory: Any = None,
    ):
        settings = settings or load_config()
        provider = provider or get_provider(getattr(settings, "provider", "pollinations"), settings)
        super().__init__(
            name="orchestrator",
            provider=provider,
            skill_name="system",
            settings=settings,
            mode=mode,
            memory=memory,
            silent=False,
        )
        self.agents: Dict[str, StemAgent] = {}
        self._last_agent_id: Optional[str] = None
        self._user_active = True
        self._active_session_id: Optional[str] = None
        self._git_mgr = None
        if settings.checkpoints_enabled and mode.upper() == "BUILD":
            self._git_mgr = GitManager(Path.cwd(), max_snapshots=settings.checkpoint_max_snapshots)

    def set_mode(self, mode: str) -> bool:
        mode = mode.upper()
        if mode not in MODES:
            return False
        self.mode = mode
        self.invalidate_cache()
        return True

    def set_role(self, role: str) -> bool:
        if not role_exists(role):
            return False
        self.skill_name = role
        self.name = role
        self._load_tools()
        self.invalidate_cache()
        return True

    def switch_provider(self, name: str) -> bool:
        try:
            self.provider = get_provider(name, self.settings)
            self.settings.provider = name
            self.settings.save()
            self.invalidate_cache()
            return True
        except Exception as e:
            logger.warning("Provider switch failed: %s", e)
            return False

    def get_mode_status(self) -> dict:
        return {
            "mode": self.mode,
            "role": self.skill_name,
            "provider": getattr(self.settings, "provider", "unknown"),
        }

    def new_session(self, title: str = None) -> None:
        from src.utils import reset_global_tokens
        reset_global_tokens()
        self.history.clear()
        self.agents.clear()
        self._run = None
        if self.memory:
            try:
                session_id = self.memory._session_db.create_session(
                    provider=getattr(self.provider, "name", "unknown")
                )
                self.memory.set_active_session(session_id)
                self._active_session_id = session_id
                if title:
                    self.memory._session_db.set_session_title(session_id, title[:100])
            except Exception:
                logger.debug("Session reset failed", exc_info=True)

    def _checkpoint(self, reason: str) -> Optional[str]:
        if not (self._git_mgr and self._active_session_id and getattr(self.settings, "checkpoints_enabled", True) and self._run):
            return None
        state = {
            "reason": reason,
            "goal": self._run.goal,
            "mode": self.mode,
            "run_id": self._run.id,
            "messages": self.history[-20:],
            "run": self._run.to_dict(),
        }
        checkpoint_id = self._git_mgr.checkpoint(self._active_session_id, state)
        st = self._git_mgr.load_state(checkpoint_id) or {}
        self._run.checkpoint = CheckpointRef(id=checkpoint_id, revision=st.get("revision"))
        self._run.metadata["checkpoint_reason"] = reason
        self._run.emit(
            "checkpoint", self.name, "git", "saved",
            output={"checkpoint_id": checkpoint_id, "git_sha": st.get("revision") or ""},
        )
        return checkpoint_id

    async def resume_checkpoint(self, checkpoint_id: str = "") -> str:
        if not self._git_mgr or not getattr(self.settings, "resume_enabled", True):
            return "Error: Checkpoints are unavailable."
        state = self._git_mgr.restore_checkpoint(checkpoint_id) if hasattr(self._git_mgr, "restore_checkpoint") else (self._git_mgr.load_state(checkpoint_id) if checkpoint_id else None)
        if not state:
            return "Error: Checkpoint not found or could not be restored."
        payload = state.get("state") or {}
        self.history = list(payload.get("messages") or [])
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            return f"Restored checkpoint {state.get('id', '-')}; no resumable goal found."
        self.mode = str(payload.get("mode") or self.mode).upper()
        return await self.run_task(goal, self.mode)
    
    def request_interrupt(self) -> None:
        for agent in self.agents.values():
            agent.request_interrupt()
        super().request_interrupt()
    
    async def _verify_artifacts(self) -> List[str]:
        failures = []
        for artifact in (self._run.artifacts if self._run else []):
            path = Path(artifact.path)
            if not path.exists():
                failures.append(f"Missing artifact: {artifact.path}")
                continue
            if artifact.kind == "file" and path.suffix.lower() == ".py":
                import py_compile
                try:
                    py_compile.compile(str(path), doraise=True)
                except py_compile.PyCompileError as e:
                    failures.append(f"Python syntax failed: {artifact.path}: {e.msg or e}")
            elif artifact.kind == "file" and path.suffix.lower() == ".json":
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except Exception as e:
                    failures.append(f"JSON validation failed: {artifact.path}: {e}")
        commands = list(getattr(self.settings, "verification_commands", []) or []) if self.mode == "BUILD" else []
        for command in commands:
            if not str(command).strip():
                continue
            result = await self._execute_tool("shell", {"command": command})
            if str(result).startswith("Error"):
                failures.append(f"Verification command failed: {command}\n{result}")
        if self._run:
            self._run.status = self._run.status.VERIFYING
            self._run.emit("verification", self.name, "artifacts", "passed" if not failures else "failed", output=failures or "artifact checks passed")
        return failures

    async def run_task(self, task: str, mode: str = "BUILD") -> str:
        if not self.settings.critic_enabled:
            return await self._execute_waves(task, mode)

        skill_contract = skill_index.get(self.skill_name)
        original_task = task
        if skill_contract:
            pre_err = SkillEngine.validate_pre(skill_contract, {"task": task})
            if pre_err:
                return f"PRE-VALIDATION FAILED: {pre_err}"

        max_rounds = max(1, int(self.settings.max_refine_rounds))
        for round_no in range(1, max_rounds + 1):
            self._log(self.name, f"Round {round_no}/{max_rounds}", "state", self.mode)
            if self._run:
                self._run.status = self._run.status.RUNNING
            if self.mode == "BUILD" and self._git_mgr and self._active_session_id:
                self._git_mgr.snapshot(self._active_session_id)

            result = await self._execute_waves(task, mode)
            verify_failures = await self._verify_artifacts()
            if verify_failures:
                self._log(self.name, "VERIFICATION FAILED:\n" + "\n".join(verify_failures), "state", self.mode)
                result = f"{result}\n\n[VERIFICATION FAILURES]\n" + "\n".join(verify_failures)

            procedure = skill_contract.procedure if skill_contract and skill_contract.procedure else []
            if procedure:
                grade = await SkillEngine.grade(self.provider, original_task, str(result), procedure)
            else:
                grade_prompt = f"""Strict verifier. The ORIGINAL TASK is the rubric, nothing else.
ORIGINAL TASK:
{original_task}

OUTPUT TO GRADE:
{str(result)}

1. List each concrete thing the task actually asks for, one per line.
2. Mark each MET or MISSING (3-word reason).
3. Score = round(5 * met / total). Reward only satisfied requirements, never effort.
End with exactly one line: GRADE: [1-5]"""
                grade_resp = await self.provider.generate_text([{"role": "user", "content": grade_prompt}], system_prompt="Strict verifier.")
                match = re.search(r"GRADE:\s*([1-5])", grade_resp)
                grade = float(match.group(1)) if match else 1.0

                post_err = SkillEngine.validate_post(skill_contract, result) if skill_contract else None
                if post_err:
                    grade = min(grade, float(self.settings.critic_gate_threshold) - 0.01)
                    self._log(self.name, f"POST-VALIDATION FAILED: {post_err}", "state", self.mode)

                if self._run:
                    self._run.metadata["last_grade"] = grade
                    self._run.metadata["verification_failures"] = verify_failures
                    self._run.status = self._run.status.DONE if grade >= self.settings.critic_gate_threshold else self._run.status.VERIFYING

                await memory_core.add_memory(str(result), category="blueprint", critic_score=grade)

                if grade >= self.settings.critic_gate_threshold and not verify_failures:
                    return result
                if round_no == max_rounds:
                    if self._git_mgr:
                        try:
                            self._git_mgr.rollback(1)
                            self._log(self.name, f"Auto-rollback: grade {grade}", "state", self.mode)
                        except Exception as e:
                            self._log(self.name, f"Rollback failed: {e}", "state", self.mode)
                    if self._run:
                        self._run.status = self._run.status.FAILED
                    return f"[ROLLBACK] Grade {grade} — reverted 1 step. Escalate."

                task = f"Original task: {original_task}\n\nPrevious attempt scored {grade}/5. Fix the missing/broken parts. Focus ONLY on gaps."

        return "Degeneration guard triggered. Halted."

    async def _execute_waves(self, task: str, mode: str) -> str:
        return await super().run(task)

    async def _handle_delegation(self, args: Dict) -> str:
        if self._run and not self._run.budget.delegation():
            return "Error: Delegation budget exhausted."
        agent_name = args.get("agent_name", "")
        task = args.get("task", "")
        context = args.get("context", "")
        agents_list = args.get("agents", "")
        full_task = f"{context}\n\n{task}" if context else task
        if not full_task.strip():
            return "Error: Empty task"

        if agents_list:
            try:
                specs = json.loads(agents_list) if isinstance(agents_list, str) else agents_list
                if not isinstance(specs, list):
                    return "Error: agents must be a JSON list"
                for spec in specs:
                    if not spec.get("agent_name"):
                        return "Error: each agent spec needs agent_name"
                    if not role_exists(spec["agent_name"]):
                        return f"Error: agent '{spec['agent_name']}' not found"
                waves = self._compute_waves(specs)
                plan = Plan()
                for spec in [item for wave in waves for item in wave]:
                    plan.add(Task(
                        id=str(spec.get("id") or spec.get("task_id") or spec["agent_name"]),
                        objective=str(spec.get("task", "")),
                        agent=spec["agent_name"],
                        depends_on=list(spec.get("depends_on", []) or []),
                    ))
                plan.validate()
                if self._run:
                    self._run.plan = plan

                all_results = []
                results_by_id = {}
                for wave_idx, wave_specs in enumerate(waves):
                    self._log(self.name, f"WAVE {wave_idx + 1}/{len(waves)}: {[s.get('id', s['agent_name']) for s in wave_specs]}", "delegation_start", self.mode)
                    wave_tasks = []
                    agent_id_map = {}
                    for spec in wave_specs:
                        dep_text = [f"Dependency {dep} result:\n{results_by_id[dep]}" for dep in spec.get("depends_on", []) if dep in results_by_id]
                        spawn_context = f"Overall task: {full_task}\n\nYour specific assignment: {spec.get('task', '')}"
                        if dep_text:
                            spawn_context += "\n\n" + "\n\n".join(dep_text)
                        if spec.get("context"):
                            spawn_context = f"{spec['context']}\n\n{spawn_context}"
                        agent_id, agent = self._spawn_agent(role_name=spec["agent_name"], context=spawn_context, max_steps=spec.get("max_steps"))
                        agent_id_map[agent_id] = spec
                        if self._run:
                            task_obj = plan.tasks[str(spec.get("id") or spec.get("task_id") or spec["agent_name"])]
                            task_obj.status = TaskStatus.RUNNING
                        wave_tasks.append(agent.run(full_task if not spec.get("task") else spawn_context))

                    limit = max(1, getattr(self.settings, "max_parallel_agents", 4))
                    wave_results = []
                    for start in range(0, len(wave_tasks), limit):
                        wave_results.extend(await asyncio.gather(*wave_tasks[start:start + limit], return_exceptions=True))

                    for agent_id, result in zip(agent_id_map.keys(), wave_results):
                        if agent_id in self.agents:
                            self._log(self.agents[agent_id].name, str(result), "delegation_result", self.mode)
                            self._last_agent_id = agent_id

                    for result, spec in zip(wave_results, wave_specs):
                        task_id = str(spec.get("id") or spec.get("task_id") or spec["agent_name"])
                        results_by_id[task_id] = str(result)
                        task_obj = plan.tasks[task_id]
                        if isinstance(result, Exception):
                            task_obj.status = TaskStatus.FAILED
                            task_obj.result = f"{type(result).__name__}: {result}"
                        else:
                            ok = bool(result) and not str(result).strip().startswith("Error")
                            task_obj.status = TaskStatus.DONE if ok else TaskStatus.FAILED
                            task_obj.result = str(result)
                        child_id = next((aid for aid, sp in agent_id_map.items() if sp is spec), None)
                        child = self.agents.get(child_id or "")
                        if child and getattr(child, "_run", None):
                            task_obj.artifacts = list(child._run.artifacts)
                        if task_obj.status != TaskStatus.DONE:
                            for dependent in plan.tasks.values():
                                if task_id in dependent.depends_on and dependent.status == TaskStatus.PENDING:
                                    dependent.status = TaskStatus.BLOCKED

                    all_results.extend(
                        f"=== {spec.get('id', spec['agent_name'])}: {spec['agent_name']} ===\n{str(result)}"
                        for result, spec in zip(wave_results, wave_specs)
                    )
                    if any(plan.tasks[str(s.get("id") or s.get("task_id") or s["agent_name"])].status in {TaskStatus.FAILED, TaskStatus.BLOCKED} for s in wave_specs):
                        break
                if self._run:
                    self._run.plan = plan
                return "\n\n".join(all_results)
            except Exception as e:
                return f"Error in delegation: {e}"

        if not agent_name:
            return "Error: agent_name required"
        agent_id = args.get("agent_id", "")
        if agent_id and agent_id in self.agents:
            agent = self.agents[agent_id]
        else:
            agent_id, agent = self._spawn_agent(role_name=agent_name, context=context, max_steps=None)
        result = await agent.run(full_task)
        self._last_agent_id = agent_id
        return str(result)

    def _compute_waves(self, agents_spec: List[Dict]) -> List[List[Dict]]:
        by_name = {spec["agent_name"]: spec for spec in agents_spec}
        in_degree = {spec["agent_name"]: 0 for spec in agents_spec}
        graph = {spec["agent_name"]: set() for spec in agents_spec}
        for spec in agents_spec:
            deps = spec.get("depends_on", [])
            for dep in deps:
                if dep in graph:
                    graph[dep].add(spec["agent_name"])
                    in_degree[spec["agent_name"]] = in_degree.get(spec["agent_name"], 0) + 1
                else:
                    logger.warning(
                        f"Agent '{spec['agent_name']}' depends on unknown "
                        f"agent '{dep}' — dependency ignored")
        waves = []
        remaining = [s for s in agents_spec if in_degree.get(s["agent_name"], 0) == 0]
        while remaining:
            current_wave = remaining[:]
            waves.append(current_wave)
            next_remaining = []
            for spec in current_wave:
                name = spec["agent_name"]
                for succ in graph.get(name, set()):
                    in_degree[succ] = in_degree.get(succ, 0) - 1
                    if in_degree[succ] == 0:
                        next_remaining.append(by_name[succ])
            remaining = next_remaining
        all_names = {s["agent_name"] for s in agents_spec}
        placed = {s["agent_name"] for w in waves for s in w}
        if all_names != placed:
            msg = f"Circular dependency detected among: {all_names - placed}"
            logger.error(msg)
            raise ValueError(msg)
        return waves

    def _spawn_agent(self, role_name: str, context: str, max_steps: int = None) -> tuple:
        if not role_exists(role_name):
            raise ValueError(f"Role '{role_name}' does not exist")
        agent_id = f"{role_name}_{uuid.uuid4().hex[:8]}"
        role_cfg = load_role(role_name) or load_role("system")
        prompt = role_cfg.prompt if role_cfg else f"You are {role_name}."
        initial_history = [{"role": "system", "content": prompt}]
        non_system = [
            m for m in self.history
            if m.get("role") in ("user", "assistant")
        ]
        for msg in non_system[-6:]:
            initial_history.append({"role": msg["role"], "content": msg["content"]})
        agent = StemAgent(
            name=role_name,
            provider=self.provider,
            skill_name=role_name,
            settings=self.settings,
            mode=self.mode,
            memory=self.memory,
            initial_history=initial_history,
            silent=True,
            orchestrator=self,
        )
        agent._ask_user = self._ask_user
        agent.status_cb = self.status_cb
        agent._max_steps_override = max_steps or self._get_max_steps()
        agent._is_delegated = True
        agent._mcp = self._mcp
        agent._mcp_initialized = True
        agent.tools = {k: v for k, v in self.tools.items() if k != "delegate_to"}
        self.agents[agent_id] = agent
        self._log(self.name, f"SPAWN: {role_name} ({agent_id})", "agent_spawn", self.mode)
        return agent_id, agent