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
from src.tools import shell_exec
from src.utils import GitManager
from src.core.runtime import CheckpointRef, Plan, RunStatus, Task, TaskStatus

logger = logging.getLogger(__name__)
console = Console()

_FAILURE_MARKERS = re.compile(
    r"^(Error|BLOCKED|Timeout|TypeError|Exit code: [1-9]\d*|PRE-VALIDATION|MCP Error|"
    r"Max steps|Stopped by user|\[CRITIC FAILED\])\b")


def _looks_failed(text: str) -> bool:
    return bool(_FAILURE_MARKERS.match(text.strip()))


def _child_succeeded(agent: Any, text: str) -> bool:
    """Trust the child's own terminal status over a text heuristic."""
    run = getattr(agent, "_run", None) if agent is not None else None
    if run is not None:
        return run.status == RunStatus.DONE
    if agent is not None:
        return False
    return not _looks_failed(text)


class Orchestrator(StemAgent):
    """Coordinates agents, plans and verification without a second task-state model."""

    def __init__(
        self,
        provider: Optional[AIProvider] = None,
        settings: Optional[GlobalSettings] = None,
        mode: str = "CALL",
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
        self._goal_text = ""
        self._goal_round = 0
        self._goal_max = 10
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
            reason = self._check_file(Path(artifact.path))
            if reason:
                failures.append(reason)
        commands = list(getattr(self.settings, "verification_commands", []) or []) if self.mode == "BUILD" else []
        for command in commands:
            if not str(command).strip():
                continue
            result = await shell_exec(command)
            if not result.ok:
                failures.append(f"Verification command failed: {command}\n{result.to_text()}")
        if self._run:
            self._run.metadata["verification_passed"] = not failures
            self._run.emit("verification", self.name, "artifacts", "passed" if not failures else "failed", output=failures or "artifact checks passed")
        return failures

    def _with_goal(self, task: str) -> str:
        """Prefix the active /goal contract and its round counter."""
        if not self._goal_text:
            return task
        return f"{task}\n\n[GOAL] {self._goal_text}\n[ROUND {self._goal_round}/{self._goal_max}]"

    async def run(self, task: str) -> str:
        """Entry point: the supervised path, not the bare stem loop."""
        return await self.run_task(task, self.mode)

    async def run_task(self, task: str, mode: str = "BUILD") -> str:
        task = self._with_goal(task)
        if not self.settings.critic_enabled:
            if self.mode == "BUILD" and self._git_mgr and self._active_session_id:
                self._git_mgr.snapshot(self._active_session_id)
            result = await self._execute_waves(task, mode)
            self._checkpoint("task")
            return result

        skill_contract = skill_index.get(self.skill_name)
        original_task = task
        if skill_contract:
            pre_err = SkillEngine.validate_pre(skill_contract, task, set(self._active_tools()))
            if pre_err:
                return f"PRE-VALIDATION FAILED: {pre_err}"

        max_rounds = max(1, int(self.settings.max_refine_rounds))
        for round_no in range(1, max_rounds + 1):
            self._log(self.name, f"Round {round_no}/{max_rounds}", "state", self.mode)
            if self.mode == "BUILD" and self._git_mgr and self._active_session_id:
                self._git_mgr.snapshot(self._active_session_id)

            result = await self._execute_waves(task, mode)
            self._checkpoint(f"round {round_no}")
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
            if self._active_session_id:
                memory_core._session_db.update_session_grade(self._active_session_id, grade)

            await memory_core.add_memory(str(result), category="blueprint", critic_score=grade)

            if grade >= self.settings.critic_gate_threshold and not verify_failures:
                return result
            if round_no == max_rounds:
                reverted = False
                if self._git_mgr and self._active_session_id:
                    try:
                        reverted = bool(self._git_mgr.rollback(1))
                        if reverted:
                            self._log(self.name, f"Auto-rollback: grade {grade}", "state", self.mode)
                    except Exception as e:
                        self._log(self.name, f"Rollback failed: {e}", "state", self.mode)
                if self._run:
                    self._run.metadata["critic_failed"] = True
                    self._run.metadata["critic_grade"] = grade
                    self._run.status = RunStatus.FAILED
                note = "reverted 1 step" if reverted else "no checkpoint available, nothing reverted"
                return f"[CRITIC FAILED] Grade {grade} — {note}. Escalate."

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
        requested_mode = str(args.get("mode") or "").upper()
        if requested_mode:
            from src.config import MODES
            if requested_mode not in MODES:
                return f"Error: unknown mode '{requested_mode}'. One of: {', '.join(MODES)}."
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
                    skipped = [s for s in wave_specs
                               if plan.tasks[self._task_key(s)].status == TaskStatus.BLOCKED]
                    wave_specs = [s for s in wave_specs if s not in skipped]
                    if skipped:
                        all_results.extend(f"=== {self._task_key(s)}: skipped (dependency failed) ===" for s in skipped)
                    if not wave_specs:
                        continue
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
                        child_id = next((aid for aid, sp in agent_id_map.items() if sp is spec), None)
                        child = self.agents.get(child_id or "")
                        if isinstance(result, Exception):
                            task_obj.status = TaskStatus.FAILED
                            task_obj.result = f"{type(result).__name__}: {result}"
                        else:
                            ok = bool(result) and _child_succeeded(child, str(result))
                            task_obj.status = TaskStatus.DONE if ok else TaskStatus.FAILED
                            task_obj.result = str(result)
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
                    waiting = [t for t in plan.tasks.values() if t.status == TaskStatus.PENDING]
                    if not waiting:
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
        if requested_mode:
            agent.mode = requested_mode
            agent._runtime = agent._build_runtime()
            agent.invalidate_cache()
        if str(args.get("memory", "")).strip().lower() in ("off", "false", "0", "no"):
            agent.memory = None
        result = await agent.run(full_task)
        self._last_agent_id = agent_id
        return str(result)

    @staticmethod
    def _task_key(spec: Dict) -> str:
        return str(spec.get("id") or spec.get("task_id") or spec["agent_name"])

    def _compute_waves(self, agents_spec: List[Dict]) -> List[List[Dict]]:
        """Group specs into waves by task id, so two specs may share an agent."""
        by_key = {self._task_key(spec): spec for spec in agents_spec}
        if len(by_key) != len(agents_spec):
            raise ValueError("Delegation specs need unique ids")
        in_degree = {key: 0 for key in by_key}
        graph = {key: set() for key in by_key}
        for spec in agents_spec:
            key = self._task_key(spec)
            for dep in spec.get("depends_on", []) or []:
                dep = str(dep)
                if dep in graph:
                    graph[dep].add(key)
                    in_degree[key] += 1
                else:
                    logger.warning(
                        f"Task '{key}' depends on unknown task '{dep}' — dependency ignored")
        waves = []
        remaining = [spec for spec in agents_spec if in_degree[self._task_key(spec)] == 0]
        while remaining:
            waves.append(list(remaining))
            next_remaining = []
            for spec in remaining:
                for successor in graph.get(self._task_key(spec), set()):
                    in_degree[successor] -= 1
                    if in_degree[successor] == 0:
                        next_remaining.append(by_key[successor])
            remaining = next_remaining
        placed = {self._task_key(spec) for wave in waves for spec in wave}
        if placed != set(by_key):
            raise ValueError(f"Circular dependency detected among: {set(by_key) - placed}")
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
        child_provider = self.provider.clone() if hasattr(self.provider, "clone") else self.provider
        agent = StemAgent(
            name=role_name,
            provider=child_provider,
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
        agent.trace.paint = False
        agent._max_steps_override = max_steps or self._get_max_steps()
        agent._is_delegated = True
        agent._parent_run = getattr(self, "_run", None)
        agent._mcp = self._mcp
        agent._mcp_initialized = True
        agent.tools.pop("delegate", None)
        self.agents[agent_id] = agent
        self._log(self.name, f"SPAWN: {role_name} ({agent_id})", "agent_spawn", self.mode)
        return agent_id, agent