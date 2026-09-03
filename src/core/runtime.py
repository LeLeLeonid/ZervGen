from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import time
import uuid


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class Task:
    id: str
    objective: str
    agent: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    artifacts: List["Artifact"] = field(default_factory=list)


@dataclass
class Plan:
    tasks: Dict[str, Task] = field(default_factory=dict)

    def add(self, task: Task) -> None:
        if not task.id or task.id in self.tasks:
            raise ValueError(f"Duplicate or empty task id: {task.id!r}")
        self.tasks[task.id] = task

    def validate(self) -> None:
        for task in self.tasks.values():
            missing = [dep for dep in task.depends_on if dep not in self.tasks]
            if missing:
                raise ValueError(f"Task '{task.id}' depends on missing task(s): {', '.join(missing)}")
            if task.id in task.depends_on:
                raise ValueError(f"Task '{task.id}' cannot depend on itself")

        visiting = set()
        visited = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError(f"Cyclic task dependency involving '{task_id}'")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dep in self.tasks[task_id].depends_on:
                visit(dep)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in self.tasks:
            visit(task_id)

    def ready(self) -> List[Task]:
        ready = []
        for task in self.tasks.values():
            if task.status != TaskStatus.PENDING:
                continue
            dependencies = [self.tasks[dep] for dep in task.depends_on if dep in self.tasks]
            if all(dep.status == TaskStatus.DONE for dep in dependencies) and len(dependencies) == len(task.depends_on):
                ready.append(task)
        return ready

    def next_ready(self) -> Optional[Task]:
        ready = self.ready()
        return ready[0] if ready else None

    def blocked(self) -> List[Task]:
        blocked = []
        for task in self.tasks.values():
            if task.status != TaskStatus.PENDING:
                if task.status == TaskStatus.BLOCKED:
                    blocked.append(task)
                continue
            if any(self.tasks.get(dep) and self.tasks[dep].status in {TaskStatus.FAILED, TaskStatus.BLOCKED} for dep in task.depends_on):
                task.status = TaskStatus.BLOCKED
                blocked.append(task)
        return blocked


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]
    agent: str
    started_at: float = field(default_factory=time.time)


@dataclass
class ToolResult:
    call_id: str
    success: bool
    output: Any = None
    error: Optional[str] = None
    ended_at: float = field(default_factory=time.time)
    duration: float = 0.0


@dataclass
class Artifact:
    path: str
    kind: str = "file"
    content_hash: Optional[str] = None


@dataclass
class CheckpointRef:
    id: str
    revision: Optional[str] = None
    state_path: Optional[str] = None


@dataclass
class ModelProfile:
    tier: str
    provider: str
    model: str
    tools: bool = True
    structured_output: bool = False
    reasoning: bool = False
    vision: bool = False
    programmatic_tools: bool = True


@dataclass
class Budget:
    max_steps: int = 50
    max_tool_calls: int = 200
    max_delegations: int = 12
    max_parallel_agents: int = 4
    steps: int = 0
    tool_calls: int = 0
    delegations: int = 0

    def step(self) -> bool:
        self.steps += 1
        return self.steps <= self.max_steps

    def tool(self) -> bool:
        self.tool_calls += 1
        return self.tool_calls <= self.max_tool_calls

    def delegation(self) -> bool:
        self.delegations += 1
        return self.delegations <= self.max_delegations


@dataclass
class TraceEvent:
    run_id: str
    type: str
    actor: str
    timestamp: float = field(default_factory=time.time)
    parent_id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    input: Any = None
    output: Any = None
    duration: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "type": self.type,
            "actor": self.actor,
            "timestamp": self.timestamp,
            "parent_id": self.parent_id,
            "name": self.name,
            "status": self.status,
            "input": self.input,
            "output": self.output,
            "duration": self.duration,
            "metadata": self.metadata,
        }


@dataclass
class Run:
    id: str
    goal: str
    agent: str
    status: RunStatus = RunStatus.PENDING
    plan: Plan = field(default_factory=Plan)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)
    events: List[TraceEvent] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    checkpoint: Optional[CheckpointRef] = None
    parent_run_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, goal: str, agent: str, max_steps: int = 50, parent_run_id: Optional[str] = None) -> "Run":
        return cls(
            id=uuid.uuid4().hex[:12],
            goal=goal,
            agent=agent,
            parent_run_id=parent_run_id,
            budget=Budget(max_steps=max_steps),
        )

    def emit(
        self,
        event_type: str,
        actor: str,
        name: Optional[str] = None,
        status: Optional[str] = None,
        input: Any = None,
        output: Any = None,
        duration: Optional[float] = None,
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TraceEvent:
        event = TraceEvent(
            run_id=self.id,
            type=event_type,
            actor=actor,
            name=name,
            status=status,
            input=input,
            output=output,
            duration=duration,
            parent_id=parent_id,
            metadata=metadata or {},
        )
        self.events.append(event)
        return event

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "agent": self.agent,
            "status": self.status.value,
            "plan": {
                "tasks": {
                    key: {
                        "id": task.id,
                        "objective": task.objective,
                        "agent": task.agent,
                        "depends_on": task.depends_on,
                        "status": task.status.value,
                        "result": task.result,
                        "artifacts": [artifact.__dict__ for artifact in task.artifacts],
                    }
                    for key, task in self.plan.tasks.items()
                }
            },
            "messages": self.messages[-20:],
            "tool_calls": [call.__dict__ for call in self.tool_calls[-50:]],
            "tool_results": [result.__dict__ for result in self.tool_results[-50:]],
            "artifacts": [artifact.__dict__ for artifact in self.artifacts[-50:]],
            "events": [event.as_dict() for event in self.events[-200:]],
            "checkpoint": self.checkpoint.__dict__ if self.checkpoint else None,
            "parent_run_id": self.parent_run_id,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "metadata": self.metadata,
            "budget": self.budget.__dict__,
        }
