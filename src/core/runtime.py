from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import asyncio
import fnmatch
import inspect
import json
import re
import time
import uuid


class RunStatus(str, Enum):
    """Terminal and intermediate states of a run."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    BUDGET = "budget"


class TaskStatus(str, Enum):
    """Lifecycle states of a plan task."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class Task:
    """One unit of work inside a plan."""

    id: str
    objective: str
    agent: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    artifacts: List["Artifact"] = field(default_factory=list)


@dataclass
class Plan:
    """Dependency graph of tasks for a single run."""

    tasks: Dict[str, Task] = field(default_factory=dict)

    def add(self, task: Task) -> None:
        """Register a task; ids must be unique."""
        if not task.id or task.id in self.tasks:
            raise ValueError(f"Duplicate or empty task id: {task.id!r}")
        self.tasks[task.id] = task

    def validate(self) -> None:
        """Reject missing dependencies and cycles."""
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
        """Pending tasks whose dependencies are done."""
        ready = []
        for task in self.tasks.values():
            if task.status != TaskStatus.PENDING:
                continue
            dependencies = [self.tasks[dep] for dep in task.depends_on if dep in self.tasks]
            if len(dependencies) == len(task.depends_on) and all(dep.status == TaskStatus.DONE for dep in dependencies):
                ready.append(task)
        return ready

    def next_ready(self) -> Optional[Task]:
        """First runnable task, if any."""
        return self.ready()[0] if self.ready() else None

    def blocked(self) -> List[Task]:
        """Mark and return tasks behind failed dependencies."""
        blocked = []
        for task in self.tasks.values():
            if task.status == TaskStatus.BLOCKED:
                blocked.append(task)
                continue
            if task.status != TaskStatus.PENDING:
                continue
            if any(self.tasks.get(dep) and self.tasks[dep].status in {TaskStatus.FAILED, TaskStatus.BLOCKED} for dep in task.depends_on):
                task.status = TaskStatus.BLOCKED
                blocked.append(task)
        return blocked


@dataclass
class ToolCall:
    """An issued tool invocation."""

    id: str
    name: str
    arguments: Dict[str, Any]
    agent: str
    started_at: float = field(default_factory=time.time)


@dataclass
class ToolResult:
    """The single outcome of a tool call."""

    call_id: str
    success: bool
    output: Any = None
    error: Optional[str] = None
    ended_at: float = field(default_factory=time.time)
    duration: float = 0.0


@dataclass
class Artifact:
    """A file or resource produced by a run."""

    path: str
    kind: str = "file"
    content_hash: Optional[str] = None


@dataclass
class CheckpointRef:
    """Pointer to a persisted run checkpoint."""

    id: str
    revision: Optional[str] = None
    state_path: Optional[str] = None


@dataclass
class ModelProfile:
    """Capabilities of the selected model."""

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
    """Per-run ceilings for steps, tools and delegation."""

    max_steps: int = 50
    max_tool_calls: int = 200
    max_delegations: int = 12
    max_parallel_agents: int = 4
    max_file_writes: int = 20
    steps: int = 0
    tool_calls: int = 0
    delegations: int = 0
    file_writes: int = 0

    def step(self) -> bool:
        """Consume one step, or report exhaustion."""
        if self.steps >= self.max_steps:
            return False
        self.steps += 1
        return True

    def tool(self) -> bool:
        """Consume one tool call, or report exhaustion."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        return True

    def delegation(self) -> bool:
        """Consume one delegation, or report exhaustion."""
        if self.delegations >= self.max_delegations:
            return False
        self.delegations += 1
        return True

    def write(self) -> bool:
        """Consume one file write, or report exhaustion."""
        if self.max_file_writes and self.file_writes >= self.max_file_writes:
            return False
        self.file_writes += 1
        return True


class _BranchList(list):
    """A list view of the active branch; appends are persisted to the session."""

    def __init__(self, session: "Session"):
        """Bind the view to the session that persists its appends."""
        super().__init__()
        self._session = session

    def append(self, item) -> None:
        """Append in memory and persist the message."""
        super().append(item)
        self._session.record(item)

    def extend(self, items) -> None:
        """Append each message so every one is persisted."""
        for item in items:
            self.append(item)


class Session:
    """Append-only JSONL conversation tree."""

    def __init__(self, session_id: Optional[str] = None, root: Optional[Path] = None,
                 seed: Optional[List[Dict[str, Any]]] = None, persist: bool = True):
        """Create or reopen a session file; persist=False keeps it in memory."""
        self.id = session_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.persist = persist
        self.root = Path(root) if root else Path("tmp/memory/sessions")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{self.id}.jsonl"
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._leaf: Optional[str] = None
        self.messages = _BranchList(self)
        self._load()
        for message in seed or []:
            self.messages.append(message)

    def _load(self) -> None:
        """Rebuild the active branch from disk."""
        if not self.path.exists():
            return
        last = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            self._entries[entry["id"]] = entry
            last = entry["id"]
        self._leaf = last
        if last:
            chain = []
            current = last
            while current and current in self._entries:
                entry = self._entries[current]
                chain.append({"role": entry.get("role", "assistant"), "content": entry.get("content", "")})
                current = entry.get("parent_id")
            list.extend(self.messages, reversed(chain))

    def record(self, message: Dict[str, Any]) -> str:
        """Append a message node and return its id."""
        entry_id = uuid.uuid4().hex[:12]
        entry = {
            "id": entry_id,
            "parent_id": self._leaf,
            "role": message.get("role", "assistant"),
            "content": message.get("content", ""),
            "ts": time.time(),
            "meta": message.get("meta") or {},
        }
        self._entries[entry_id] = entry
        self._leaf = entry_id
        if self.persist:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry_id

    def active_branch(self) -> List[Dict[str, Any]]:
        """Messages from root to the current leaf."""
        chain = []
        current = self._leaf
        while current and current in self._entries:
            entry = self._entries[current]
            chain.append({"role": entry.get("role", "assistant"), "content": entry.get("content", "")})
            current = entry.get("parent_id")
        return list(reversed(chain))

    def _chain_ids(self) -> List[str]:
        """Entry ids from root to leaf."""
        ids = []
        current = self._leaf
        while current and current in self._entries:
            ids.append(current)
            current = self._entries[current].get("parent_id")
        return list(reversed(ids))

    def reset(self, messages: List[Dict[str, Any]]) -> None:
        """Rebuild the branch, recording only messages the branch does not have."""
        existing = self.active_branch()
        shared = 0
        for old, new in zip(existing, messages):
            if old.get("role") != new.get("role") or old.get("content") != new.get("content"):
                break
            shared += 1
        ids = self._chain_ids()
        self._leaf = ids[shared - 1] if shared else None
        list.clear(self.messages)
        list.extend(self.messages, messages[:shared])
        for message in messages[shared:]:
            self.messages.append(message)

    def compact(self, summary: str, keep: int = 8) -> None:
        """Replace the branch with a summary plus a verbatim tail."""
        branch = self.active_branch()
        tail = branch[-keep:] if keep > 0 else []
        marker = {"role": "system", "content": f"<context-summary>\n{summary}\n</context-summary>",
                  "meta": {"kind": "summary", "elided": max(0, len(branch) - len(tail))}}
        self.reset([marker] + tail)

    def rollback(self, entry_id: str) -> bool:
        """Move the leaf back to an existing entry."""
        if entry_id not in self._entries:
            return False
        self._leaf = entry_id
        list.clear(self.messages)
        list.extend(self.messages, self.active_branch())
        return True


class InputPending(Exception):
    """Raised when a mid-run message rings the doorbell and a turn must re-prompt."""


class RunCoordinator:
    """One busy period per session: doorbell for mid-run input, terminal status set once."""

    def __init__(self):
        """Start idle with an empty inbox."""
        self.busy = False
        self.doorbell = asyncio.Event()
        self.status: Optional[RunStatus] = None
        self._inbox: List[str] = []

    def begin(self) -> None:
        """Enter the busy period."""
        self.busy = True
        self.status = RunStatus.RUNNING
        self.doorbell.clear()
        self._inbox.clear()

    def ring(self, message: str) -> None:
        """Queue a mid-run message and ring the doorbell."""
        if message and message.strip():
            self._inbox.append(message.strip())
            self.doorbell.set()

    def drain(self) -> List[str]:
        """Take all queued messages and lower the doorbell."""
        pending, self._inbox = self._inbox, []
        self.doorbell.clear()
        return pending

    def finish(self, status: RunStatus, run: Any = None) -> Optional[RunStatus]:
        """Set a terminal status exactly once; None if one was already set."""
        if self.status in (RunStatus.DONE, RunStatus.FAILED, RunStatus.STOPPED,
                           RunStatus.TIMEOUT, RunStatus.MAX_STEPS):
            return None
        self.status = status
        self.busy = False
        if run is not None:
            run.status = status
            run.ended_at = time.time()
        return status


class PermissionPolicy:
    """Allow/ask/deny decided in code; the prompt only describes it."""

    ALWAYS_DENY = {"eval", "exec", "compile", "__import__"}
    RISKY = {"shell", "write", "append", "edit",
             "mcp_add", "mcp", "delegate", "click", "keys",
             "set_mode"}
    BUILD_ONLY = {"write", "append", "edit", "shell",
                  "mcp_add", "mcp", "set_mode"}
    SCOPE_KEYS = ("path", "file_path", "directory", "target", "filename", "cwd")

    def __init__(self, permission_mode: str = "ASK", mode: str = "BUILD", roots: Optional[List[str]] = None,
                 allow_rules: Optional[List[str]] = None, deny_rules: Optional[List[str]] = None):
        """Bind mode, permission level, allowed roots and explicit allow/deny globs."""
        self.permission_mode = (permission_mode or "ASK").upper()
        if self.permission_mode == "AUTO":
            self.permission_mode = "YOLO"
        self.mode = (mode or "BUILD").upper()
        self.roots = [Path(r).resolve() for r in (roots or [])]
        self.allow_rules = list(allow_rules or [])
        self.deny_rules = list(deny_rules or [])

    def is_mcp(self, tool_name: str) -> bool:
        return tool_name.startswith("mcp_") or tool_name.startswith("mcp__")

    def is_risky(self, tool_name: str) -> bool:
        """Whether a tool needs confirmation."""
        return tool_name in self.RISKY or self.is_mcp(tool_name)

    def _outside(self, value: str) -> bool:
        try:
            resolved = Path(value).expanduser().resolve()
        except Exception:
            return False
        return not any(resolved == root or root in resolved.parents for root in self.roots)

    @classmethod
    def _scoped_values(cls, value: Any, depth: int = 0) -> List[str]:
        """Path-like strings anywhere in an argument tree, MCP payloads included."""
        if depth > 4:
            return []
        if isinstance(value, str):
            text = value.strip()
            if text[:1] in ("{", "["):
                try:
                    return cls._scoped_values(json.loads(text), depth + 1)
                except (json.JSONDecodeError, TypeError):
                    return []
            return []
        if isinstance(value, dict):
            found = []
            for key, item in value.items():
                if key in cls.SCOPE_KEYS and isinstance(item, str) and item:
                    found.append(item)
                elif isinstance(item, (dict, list)):
                    found.extend(cls._scoped_values(item, depth + 1))
            return found
        if isinstance(value, list):
            found = []
            for item in value:
                found.extend(cls._scoped_values(item, depth + 1))
            return found
        return []

    def check_scope(self, args: Dict[str, Any]) -> Optional[str]:
        """Reject paths outside the allowed roots, including nested tool payloads."""
        if not self.roots:
            return None
        for value in self._scoped_values(args):
            if self._outside(value):
                return f"Error: path outside allowed roots: {value}"
        return None

    @staticmethod
    def _shell_paths(command: str) -> List[str]:
        """Absolute paths a shell command names, quoted segments included."""
        return re.findall(r"(?:^|[\s'\"=:])((?:~|/)[^\s'\";|&)]*)", str(command or ""))

    def _rule_match(self, rules: List[str], tool_name: str, args: Dict[str, Any]) -> bool:
        """A rule matches on `tool_name` and/or `path=` glob, comma-joined."""
        if not rules:
            return False
        for rule in rules:
            for part in str(rule).split(","):
                part = part.strip()
                if not part:
                    continue
                if part.startswith("path:"):
                    pat = part[5:]
                    for v in self._scoped_values(args):
                        if fnmatch.fnmatch(v, pat) or fnmatch.fnmatch(Path(v).name, pat):
                            return True
                elif fnmatch.fnmatch(tool_name, part):
                    return True
        return False

    def decide(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Resolve a tool call to allow, ask or deny."""
        if tool_name in self.ALWAYS_DENY:
            return "deny"
        if self._rule_match(self.deny_rules, tool_name, args):
            return "deny"
        if self._rule_match(self.allow_rules, tool_name, args):
            return "allow"
        if self.mode in ("ASK", "CALL") and (tool_name in self.BUILD_ONLY or self.is_mcp(tool_name)):
            return "deny"
        if self.check_scope(args):
            return "deny"
        scoped_outer = tool_name == "shell" and self.roots and any(
            self._outside(path) for path in self._shell_paths(args.get("command", "")))
        if scoped_outer:
            return "deny" if self.permission_mode == "DENY" else "ask"
        if self.permission_mode == "YOLO":
            return "allow"
        if self.permission_mode == "DENY":
            return "deny" if self.is_risky(tool_name) else "allow"
        return "ask" if self.is_risky(tool_name) else "allow"


class ToolRuntime:
    """One dispatch path: policy, budget, timeout, trace, one ToolResult."""

    MAX_LINES = 2000
    MAX_CHARS = 50_000

    def with_hooks(self, hooks: Optional[Any], fail_closed: bool = False) -> "ToolRuntime":
        """Attach pre/post shell hooks and return self for chaining."""
        self.hooks = hooks
        self.hooks_fail_closed = bool(fail_closed)
        return self

    def __init__(self, tools: Dict[str, Any], policy: PermissionPolicy, run: Optional["Run"] = None,
                 timeout: float = 60.0, agent: str = "agent",
                 on_event: Optional[Any] = None, on_log: Optional[Any] = None,
                 on_permission: Optional[Any] = None, spill_dir: Optional[Path] = None):
        """Bind tools, policy and reporting hooks."""
        self.tools = tools
        self.policy = policy
        self.run = run
        self.hooks: Optional[Any] = getattr(on_event, "hooks", None)
        self.hooks_fail_closed = bool(getattr(on_event, "hooks_fail_closed", False))
        self.timeout = float(timeout or 60)
        self.agent = agent
        self.on_event = on_event
        self.on_log = on_log
        self.on_permission = on_permission
        self.spill_dir = Path(spill_dir or "tmp/trace")

    def cap(self, text: str, name: str = "output") -> str:
        """Truncate output and spill the full copy to disk."""
        if not text:
            return text
        original = len(text)
        original_text = text
        reasons = []
        lines = text.splitlines()
        if len(lines) > self.MAX_LINES:
            text = "\n".join(lines[: self.MAX_LINES])
            reasons.append(f"{len(lines)} lines > {self.MAX_LINES}")
        if len(text) > self.MAX_CHARS:
            text = text[: self.MAX_CHARS]
            reasons.append(f"{original} chars > {self.MAX_CHARS}")
        if not reasons:
            return text
        note = f"[truncated: {', '.join(reasons)}"
        try:
            self.spill_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name)[:32] or "output"
            path = self.spill_dir / f"{slug}_{int(time.time() * 1000)}.txt"
            path.write_text(original_text, encoding="utf-8", errors="replace")
            return f"{text}\n\n{note}; full copy: {path}]"
        except Exception:
            return f"{text}\n\n{note}]"

    def _emit(self, kind: str, **kwargs) -> None:
        """Forward an event without letting it break the call."""
        if self.on_event is None:
            return
        try:
            self.on_event(kind, **kwargs)
        except Exception:
            pass

    async def execute(self, name: str, args: Dict[str, Any], timeout: Optional[float] = None) -> "ToolResult":
        """Run one tool under policy, budget, timeout and trace."""
        call_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        func = self.tools.get(name)
        if func is None:
            return self._finish(call_id, False, error=f"Error: Tool '{name}' not found",
                                started=started, name=name)
        decision = self.policy.decide(name, args)
        if decision == "deny":
            reason = self.policy.check_scope(args) or f"Error: {name} denied by policy."
            return self._finish(call_id, False, error=reason, started=started, name=name)
        if decision == "ask":
            allowed = False
            if self.on_permission is not None:
                details = json.dumps(args, ensure_ascii=False, default=str)[:300]
                try:
                    allowed = await self.on_permission(f"Allow {name}?", details, False)
                except Exception:
                    allowed = False
            if not allowed:
                return self._finish(call_id, False, error=f"Error: user denied {name}.",
                                    started=started, name=name)
        if self.run is not None and not self.run.budget.tool():
            return self._finish(call_id, False, error="Error: Tool-call budget exhausted",
                                started=started, name=name)
        if self.hooks:
            try:
                blocked = await self.hooks("pre_tool_call", name, args)
            except Exception:
                blocked = "hook crashed (fail_closed)" if self.hooks_fail_closed else None
            if blocked:
                return self._finish(call_id, False, error=f"Error: blocked by hook: {blocked}",
                                    started=started, name=name)
        call = ToolCall(call_id, name, dict(args), self.agent)
        if self.run is not None:
            self.run.tool_calls.append(call)
        self._emit("tool_call", name=name, status="running", input=args, call_id=call_id)
        limit = float(timeout if timeout is not None else self.timeout)
        try:
            if inspect.iscoroutinefunction(func):
                value = await asyncio.wait_for(func(**args), timeout=limit)
            else:
                value = await asyncio.wait_for(asyncio.to_thread(func, **args), timeout=limit)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return self._finish(call_id, False, error=f"Error: Tool '{name}' timed out", started=started, name=name)
        except TypeError as exc:
            hint = ""
            try:
                hint = f"\nExpected signature: {name}{inspect.signature(func)}"
            except Exception:
                pass
            return self._finish(call_id, False, error=f"TypeError: {exc}{hint}", started=started, name=name)
        except Exception as exc:
            return self._finish(call_id, False, error=f"Error: Tool '{name}' failed: {exc}", started=started, name=name)
        text = value if isinstance(value, str) else (json.dumps(value, ensure_ascii=False) if value is not None else "")
        if self.hooks:
            await self.hooks("post_tool_call", name, {**args, "_result": text[:2000]})
        return self._finish(call_id, True, output=self.cap(text, name), started=started, name=name)

    def _finish(self, call_id: str, success: bool, output: Any = None, error: Optional[str] = None,
                started: Optional[float] = None, name: str = "") -> "ToolResult":
        """Record exactly one ToolResult for a call."""
        duration = time.monotonic() - (started if started is not None else time.monotonic())
        result = ToolResult(call_id, success, output, error, duration=duration, ended_at=time.time())
        if self.run is not None:
            self.run.tool_results.append(result)
        self._emit("tool_result", name=name, status="ok" if success else "error",
                   output=output if success else error, duration=duration, call_id=call_id)
        return result


class Compactor:
    """Budget from the catalog; verbatim tail plus a structured checkpoint."""

    FLOOR = 8_000

    def __init__(self, provider=None, session: Optional[Session] = None):
        """Optionally bind a provider for summaries."""
        self.provider = provider
        self.session = session
        self._summary = ""

    @staticmethod
    def budget(provider_name: str, model: str, settings=None) -> int:
        """Usable context window from the catalog, with a floor."""
        from src.utils import ModelCatalog
        window = ModelCatalog.get_context(provider_name, model) or 0
        if window:
            return max(int(window), Compactor.FLOOR)
        return int(getattr(settings, "max_token_budget", 0) or 0)

    @staticmethod
    def used(messages: List[Dict[str, Any]]) -> int:
        """Approximate tokens held by a message list."""
        from src.utils import count_tokens
        return count_tokens(messages, "")[0]

    async def compact(self, messages: List[Dict[str, Any]], budget: int, keep: int = 8) -> List[Dict[str, Any]]:
        """Trim a message list to a budget, keeping the tail and the user's own words."""
        if self.used(messages) <= budget:
            return messages
        tail = messages[-keep:] if keep > 0 else []
        middle = messages[: len(messages) - len(tail)]
        summary = await self._summarize(middle, budget)
        if self.session is not None:
            self.session.compact(summary, keep=keep)
        head = f"<context-summary>\n{summary}\n</context-summary>"
        spec = self._specification(middle)
        if spec:
            head += f"\n<user-specification>\n{spec}\n</user-specification>"
        return [{"role": "system", "content": head}] + tail

    @staticmethod
    def _specification(middle: List[Dict[str, Any]]) -> str:
        """The user's own words, verbatim, so the goal cannot drift."""
        asked = [str(m.get("content", "")).strip() for m in middle if m.get("role") == "user"]
        return "\n".join(a[:600] for a in [a for a in asked if a][-3:])

    async def _summarize(self, middle: List[Dict[str, Any]], budget: int) -> str:
        """Build a structured checkpoint from what the user asked and what tools returned."""
        if not middle:
            return self._summary
        seen = [m for m in middle if m.get("role") != "assistant"]
        if not seen:
            return f"Earlier context elided ({len(middle)} messages)."
        transcript = "\n".join(
            f"[{m.get('role', '?')}]: {str(m.get('content', ''))[:600]}" for m in seen
        )[-max(4_000, budget * 2):]
        prompt = (
            "Checkpoint the conversation above. Keep, in this order: goal, done, decided, open, files touched. "
            "Preserve exact names and numbers. No commentary.\n\n"
            f"Previous checkpoint:\n{self._summary}\n\nNew segment:\n{transcript}"
        )
        if self.provider is None:
            return f"Earlier context elided ({len(middle)} messages)."
        try:
            text = await self.provider.generate_text(
                [{"role": "user", "content": prompt}],
                "You maintain a compact working checkpoint. Output only the checkpoint.",
            )
        except Exception:
            return f"Earlier context elided ({len(middle)} messages)."
        self._summary = str(text).strip()
        return self._summary


@dataclass
class TraceEvent:
    """Single entry in a run's trace log."""

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
        """Plain-dict form for logs and JSON."""
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
    """Everything one agent turn produced: status, trace, tools, budget."""

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
        """Start a new run with a step budget."""
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
        """Append a trace event and return it."""
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
        """Serializable snapshot of the run."""
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
