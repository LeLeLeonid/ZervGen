import asyncio
import ast
import hashlib
import json
import logging
import inspect
import re
import subprocess
import time
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from src.config import MODES, GlobalSettings, load_config, ZG_PROTOCOL
from src.core.memory import memory_core
from src.core.provider import AIProvider, get_provider
from src.skills_loader import load_role, SkillEngine
from src.tools import TOOL_REGISTRY, current_agent, shell_exec, _mcp_schema, _schema_signature, _schema_doc, _native_tool_schemas
from src.utils import get_system_context, sanitize_for_prompt, generate_random_delimiter, count_tokens, add_global_tokens, add_provider_tokens, ContextCompressor, ModelCatalog, safe_run, GitManager, truncate_to_tokens, context_ceiling
from src.core.runtime import (
    Run, ToolCall, ToolResult, Artifact, RunStatus, Task,
    Session, RunCoordinator, PermissionPolicy, ToolRuntime, Compactor,
)
from src.utils import SHORTHAND_RE, TERMINAL_TOOLS, TraceBus, TurnIntent, parse_turn, strip_ptc

logger = logging.getLogger(__name__)
console = Console()


class _StreamFilter:
    """Streams prose to stdout, hiding code fences and ``::tool`` lines.

    With the bus collapsed, the streamed text is the only place raw model
    output can still reach the terminal. Fenced Python is hidden; now the
    ``::tool`` shorthand is hidden too, because it is a tool invocation, not
    an answer — it renders once, as the collapsed tool row.
    """

    HOLD = 400

    def __init__(self, thought_cb=None):
        self.buf = ""
        self.in_fence = False
        self.thought_cb = thought_cb
        self.wrote = False
        self.emitted = ""

    def _push_thought(self, text: str):
        if not self.thought_cb or not text.strip():
            return
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if lines:
            self.thought_cb(lines[-1][:60])

    def _emit(self, text: str) -> None:
        if not text.strip():
            return
        sys.stdout.write(text)
        sys.stdout.flush()
        self.wrote = True
        self.emitted = (self.emitted + text)[-20_000:]

    def _drain(self, final: bool = False) -> None:
        while "\n" in self.buf:
            line, _, self.buf = self.buf.partition("\n")
            if not SHORTHAND_RE.match(line):
                self._emit(line + "\n")
                self._push_thought(line)
        if final and self.buf:
            line, self.buf = self.buf, ""
            if not SHORTHAND_RE.match(line):
                self._emit(line)
                self._push_thought(line)
        elif len(self.buf) > self.HOLD:
            self._drain(final=True)

    def __call__(self, token: str) -> None:
        self.buf += token
        while "```" in self.buf:
            pre, _, post = self.buf.partition("```")
            if not self.in_fence:
                self.buf = pre + "\n"
                self._drain(final=True)
            self.in_fence = not self.in_fence
            self.buf = post
        if not self.in_fence:
            self._drain()

    def flush(self) -> None:
        """Emit the trailing partial line once the provider is done."""
        if not self.in_fence:
            self._drain(final=True)
        # A trace line starts on its own row: without this, the answer receipt
        # lands glued to the last streamed character (``…structure.19:36:41``).
        if self.wrote and not self.emitted.endswith("\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.emitted += "\n"


class StemAgent:
    WRITE_TOOLS = {"write_file", "append_file", "edit_file", "delete_file"}

    def __init__(
        self,
        name: str,
        provider: Optional[AIProvider] = None,
        skill_name: str = "system",
        settings: Optional[GlobalSettings] = None,
        mode: str = "BUILD",
        memory: Any = None,
        initial_history: Optional[List[Dict]] = None,
        silent: bool = False,
        orchestrator: Optional[Any] = None,
    ):
        self.name = name
        self.provider = provider
        self.skill_name = skill_name
        self.settings = settings or load_config()
        self.mode = mode.upper() if mode else "BUILD"
        self.memory = memory
        self.session = Session(seed=initial_history)
        self._history: List[Dict[str, str]] = self.session.messages
        self.coordinator = RunCoordinator()
        self.tools: Dict[str, Any] = {}
        self.interrupt_event = asyncio.Event()
        self.orchestrator = orchestrator
        self._mcp = None
        self._mcp_initialized = False
        self._max_steps_override: Optional[int] = None
        self._silent = silent
        self._status = None
        self.status_cb = None
        self._is_delegated = False
        self._active_todos: List[str] = []
        self._user_active = False
        self._last_result = None
        self._last_stable_result = None
        self._repeat_count = 0
        self._cached_system_prompt: Optional[str] = None
        self._compressor = None
        self._prompt_hash = None
        self._prompt_obj_ref = None
        self._response_called = False
        self._response_value = ""
        self._run: Optional[Run] = None
        self._trace_parent_id = ""
        self._tripwire_errors: Dict[str, int] = {}
        self._terminal_calls: set[str] = set()
        self._task_query = ""
        self._sensor_attempts = 0
        self._last_streamed = False
        self._last_streamed_text = ""
        self._answer_painted = False
        self._status_base = ""
        self._background_tasks: set[asyncio.Task] = set()
        self.trace = TraceBus(
            role=name,
            enabled=bool(getattr(self.settings, "trace_enabled", True)),
            capture_chars=int(getattr(self.settings, "trace_capture_chars", 2000) or 2000),
        )
        self.trace.paint = not silent
        self._load_tools()
        self.tools["set_mode"] = self._tool_set_mode
        self._runtime = None

    @property
    def history(self) -> List[Dict[str, str]]:
        """Messages on the active session branch."""
        if not hasattr(self, "_history"):
            self._history = []
        return self._history

    @history.setter
    def history(self, value: List[Dict[str, str]]) -> None:
        self._history = list(value)
        session = getattr(self, "session", None)
        if session is not None:
            session.reset(self._history)
            self._history = session.messages

    def _build_runtime(self) -> ToolRuntime:
        """Wire the policy, budget and hooks for one run."""
        from src.config import get_allowed_roots
        return ToolRuntime(
            tools=self._active_tools(),
            policy=PermissionPolicy(
                permission_mode=getattr(self.settings, "permission_mode", "ASK"),
                mode=self.mode,
                roots=get_allowed_roots(),
            ),
            run=self._run,
            timeout=float(getattr(self.settings, "tool_timeout", 60) or 60),
            agent=self.name,
            on_event=self._runtime_event,
            on_permission=self.request_permission,
        )

    def _runtime_event(self, kind: str, **fields) -> None:
        """Route one tool event to the trace bus; a call and its result are one row.

        ``response()`` is a loop verb, not a tool: its call and result are
        tracked by ``call_id`` and dropped before either becomes a row.
        """
        name = fields.get("name") or ""
        call_id = fields.get("call_id") or ""
        if name in TERMINAL_TOOLS:
            if call_id:
                self._terminal_calls.add(call_id)
            return
        if call_id and call_id in self._terminal_calls:
            if kind == "tool_result":
                self._terminal_calls.discard(call_id)
            return
        if kind == "tool_call":
            self.trace.tool_call(call_id, name, fields.get("input") or {})
            self._emit_run(kind, self.name, name=name, status=fields.get("status") or "",
                           input=fields.get("input"), output=fields.get("output"))
            return
        if kind == "tool_result":
            ok = (fields.get("status") or "") == "ok"
            self.trace.tool_result(
                call_id,
                name,
                ok=ok,
                output=fields.get("output"),
                error=None if ok else fields.get("output"),
                duration=float(fields.get("duration") or 0.0),
            )
            self._emit_run(kind, self.name, name=name, status=fields.get("status") or "",
                           input=fields.get("input"), output=fields.get("output"),
                           duration_ms=float(fields.get("duration") or 0.0) * 1000)
            return
        self._emit_run(
            kind,
            self.name,
            name=name,
            status=fields.get("status") or "",
            input=fields.get("input"),
            output=fields.get("output"),
            duration_ms=float(fields.get("duration") or 0.0) * 1000,
        )

    def _format_tool_output(self, tool_name: str, result: str) -> str:
        if not result:
            return ""
        if result.strip().startswith(("{", "[")):
            try:
                data = json.loads(result)
                if isinstance(data, list) and len(data) > 3 and isinstance(data[0], dict):
                    summary = [f"Item {i+1}: {json.dumps(item, ensure_ascii=False)[:150]}" for i, item in enumerate(data[:5])]
                    return f"[JSON Array: {len(data)} items]\n" + "\n".join(summary)
                if isinstance(data, dict) and len(result) > 2000:
                    return f"[JSON Object Keys: {list(data.keys())}] (Truncated. Use specific extraction if needed)."
            except json.JSONDecodeError:
                pass
        if "<html" in result.lower()[:500] or "<!doctype" in result.lower()[:500] or "<div" in result.lower()[:500]:
            text = re.sub(r'<[^>]+>', ' ', result)
            text = re.sub(r'\s+', ' ', text).strip()
            return f"[HTML Extracted Text]: {text[:1500]}"
        TRUNC_LIMIT = getattr(self.settings, 'tool_output_limit', 8000)
        if len(result) > TRUNC_LIMIT:
            return result[:TRUNC_LIMIT] + f"\n... [TRUNCATED {len(result) - TRUNC_LIMIT} CHARS — use read with offset to get the rest]"
        return result

    async def _tool_set_mode(self, mode: str) -> str:
        old = self.mode
        self.mode = mode.upper()
        self._runtime = self._build_runtime()
        self.invalidate_cache()
        return f"Mode changed: {old} -> {self.mode}"

    @classmethod
    async def create(cls, role: str, mode: str = "BUILD", settings=None, memory=None, initial_history: Optional[List[Dict]] = None) -> "StemAgent":
        settings = settings or load_config()
        provider = get_provider(getattr(settings, 'provider', 'pollinations'), settings) or get_provider('pollinations', settings)
        return cls(name=role, provider=provider, skill_name=role, settings=settings, mode=mode, memory=memory, initial_history=initial_history)

    def _load_tools(self) -> None:
        role_cfg = load_role(self.skill_name)
        allowed = set(getattr(role_cfg, "tools", []) or [])
        if self.skill_name == "system" or "all" in allowed:
            allowed = set(TOOL_REGISTRY.keys())
        else:
            allowed.update({"response", "load_skill", "find_skill", "search_skill_hub", "install_skill",
                            "search_memory", "search_tgs", "list_skills", "list_mcp_servers",
                            "set_external_skills", "scan_tools"})
        self.tools = {k: v for k, v in TOOL_REGISTRY.items() if k in allowed}

    def _mcp_tools(self) -> Dict[str, Any]:
        """Every connected MCP tool under the name the manager published."""
        manager = self._mcp
        if not getattr(self.settings, "mcp_expose_direct_tools", True) or not manager:
            return {}
        tools: Dict[str, Any] = {}
        for public, (server_name, tool_name) in dict(getattr(manager, "_calls", {})).items():
            server = getattr(manager, "servers", {}).get(server_name)
            spec = (getattr(server, "tools", {}) or {}).get(tool_name)
            if public in tools or spec is None:
                continue
            tools[public] = self._make_mcp_wrapper(tool_name, spec, server_name)
        return tools

    def _active_tools(self) -> dict:
        write_set = {"write_file", "append_file", "edit_file", "delete_file", "shell"}
        tools = dict(self.tools)
        for name, func in self._mcp_tools().items():
            tools.setdefault(name, func)
        if self.mode == "BUILD":
            return tools
        return {k: v for k, v in tools.items() if k not in write_set}

    def _tool_specs(self) -> Dict[str, Any]:
        """Active tool callables, keyed by public name (for argument binding)."""
        return {name: func for name, func in self._active_tools().items()
                if name not in TERMINAL_TOOLS}

    def _native_specs(self) -> List[Dict[str, Any]]:
        """Canonical tool descriptions, or [] when the wire stays text-only.

        ``auto`` only opts known tool-capable backends in: a third-party
        OpenAI-compatible preset may reject ``tools`` outright, and failing
        every turn is worse than using shorthand there.
        """
        protocol = str(getattr(self.settings, "tool_protocol", "auto") or "auto").lower()
        if protocol not in ("native", "auto") or self.provider is None:
            return []
        if not getattr(self.provider, "supports_native_tools", False):
            return []
        if protocol == "auto":
            from src.core.provider import NATIVE_TOOL_PROVIDERS
            if str(getattr(self.provider, "name", "")) not in NATIVE_TOOL_PROVIDERS:
                return []
        return _native_tool_schemas(self._tool_specs())

    @staticmethod
    def _native_markup(calls: Iterable[Dict[str, Any]]) -> str:
        """Native calls re-enter the loop as the shorthand it already parses."""
        lines = []
        for call in calls:
            name = str(call.get("name") or "").strip()
            if not name:
                continue
            args = call.get("arguments")
            arguments = args if isinstance(args, dict) else {}
            lines.append(f"::tool {name} {json.dumps(arguments, ensure_ascii=False)}")
        return "\n".join(lines)

    async def _response_verb(self, text: Optional[str] = None) -> str:
        """End the turn with a user-facing answer. Never reaches ToolRuntime."""
        if text is not None and not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False, indent=2)
        self._response_called = True
        self._response_value = text or ""
        return self._response_value

    def _ptc_tools(self) -> Dict[str, Any]:
        """Async wrappers binding each tool's signature to a runtime call.

        ``response`` is the loop verb that ends a turn: it is injected straight
        into the PTC namespace, so it produces no tool row, policy check or
        budget entry.
        """
        tools: Dict[str, Any] = {}
        active = self._active_tools()
        if any(name in TERMINAL_TOOLS for name in active):
            tools["response"] = self._response_verb
        for name, func in active.items():
            if name in TERMINAL_TOOLS:
                continue
            try:
                signature = inspect.signature(func)
            except (TypeError, ValueError):
                signature = None

            async def _call(*args, _name=name, _sig=signature, **kwargs):
                if _sig is not None:
                    try:
                        kwargs = dict(_sig.bind(*args, **kwargs).arguments)
                    except TypeError:
                        pass
                return await self._execute_tool(_name, kwargs)

            _call.__name__ = name
            tools[name] = _call
        return tools

    async def _init_mcp(self) -> None:
        if self._mcp_initialized or not getattr(self.settings, 'mcp_enabled', True):
            return
        try:
            from src.core.mcp_manager import MCPManager
            self._mcp = MCPManager(self.settings)
            self._mcp_initialized = True
            if getattr(self.settings, "mcp_connect_on_start", True):
                await self._mcp.connect_all()
        except Exception as e:
            logger.warning(f"MCP init failed (will retry): {e}")

    def _make_mcp_wrapper(self, tool_name: str, spec: Any = None, server: str = ""):
        """Wrap one MCP tool, publishing the server's real parameter schema."""
        mcp = self._mcp
        async def _mcp_call(**kwargs):
            args = kwargs if kwargs else {}
            if "arguments" in args and isinstance(args["arguments"], dict) and len(args) == 1:
                args = args["arguments"]
            return await mcp.execute_tool(tool_name, args, server=server or None)
        schema = _mcp_schema(spec)
        _mcp_call.__signature__ = _schema_signature(tool_name, schema)
        _mcp_call.__doc__ = _schema_doc(tool_name, schema, spec)
        return _mcp_call

    async def cleanup(self) -> None:
        if self._background_tasks:
            tasks = tuple(self._background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp and self._mcp_initialized:
            try:
                await self._mcp.cleanup()
            except Exception:
                pass
        from src.core.mcp_manager import MCPManager
        MCPManager._instance = None
        self._mcp = None
        self._mcp_initialized = False

    def request_interrupt(self) -> None:
        self.interrupt_event.set()
    
    def invalidate_cache(self):
        self._cached_system_prompt = None
        self._prompt_hash = None

    async def request_permission(self, question: str, details: str = "", default: bool = False) -> bool:
        mode = getattr(self.settings, "permission_mode", "ASK")
        if mode == "AUTO":
            return True
        if mode == "DENY":
            return False
        ask = getattr(self, "_ask_user", None)
        if ask:
            return await ask(question, details, default)
        return default

    async def _ask_user(self, question: str, details: str = "", default: bool = False) -> bool:
        return default
    
    
    def _emit_run(self, kind, actor, name="", status="", input=None, output=None, error="", duration_ms=0.0, parent_id=""):
        if not self._run:
            return None
        from src.utils import redact_fast
        cap = max(200, int(getattr(self.settings, "trace_capture_chars", 2000)))
        def clean(v):
            if v is None:
                return None
            t = redact_fast(str(v))
            return t[:cap] + ("..." if len(t) > cap else "")
        meta = {"error": clean(error)} if error else None
        event = self._run.emit(kind, actor, name, status, clean(input), clean(output), duration_ms, parent_id or self._trace_parent_id, meta)
        try:
            payload = event.as_dict() if hasattr(event, "as_dict") else {"event": str(event)}
        except Exception:
            payload = {"event": str(event)}
        # Structured detail belongs in the JSONL, not in agent.log: the log
        # stays line-for-line comparable with the terminal.
        self.trace.record("run_event", actor, f"{kind} {name} {status}".strip(),
                          run_event=payload)
        return event

    def _record_error_tripwire(self, error: str) -> bool:
        if not self._run:
            return False
        normalized = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", str(error).lower())
        normalized = re.sub(r"\d+(?:\.\d+)?s", "<n>s", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()[:300]
        if not normalized:
            return False
        count = self._tripwire_errors.get(normalized, 0) + 1
        self._tripwire_errors[normalized] = count
        threshold = max(2, int(getattr(self.settings, "tripwire_error_repeats", 3)))
        if count < threshold:
            return False
        self._run.metadata.setdefault("tripwires", {})[normalized] = count
        self._emit_run("tripwire", self.name, "repeated_error", "triggered", output=normalized)
        return True

    def _get_trigger_input(self) -> str:
        return self.history[-1]["content"] if self.history else ""

    async def _trim_history(self) -> None:
        """Compact history down to the model's context budget."""
        if not self.settings.history_trim_enabled:
            return
        from src.config import resolve_provider
        from src.core.provider import get_model_name
        provider_name = resolve_provider(getattr(self.settings, "provider", None) or "pollinations")
        model_name = getattr(self.provider, "model", "") or get_model_name(provider_name, self.settings)
        window = Compactor.budget(provider_name, model_name, self.settings)
        if not window:
            return
        threshold = context_ceiling(window)
        if Compactor.used(self.history) <= threshold:
            return
        keep = max(2, int(getattr(self.settings, "history_trim_size", 20) or 20))
        if self._compressor is None:
            self._compressor = Compactor(self.provider, getattr(self, "session", None))
        compressor = self._compressor
        if hasattr(compressor, "compact"):
            compacted = await compressor.compact(list(self.history), threshold, keep=keep)
        else:
            compacted = await compressor.compress(list(self.history), keep_tokens=max(1000, int(threshold * 0.3)))
        if compacted:
            self.history = compacted
        if Compactor.used(self.history) > threshold:
            body = [m for m in self.history if m.get("role") != "system"]
            head = [m for m in self.history if m.get("role") == "system"]
            cut = max(0, len(body) - keep)
            while cut < len(body) and body[cut].get("role") in ("tool", "toolResult"):
                cut += 1
            self.history = head + body[cut:]
        self._cached_system_prompt = None

    def _prompt_header(self, role_cfg) -> str:
        role_prompt = role_cfg.prompt if role_cfg else "You are a ZervGen Agent."
        tier = getattr(self.settings, "active_model_tier", "COOL")
        return f"{role_prompt}\n=== OP STATE ===\nROLE: {self.skill_name}\nMODE: {self.mode}\nTIER: {tier}"

    def _prompt_run(self) -> str:
        if not self._run:
            return ""
        b = self._run.budget
        return (
            "--- RUN ---\n"
            f"ID: {self._run.id}\n"
            f"STATUS: {self._run.status.value}\n"
            f"STEPS: {getattr(b, 'steps', 0)}/{getattr(b, 'max_steps', 0)}\n"
            f"TOOLS: {getattr(b, 'tool_calls', 0)}/{getattr(b, 'max_tool_calls', 0)}\n"
            f"DELEGATIONS: {getattr(b, 'delegations', 0)}/{getattr(b, 'max_delegations', 0)}"
        )

    @property
    def _memory_on(self) -> bool:
        """Memory is wired and not disabled by settings."""
        return bool(self.memory) and bool(getattr(self.settings, "memory_enabled", True))

    def _prompt_memory(self, user_input: str) -> str:
        if not self._memory_on:
            return ""
        try:
            limit = max(1, int(getattr(self.settings, "prompt_memory_limit", 4)))
            value = self.memory.inject_context(user_input, limit=limit, trusted_sources={"user", "tool_result", "delegation"})
            return f"--- MEMORY ---\n{value}" if value else ""
        except Exception as e:
            logger.debug("Prompt memory failed: %s", e)
            return ""

    def _prompt_peer_cards(self, user_input: str) -> str:
        try:
            from src.core.memory import PeerCards
            limit = max(1, int(getattr(self.settings, "prompt_peer_card_limit", 3)))
            cards = PeerCards().get_relevant(self.skill_name, user_input, limit=limit)
            if cards:
                return "--- PEER CARDS ---\n" + "\n\n".join(c.to_prompt_block() for c in cards)
        except Exception as e:
            logger.debug("Prompt peer cards failed: %s", e)
        return ""

    def _prompt_mcp(self) -> str:
        if not getattr(self.settings, "mcp_enabled", True) or not self._mcp:
            return ""
        if not getattr(self.settings, "prompt_show_mcp_tools", False):
            return ""
        active = []
        for name, srv in self._mcp.servers.items():
            if srv.connected:
                active.append(f"- {name}: {len(srv.tools)} tools")
            elif srv.state in ("degraded", "dead"):
                active.append(f"- {name}: {srv.state} ({len(srv.tools)} cached tools) — call mcp_execute to retry")
        return "--- MCP ---\n" + "\n".join(active) if active else ""

    def _prompt_skills(self, user_input: str) -> str:
        """Skill topology: a category-grouped index; bodies load on demand."""
        try:
            from src.skills_loader import skill_index
            show_internal = bool(getattr(self.settings, "prompt_show_internal_skills"))
            show_external = bool(getattr(self.settings, "prompt_show_external_skills"))
            auto_internal = bool(getattr(self.settings, "prompt_auto_trigger_skills"))
            auto_external = bool(getattr(self.settings, "prompt_auto_trigger_external_skills"))
            limit = max(1, int(getattr(self.settings, "prompt_max_skills", 150) or 150))
            desc_chars = max(20, int(getattr(self.settings, "prompt_skill_description_chars", 90) or 90))
            budget = max(500, int(getattr(self.settings, "prompt_skills_chars", 7000) or 7000))
            visible = [
                skill for skill in skill_index.visible()
                if (show_external if skill.external else show_internal)
            ]
            matched = {
                skill.name for skill in visible
                if (auto_external if skill.external else auto_internal)
                and skill.match_trigger(user_input)
            }
            return skill_index.index(
                budget_chars=budget, desc_chars=desc_chars, max_skills=limit,
                matched=matched, skills=[s for s in visible if s.category],
            )
        except Exception as e:
            logger.debug("Prompt skills failed: %s", e)
            return ""

    def _prompt_protocol(self) -> str:
        """Execution protocol; native backends get the tool-first variant."""
        text = ZG_PROTOCOL.strip()
        if self._native_specs():
            text += ("\n- Native tool calls are enabled: call a tool directly and end with a plain answer."
                     " Do not print the call as text.")
        return f"--- EXECUTION PROTOCOL ---\n{text}"

    def _prompt_tools(self, declared_names: set[str] | None = None) -> str:
        active = self._active_tools()
        if not active:
            return ""
        from src.tools import get_tools_schema
        subset = {k: v for k, v in active.items() if declared_names is None or k in declared_names}
        return "--- AVAILABLE TOOLS ---\n" + get_tools_schema(subset, compact=True)

    def _prompt_project_rules(self) -> str:
        try:
            from src.utils import discover_context_files, redact_fast
            text = discover_context_files(".")
            if not text:
                return ""
            return "--- PROJECT RULES ---\n" + redact_fast(text)
        except Exception as e:
            logger.debug("Prompt project rules failed: %s", e)
            return ""

    def _build_system_prompt(self, role_cfg, include_roles: bool = False, user_input: str = "") -> str:
        sections = [
            ("header", self._prompt_header(role_cfg), 0),
            ("run", self._prompt_run(), 1),
            ("protocol", self._prompt_protocol(), 0),
            ("rules", truncate_to_tokens(self._prompt_project_rules(), 1500, "\n[Project rules truncated]"), 2),
            ("tools", self._prompt_tools(), 3),
            ("skills", self._prompt_skills(user_input), 4),
            ("mcp", self._prompt_mcp(), 5),
            ("memory", truncate_to_tokens(self._prompt_memory(user_input), 750, "\n[Memory truncated]"), 6),
            ("peers", truncate_to_tokens(self._prompt_peer_cards(user_input), 500, "\n[Peer cards truncated]"), 7),
            ("context", f"--- CONTEXT ---\n{get_system_context()}", 8),
        ]
        prompt = "\n\n".join(text for _, text, _ in sections if text)
        cap = int(getattr(self.settings, "prompt_max_chars", 0) or 0)
        if cap and len(prompt) > cap:
            dropped: set[str] = set()
            for name, text, priority in sorted(sections, key=lambda s: -s[2]):
                if len(prompt) <= cap:
                    break
                if not text or name in dropped or priority < 1:
                    continue
                dropped.add(name)
                prompt = "\n\n".join(t for n, t, _ in sections if t and n not in dropped)
                self.trace.record("prompt_drop", self.name, f"{name} ({len(text)} chars)")
            if len(prompt) > cap:
                prompt = prompt[:cap] + "\n[System prompt truncated]"
        self._prompt_obj_ref = prompt
        return prompt

    def _log(self, role: str, content: str, event_type: str = "message", mode: str = "") -> None:
        """Record one event: the bus renders + logs it once; history is written once.

        No ``logger.info`` here on purpose — the bus owns the log line, so
        ``tmp/agent.log`` and the terminal show the same sequence instead of
        the terminal showing four lines for one event and the log eight.
        """
        from src.utils import redact_fast
        safe_content = redact_fast(content)
        self.trace.emit(event_type, role, safe_content)
        if self._memory_on:
            self.memory.log_event_sync(role, safe_content, event_type, mode or self.mode)
        if event_type in ("input", "agent_start", "delegation_result"):
            if event_type == "input":
                safe_role = "user"
            elif role in ("system", "user", "assistant"):
                safe_role = role
            else:
                safe_role = "assistant"
            self.history.append({"role": safe_role, "content": self._strip_ptc(safe_content)})

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any], timeout: Optional[float] = None) -> str:
        """Dispatch one tool; the calling agent is visible to the tool."""
        runtime = self._runtime or self._build_runtime()
        if timeout is None and tool_name == "delegate_to":
            timeout = float(getattr(self.settings, "delegation_timeout", 300) or 300)
        if tool_name in self.WRITE_TOOLS and self._run is not None and not self._run.budget.write():
            limit = self._run.budget.max_file_writes
            return f"Error: file-write budget exhausted ({limit} per run)"
        token = current_agent.set(self)
        try:
            outcome = await runtime.execute(tool_name, args, timeout=timeout)
        finally:
            current_agent.reset(token)
        if outcome.success:
            output = str(outcome.output or "")
            if tool_name in {"write_file", "append_file", "edit_file"} and self._run:
                path = str(args.get("path", ""))
                if path:
                    self._run.artifacts.append(Artifact(path=path))
                    if getattr(self.settings, "sensors_enabled", True):
                        problem = self._check_file(Path(path))
                        if problem:
                            self.trace.record("sensor", self.name, problem)
                            output = f"{output}\nSENSOR: {problem}".strip()
            if tool_name in ("read_file", "fetch_url") and output and len(output) <= 500_000:
                try:
                    d = Path("tmp/trace")
                    d.mkdir(parents=True, exist_ok=True)
                    ref = str(args.get("path") or args.get("url") or "blob")
                    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ref)[:40] or "blob"
                    copy_path = d / f"{tool_name}_{slug}_{int(time.time())}.txt"
                    copy_path.write_text(output, encoding="utf-8", errors="replace")
                except Exception:
                    pass
            return self._format_tool_output(tool_name, output)
        error_msg = outcome.error or "Error: tool failed"
        if self._record_error_tripwire(f"{tool_name}: {error_msg}"):
            return f"Error: TRIPWIRE repeated failure for {tool_name}. Escalate or change strategy."
        return error_msg

    @staticmethod
    def _strip_ptc(text: str) -> str:
        text = re.sub(r'<([a-zA-Z_][\w-]*)>.*?</\1>', '', text, flags=re.DOTALL)
        return re.sub(r'```python\s*\n.*?\n\s*```', '', text, flags=re.DOTALL).strip()

    @staticmethod
    def _auto_return(code: str) -> str:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return code
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = tree.body[-1]
            tree.body[-1] = ast.copy_location(ast.Return(value=last.value), last)
            ast.fix_missing_locations(tree)
            return ast.unparse(tree)
        return code

    def _auto_await_tools(self, code: str, names: set) -> str:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return code
        conc = {"gather", "create_task", "wait_for", "ensure_future", "shield", "as_completed"}
        excluded = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in conc:
                excluded.update(id(a) for a in n.args if isinstance(a, ast.Call))
        awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
        class _AA(ast.NodeTransformer):
            def visit_Call(self, node):
                self.generic_visit(node)
                if (id(node) not in excluded and id(node) not in awaited and isinstance(node.func, ast.Name) and node.func.id in names):
                    return ast.copy_location(ast.Await(value=node), node)
                return node
        tree = _AA().visit(tree)
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    def _looks_like_shell(self, text: str) -> List[tuple[str, str]]:
        blocks = []
        for match in re.finditer(r'```(bash|cmd|shell|powershell|sh)\s*\n(.*?)\n\s*```', text, re.DOTALL | re.IGNORECASE):
            lang = match.group(1).lower()
            code = match.group(2).strip()
            if code:
                blocks.append((lang, code))
        return blocks

    BLOCKED_NAMES = {
        "eval", "exec", "compile", "__import__", "globals", "locals", "vars", "breakpoint",
        "input", "open", "getattr", "setattr", "delattr", "object", "super", "memoryview",
    }
    BLOCKED_MODULES = {"os", "sys", "subprocess", "socket", "ctypes", "builtins", "importlib", "pathlib", "shutil"}
    # str.format resolves attributes inside string data, which the AST cannot see:
    # "{0.__globals__}".format(fn) escapes. f-strings are checked as normal nodes.
    BLOCKED_ATTRS = {"format", "format_map"}

    def _validate_ptc(self, code: str) -> Optional[str]:
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return f"Syntax Error: {e}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in self.BLOCKED_ATTRS:
                return "PTC blocked: str.format can traverse private attributes. Use f-strings."
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
                return "PTC blocked: imports and global/nonlocal mutations not permitted in scripts. Use provided tools."
            if isinstance(node, ast.Name) and (node.id in self.BLOCKED_NAMES or node.id.startswith("_")):
                return f"PTC blocked: {node.id}"
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                return f"PTC blocked: private attribute access '{node.attr}'"
            if isinstance(node, ast.Name) and node.id in self.BLOCKED_MODULES:
                return f"PTC blocked: direct access to '{node.id}' module"
        return None

    def _log_ptc_error(self, message: str, full_response: str = "") -> None:
        captured = full_response.strip()
        if captured:
            message = f"{message}\nMODEL OUTPUT:\n{captured}"
        self._log(self.name, message, "ptc_error", self.mode)

    def _log_error_with_traceback(self, message: str, exc: Exception) -> None:
        import traceback
        tb = traceback.format_exc()
        from src.utils import redact_traceback
        self._log(self.name, f"{message}\n{redact_traceback(tb)}", "error", self.mode)

    async def _run_code(self, code: str, full_response: str = "") -> str:
        if not code:
            return "Error: Empty code"
        validation_error = self._validate_ptc(code) if getattr(self.settings, "ptc_strict", True) else None
        if validation_error:
            self._log_ptc_error(validation_error, full_response)
            return validation_error
        if code.count('"""') % 2 != 0:
            code += '\n"""'
        if code.count("'''") % 2 != 0:
            code += "\n'''"
        raw_text = ""
        if full_response and "```" in full_response:
            raw_text = full_response[:full_response.index("```")].strip()
        first_sentence = ""
        if raw_text:
            first_sentence = raw_text.splitlines()[0][:80].strip()
            for ch in (". ", "! ", "? "):
                idx = raw_text.find(ch)
                if idx != -1:
                    first_sentence = raw_text[:idx + 1].strip()
                    break
        comments = [l.strip().lstrip("#").strip() for l in code.split("\n") if l.strip().startswith("#")]
        if raw_text and comments:
            comment_thought = " -> ".join(c[:60] for c in comments[:3])
        elif raw_text:
            non_comments = [l.strip() for l in code.split("\n") if l.strip() and not l.strip().startswith("#")]
            comment_thought = non_comments if non_comments else ""
        else:
            comment_thought = ""
        thought = first_sentence or comment_thought or "Thinking..."
        self._set_status(True, f"▶ {thought}")
        # PTC is a transport, not an event: one record with a compact head, no render.
        self.trace.emit("ptc", self.name, code[: self.trace.capture_chars], paint=False)
        ptc_tools = self._ptc_tools()
        async_names = set(ptc_tools)
        fixed_code = self._auto_await_tools(code, async_names)
        fixed_code = self._auto_return(fixed_code)
        fixed_code = re.sub(r'\basyncio\.run\s*\(\s*([^)]+)\s*\)', r'await \1', fixed_code)
        is_delegate = bool(re.search(r'\bdelegate_to\s*\(', fixed_code))
        print_outputs = []
        def _captured_print(*args, **kwargs) -> None:
            print_outputs.append(" ".join(str(a) for a in args))
        safe_builtins = {
            "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict, "enumerate": enumerate,
            "float": float, "int": int, "len": len, "list": list, "max": max, "min": min,
            "range": range, "round": round, "set": set, "sorted": sorted, "str": str,
            "sum": sum, "tuple": tuple, "zip": zip, "isinstance": isinstance, "type": type,
            "map": map, "filter": filter, "hasattr": hasattr, "bytes": bytes, "bytearray": bytearray,
            "next": next, "iter": iter, "Exception": Exception, "KeyError": KeyError, "TypeError": TypeError, 
            "ValueError": ValueError, "NotImplementedError": NotImplementedError, "RuntimeError": RuntimeError, 
            "format": format, "repr": repr, "reversed": reversed,
        }
        safe_asyncio = SimpleNamespace(
            gather=asyncio.gather,
            wait_for=asyncio.wait_for,
            create_task=asyncio.create_task,
            ensure_future=asyncio.ensure_future,
            shield=asyncio.shield,
            as_completed=asyncio.as_completed,
        )
        root_coordinator = getattr(self, "_orchestrator", None) or getattr(self, "orchestrator", None) or self
        class _ToolRuntimeView:
            async def request_permission(self, question, details="", default=False):
                return await self.owner.request_permission(question, details, default)
            async def _handle_delegation(self, args):
                if hasattr(self.root, "_handle_delegation"):
                    return await self.root._handle_delegation(args)
                raise RuntimeError("No active Orchestrator found to handle delegation.")
            def __init__(self, owner, root):
                object.__setattr__(self, "owner", owner)
                object.__setattr__(self, "root", root)
        safe_json = SimpleNamespace(dumps=json.dumps, loads=json.loads, JSONDecodeError=json.JSONDecodeError)
        g = {**ptc_tools, "json": safe_json, "asyncio": safe_asyncio, "print": _captured_print, "_orchestrator": _ToolRuntimeView(self, root_coordinator), "__builtins__": safe_builtins}
        lines = fixed_code.split("\n")
        indented = "\n".join(f"    {l}" for l in lines)
        wrapped = f"async def __execute():\n{indented}"
        try:
            compiled = compile(wrapped, '<string>', 'exec')
        except SyntaxError as e:
            err = f"Syntax Error: {e}"
            self._log(self.name, err, "ptc_error", self.mode)
            return err
        try:
            exec(compiled, g)
            timeout = getattr(self.settings, "delegation_timeout", 300) if is_delegate else self.settings.tool_timeout
            result = await asyncio.wait_for(g["__execute"](), timeout=timeout)
            output_str = "\n".join(print_outputs)
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            if result is not None and not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, (dict, list)) else str(result)
            if result and result.strip() and output_str:
                result = f"{output_str}\n{result}"
            elif output_str:
                result = output_str
            elif result is None:
                result = ""
            if result and not is_delegate and not self._response_called:
                self.trace.emit("ptc_result", self.name, str(result), paint=False)
            return str(result) if result else ""
        except PermissionError as e:
            err = f"PERMISSION DENIED: {e}"
            self._log_ptc_error(err, full_response)
            return err  
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            err = "Error: delegated agent timed out. Retry with a smaller sub-task, or do the work yourself with native tools." if is_delegate else "Error: Execution timed out"
            self._log_ptc_error(err, full_response)
            self._record_error_tripwire(f"ptc:{err}")
            return err
        except NameError as e:
            err_str = str(e)
            if "mcp_" in err_str:
                missing = re.search(r"mcp_\w+", err_str)
                tool = missing.group(0) if missing else "unknown"
                err = f"MCP Error: {tool} unavailable. Retry once; if it persists use native equivalents (fetch_url, read_file, shell)."
                self._log_ptc_error(err, full_response)
                return err
            err = f"NameError: {err_str}"
            self._log_ptc_error(err, full_response)
            self._record_error_tripwire(f"ptc:{err}")
            return err
        except TypeError as e:
            err_msg = f"TypeError: {e}"
            hint = ""
            m = re.search(r"(\w+)\(\) (?:got an unexpected|takes)", err_msg)
            if m and m.group(1) in self.tools:
                try:
                    hint = f"\nExpected signature: {m.group(1)}{inspect.signature(self.tools[m.group(1)])}"
                except Exception:
                    pass
            self._log(self.name, err_msg + hint, "ptc_error", self.mode)
            return f"Error: {err_msg}{hint}"
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            self._log_error_with_traceback(f"ERROR: {err_msg}", e)
            err = f"Error: {err_msg}"
            self._log_ptc_error(err, full_response)
            self._record_error_tripwire(f"ptc:{err}")
            return err

    def _get_max_steps(self) -> int:
        return self._max_steps_override or self.settings.max_steps

    def _live_thought(self, text: str):
        self._set_status(True, f"▶ {text}")

    def _streamed_answer(self, answer: str) -> bool:
        """Whether the final turn's stream already painted this answer."""
        if not self._last_streamed or not answer:
            return False
        streamed = " ".join(self._last_streamed_text.split())
        head = " ".join(answer.split())[:200]
        return bool(head) and head in streamed

    def _set_status(self, enabled: bool, label: str = "") -> None:
        if self._silent:
            return
        cb = getattr(self, "status_cb", None)
        if cb:
            if not enabled:
                cb("")
            elif label:
                cb(label)
            else:
                cb(getattr(self, "_status_base", "") or "thinking...")
            return
        if enabled:
            if self._status is None:
                self._status = console.status(getattr(self, "_status_base", ""), spinner="dots")
                self._status.__enter__()
        else:
            if self._status is not None:
                self._status.__exit__(None, None, None)
                self._status = None

    def inject_user_message(self, text: str) -> None:
        if text and text.strip():
            self.coordinator.ring(text)

    async def _handle_llm_turn(self, system_prompt: str) -> str:
        if self.interrupt_event.is_set():
            raise asyncio.CancelledError("Run aborted by user.")
        mode_def = MODES.get(self.mode, MODES["BUILD"])
        delim = generate_random_delimiter(16)
        full_prompt = f"{system_prompt}\n\n{delim}\nFOCUS: {mode_def['prompt']}\n{delim}"
        provider_timeout = getattr(self.settings, 'provider_timeout', 120)
        valid_roles = {"system", "user", "assistant"}
        safe_history = [{"role": m["role"] if m["role"] in valid_roles else "assistant", "content": m["content"]} for m in self.history]
        use_streaming = not self._silent and not self.settings.debug_mode
        self._last_streamed = False
        if self.settings.debug_mode and not self._silent:
            self._set_status(False)
            console.print(Panel(Text(full_prompt), title="[purple]INPUT[/purple]"))
        response = None
        self.provider.native_tools = self._native_specs()
        try:
            if use_streaming:
                self._set_status(False)
                stream = _StreamFilter(thought_cb=self._live_thought)
                response = await asyncio.wait_for(self.provider.generate_text(safe_history, full_prompt, on_token=stream), timeout=provider_timeout)
                stream.flush()
                # A trace line must start on its own row: without this, the
                # answer receipt lands glued to the last streamed character.
                if stream.wrote and not stream.emitted.endswith("\n"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    stream.emitted += "\n"
                self._set_status(True)
                self._last_streamed = stream.wrote
                self._last_streamed_text = stream.emitted
            else:
                self._last_streamed = False
                self._last_streamed_text = ""
                response = await asyncio.wait_for(self.provider.generate_text(safe_history, full_prompt), timeout=provider_timeout)
            from src.core.provider import _record_success
            _record_success(self.provider.name if hasattr(self.provider, 'name') else (self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider))
        except Exception as e:
            from src.core.provider import _record_failure
            p_name = self.provider.name if hasattr(self.provider, 'name') else (self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider)
            _record_failure(p_name)
            raise
        response = (response or "").strip()
        if response.startswith(delim) and response.endswith(delim):
            response = response[len(delim):-len(delim)].strip()
        native = list(getattr(self.provider, "native_calls", []) or [])
        if native:
            known = set(self._active_tools()) | TERMINAL_TOOLS
            markup = self._native_markup(call for call in native if call.get("name") in known)
            if markup:
                response = f"{response}\n{markup}".strip() if response else markup
        self._emit_run("model_output", self.name, "llm", "received", output=response[:2000])
        in_tok, out_tok = count_tokens(self.history, response)
        add_global_tokens(in_tok + out_tok)
        if self.provider:
            p_name = self.provider.name if hasattr(self.provider, 'name') else (self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider)
            add_provider_tokens(p_name, in_tok, out_tok)
        if self._run:
            provider_cost = float(getattr(self.provider, "_last_cost", 0.0) or 0.0)
            if provider_cost > 0:
                self._run.metadata["cost_usd"] = float(self._run.metadata.get("cost_usd", 0.0)) + provider_cost
                max_cost = float(getattr(self.settings, "max_cost_usd", 0.0) or 0.0)
                if max_cost and self._run.metadata["cost_usd"] > max_cost:
                    raise RuntimeError(f"Cost budget exceeded (${max_cost:.4f})")
        return response

    async def _run_verification_sensors(self) -> str:
        failures = [self._file_sensor_failures()]
        commands = list(getattr(self.settings, "verification_commands", []) or [])
        if self.mode == "BUILD" and commands and "shell" in self._active_tools():
            for command in commands:
                result = await shell_exec(command)
                if not result.ok:
                    failures.append(f"{command}: {result.to_text()}")
        return "\n".join(failure for failure in failures if failure)

    def _file_sensor_failures(self) -> str:
        """Compile or parse every artifact this run wrote; report what broke."""
        if not getattr(self, "_run", None) or not getattr(self.settings, "sensors_enabled", True):
            return ""
        failures, seen = [], set()
        for artifact in self._run.artifacts:
            if artifact.path in seen:
                continue
            seen.add(artifact.path)
            reason = self._check_file(Path(artifact.path))
            if reason:
                failures.append(reason)
        return "\n".join(failures)

    @staticmethod
    def _check_file(path: Path) -> str:
        """Computational check for one written file: exists and parses."""
        try:
            if not path.is_file():
                return f"{path}: missing"
            suffix = path.suffix.lower()
            if suffix == ".py":
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            elif suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            elif suffix in (".yaml", ".yml"):
                try:
                    import yaml
                except ImportError:
                    return ""
                yaml.safe_load(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            return f"{path}:{e.lineno}: {e.msg}"
        except json.JSONDecodeError as e:
            return f"{path}:{e.lineno}: invalid JSON: {e.msg}"
        except Exception as e:
            return f"{path}: {type(e).__name__}: {e}"
        return ""

    async def _verify_or_repair(self) -> Tuple[bool, Optional[str]]:
        """Run sensors. Returns (handled, terminal): a repair turn was queued, or the run ended."""
        failures = await self._run_verification_sensors()
        if not failures:
            self._sensor_attempts = 0
            return False, None
        self._sensor_attempts = getattr(self, "_sensor_attempts", 0) + 1
        limit = max(1, int(getattr(self.settings, "sensor_max_attempts", 3) or 3))
        self._record_incident(failures)
        if self._sensor_attempts >= limit:
            packet = (f"DECISION REQUIRED: verification still fails after {self._sensor_attempts} attempts.\n"
                      f"{failures[:1000]}\nSafest default: stop and report the failure.")
            self._log(self.name, packet, "sensor", self.mode)
            return True, self._finish(RunStatus.FAILED, packet)
        self._log(self.name, failures, "sensor", self.mode)
        self.history.append({"role": "user", "content":
                             f"Verification failed:\n{failures[:2000]}\nFix the failure and verify again."})
        self.invalidate_cache()
        return True, None

    def _record_incident(self, failures: str) -> None:
        """Append the failure to tmp/state/incidents.jsonl so it is never re-derived."""
        try:
            path = Path("tmp/state/incidents.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": round(time.time(), 3), "agent": self.name,
                                         "attempt": getattr(self, "_sensor_attempts", 0),
                                         "failures": failures[:2000]}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _finish(self, status: RunStatus, message: str) -> str:
        """Close the run with a terminal status exactly once."""
        if self._run is not None:
            final = self.coordinator.finish(status, self._run)
            if final is not None:
                self._emit_run("run_end", self.name, status=final.value, output=message)
            self._record_run_summary(status, message)
        self.trace.close()
        self._set_status(False)
        self._score_recalled_memory(status)
        return message

    def _record_run_summary(self, status: RunStatus, message: str) -> None:
        """One line per finished run in tmp/state/runs.jsonl."""
        run = self._run
        try:
            path = Path("tmp/state/runs.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            duration = round((run.ended_at or time.time()) - run.created_at, 2)
            record = {"run_id": run.id, "parent_id": run.parent_run_id, "agent": self.name,
                      "status": status.value, "steps": run.budget.steps,
                      "tools": len(run.tool_calls), "cost_usd": round(float(run.metadata.get("cost_usd", 0.0)), 6),
                      "duration_s": duration, "goal": (run.goal or "")[:120],
                      "answer_chars": len(message or "")}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _score_recalled_memory(self, status: RunStatus) -> None:
        """Feed the task outcome back into the facts this run recalled."""
        if not self._memory_on or self._is_delegated or not self._task_query:
            return
        query, self._task_query = self._task_query, ""
        try:
            self.memory.record_outcome(query, success=status == RunStatus.DONE)
        except Exception:
            logger.debug("Outcome scoring failed", exc_info=True)

    async def _finish_response(self) -> Optional[str]:
        """Close a response()-ended turn, or re-prompt when verification still fails."""
        self._response_called = False
        self._repeat_count = 0
        self._last_stable_result = None
        queued, terminal = await self._verify_or_repair()
        if terminal is not None:
            return terminal
        if queued:
            return None
        answer = self._response_value or ""
        self.trace.answer(answer or "(empty)", streamed=self._streamed_answer(answer))
        if answer and (not self.history or self.history[-1].get("content") != answer):
            self.history.append({"role": "assistant", "content": answer})
        return self._finish(RunStatus.DONE, answer)

    async def run(self, task: str) -> str:
        self.interrupt_event.clear()
        self._tripwire_errors = {}
        self._terminal_calls.clear()
        if not task or not isinstance(task, str):
            return "Error: Invalid task"
        task = task.strip()
        if not task:
            return "Error: Empty task"
        if self.provider is None:
            return "Error: No provider configured"
        await self._init_mcp()
        sanitized = sanitize_for_prompt(task)
        self._task_query = sanitized
        is_new_task = not self.history and self._is_delegated
        self._log("user", sanitized, "input" if not self._is_delegated else ("agent_start" if is_new_task else "input"))
        try:
            from src.skills_loader import skill_index
            skill_contract = skill_index.get(self.skill_name)
            if skill_contract:
                pre_err = SkillEngine.validate_pre(skill_contract, {"task": task})
                if pre_err:
                    return f"PRE-VALIDATION FAILED: {pre_err}"
        except Exception:
            pass
        step = 0
        max_steps = self._get_max_steps()
        self._delegation_count = 0
        self._active_todos = []
        self.invalidate_cache()
        self._response_called = False
        self._response_value = ""
        self._answer_painted = False
        self._set_status(True)
        self._run = Run.create(task, self.name, max_steps=max_steps, parent_run_id=getattr(getattr(self, "_parent_run", None), "id", None))
        self._run.status = RunStatus.RUNNING
        self.trace.bind(self._run.id)
        self.trace.paint = not self._silent
        self.trace.paint = not self._silent
        self.coordinator.begin()
        self._runtime = self._build_runtime()
        self._run.budget.max_tool_calls = getattr(self.settings, "max_tool_calls", 200)
        self._run.budget.max_delegations = getattr(self.settings, "max_delegations", 12)
        self._run.budget.max_parallel_agents = getattr(self.settings, "max_parallel_agents", 4)
        self._run.budget.max_file_writes = int(getattr(self.settings, "max_file_writes", 0) or 0)
        self._run.plan.add(Task(id="root", objective=task, agent=self.name))
        self._run.metadata["cost_usd"] = 0.0
        deadline = time.monotonic() + max(0, int(getattr(self.settings, "run_timeout", 0)))
        try:
            while step < max_steps:
                step += 1
                if deadline and time.monotonic() > deadline:
                    self._log(self.name, "Run timeout exceeded", "ptc_error", self.mode)
                    return self._finish(RunStatus.TIMEOUT, "Error: Run timeout exceeded.")
                self._run.budget.steps = step
                self.trace.step(step)
                if self.interrupt_event.is_set():
                    self.interrupt_event.clear()
                    self._log(self.name, "Stopped by user", "ptc_error", self.mode)
                    return self._finish(RunStatus.STOPPED, "Stopped by user.")
                pending = self.coordinator.drain()
                if pending:
                    for message in pending:
                        role = message.get("role", "user") if isinstance(message, dict) else "user"
                        content = message.get("content", "") if isinstance(message, dict) else message
                        if content:
                            self.history.append({"role": role if role in ("user", "assistant") else "user", "content": content})
                    self.invalidate_cache()
                await self._trim_history()
                if self._cached_system_prompt is None:
                    role_cfg = load_role(self.skill_name) or load_role("system")
                    self._cached_system_prompt = self._build_system_prompt(role_cfg, include_roles=self.skill_name == "system", user_input=task)
                    self._prompt_hash = hashlib.sha1(self._cached_system_prompt.encode("utf-8", "replace")).hexdigest()[:12]
                tokens = count_tokens(self.history, "")[0]
                cost = float(self._run.metadata.get("cost_usd", 0.0) or 0.0)
                self.trace.emit(
                    "step", self.name,
                    f"step {step}/{max_steps} · {tokens:,} ctx · ${cost:.4f}",
                    prompt_hash=self._prompt_hash, prompt_chars=len(self._cached_system_prompt or ""),
                    history_tokens=tokens, cost_usd=round(cost, 6),
                )
                self._status_base = f"step {step}/{max_steps} · {tokens:,} ctx · ${cost:.4f}"
                self._set_status(True)
                response = None
                attempt = 0
                while response is None:
                    attempt += 1
                    try:
                        response = await self._handle_llm_turn(self._cached_system_prompt)
                    except asyncio.TimeoutError:
                        max_retries = max(1, int(getattr(self.settings, "max_retries", 3)))
                        if attempt >= max_retries:
                            return self._finish(RunStatus.FAILED, f"Error: Provider timeout after {max_retries} attempts.")
                        self._log(self.name, f"Timeout, retry {attempt}/{max_retries}", "ptc_error", self.mode)
                        await asyncio.sleep(2 * attempt)
                    except asyncio.CancelledError:
                        self.interrupt_event.clear()
                        return self._finish(RunStatus.STOPPED, "Stopped by user.")
                    except Exception as e:
                        if self.interrupt_event.is_set():
                            return self._finish(RunStatus.STOPPED, "Stopped by user.")
                        err = str(e).lower()
                        if "context" in err and "length" in err:
                            self._log(self.name, "Context overflow, compressing", "ptc_error", self.mode)
                            await self._trim_history()
                            self.invalidate_cache()
                            max_retries = max(1, int(getattr(self.settings, "max_retries", 3)))
                            if attempt >= max_retries:
                                return self._finish(RunStatus.FAILED, f"Error: {e}")
                        elif "429" in str(e) or "rate limit" in err:
                            max_retries = max(1, int(getattr(self.settings, "max_retries", 3)))
                            if attempt >= max_retries:
                                return self._finish(RunStatus.FAILED, f"Error: {e}")
                            self._log(self.name, f"Rate limited, retry {attempt}/{max_retries}", "ptc_error", self.mode)
                            await asyncio.sleep(5 * attempt)
                        elif "empty" in err:
                            max_retries = max(1, int(getattr(self.settings, "max_retries", 3)))
                            if attempt >= max_retries:
                                return self._finish(RunStatus.FAILED, f"Error: {e}")
                            self._log(self.name, f"Provider empty response, retry {attempt}/{max_retries}", "ptc_error", self.mode)
                            await asyncio.sleep(3 * attempt)
                        else:
                            import traceback
                            self._log(self.name, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}", "ptc_error", self.mode)
                            return self._finish(RunStatus.FAILED, f"Error: {e}")
                # One decision per turn: what does this message ask the loop to do?
                specs = self._tool_specs()
                intent = parse_turn(response, set(specs) | TERMINAL_TOOLS, specs)
                if intent.source == "auto-ptc" and not getattr(self.settings, "ptc_auto_detect", True):
                    intent = TurnIntent("answer", answer=response.strip(), prose=response.strip(),
                                        raw=response, source="prose")
                if intent.source == "json" and not getattr(self.settings, "json_tool_fallback", True):
                    intent = TurnIntent("answer", answer=response.strip(), prose=response.strip(),
                                        raw=response, source="prose")

                if self.settings.debug_mode and not self._silent:
                    self._set_status(False)
                    console.print(Panel(Text(response), title="[green]OUTPUT[/green]"))

                if intent.kind == "calls" and intent.prose and not self._silent \
                        and not self.settings.debug_mode and not getattr(self, "_last_streamed", False):
                    from rich.markdown import Markdown
                    self._set_status(False)
                    console.print(Markdown(intent.prose))
                    self._set_status(True)

                shell_blocks = self._looks_like_shell(response) if getattr(self.settings, "legacy_shell_blocks_enabled", False) else []
                for lang, cmd in shell_blocks:
                    shell_result = await self._execute_tool("shell", {"command": cmd})
                    self.history.append({"role": "assistant", "content": f"[SHELL {lang}]\n{cmd}\n[OUTPUT]\n{shell_result}"})

                if intent.kind == "retry":
                    hint = "Your previous response was raw XML/HTML." if intent.source == "markup" else "You returned nothing."
                    self._empty_count = getattr(self, "_empty_count", 0) + 1
                    self._empty_total = getattr(self, "_empty_total", 0) + 1
                    if self._empty_total >= 9:
                        return self._finish(RunStatus.FAILED, "[ERROR] Provider returned empty repeatedly. Halted.")
                    if self._empty_count >= 3:
                        self._empty_count = 0
                        self._log(self.name, "Provider returned no usable turn 3x.", "ptc_error", self.mode)
                        self.history.append({"role": "user", "content": f"[LOOP] {hint} Emit a ```python block, a ::tool line, or a plain final answer now."})
                    self.invalidate_cache()
                    continue
                self._empty_count = 0
                self._empty_total = 0

                if intent.kind == "calls":
                    self.history.append({"role": "assistant", "content": response})
                    if intent.source in ("ptc", "auto-ptc"):
                        if intent.source == "auto-ptc":
                            self._log(self.name, "AUTO-PTC: unfenced code detected, executing", "ptc_call", self.mode)
                        raw_output = await self._run_code(intent.code, response)
                        if raw_output and str(raw_output).startswith("PTC ") and self._record_error_tripwire(str(raw_output)):
                            return self._finish(RunStatus.FAILED, raw_output)
                        if raw_output and (not self.history or self.history[-1].get("content") != raw_output):
                            self.history.append({"role": "assistant", "content": raw_output})
                        stable = str(raw_output)
                    else:
                        outputs = []
                        for call in intent.calls:
                            result = await self._execute_tool(call.name, call.args)
                            outputs.append(f"{call.name} -> {result}")
                        stacked = "\n".join(outputs)
                        if stacked:
                            self.history.append({"role": "assistant", "content": stacked})
                        stable = stacked
                    if self._response_called:
                        done = await self._finish_response()
                        if done is not None:
                            return done
                        continue
                    if "Error" in stable or "Timeout" in stable:
                        stable = re.sub(r'\d{2}:\d{2}:\d{2}|[0-9a-f]{8}-[0-9a-f-]{8,}', '', stable)
                    if stable == self._last_stable_result:
                        self._repeat_count += 1
                        if self._repeat_count >= 3:
                            self._set_status(False)
                            self._log(self.name, f"Loop detected: same result {self._repeat_count}x", "loop", self.mode)
                            self.history.append({"role": "user", "content": f"[LOOP] Same result {self._repeat_count}x. Change strategy: different tool, different args, or answer with what you have."})
                            self._repeat_count = 0
                            self._last_stable_result = None
                            self.invalidate_cache()
                            continue
                    else:
                        self._repeat_count = 0
                        self._last_stable_result = stable
                    self._set_status(False)
                    self.invalidate_cache()
                    continue

                # intent.kind == "answer"
                if intent.calls:
                    self.history.append({"role": "assistant", "content": response})
                    for call in intent.calls:
                        result = await self._execute_tool(call.name, call.args)
                        self.history.append({"role": "assistant", "content": f"{call.name} -> {result}"})
                clean_response = (intent.answer or "").strip()
                if intent.source == "prose":
                    clean_response = strip_ptc(clean_response, set(specs))
                if not clean_response.strip():
                    self._empty_count = getattr(self, "_empty_count", 0) + 1
                    if self._empty_count >= 3:
                        self._empty_count = 0
                        self.history.append({"role": "user", "content": "[LOOP] Emit a ```python block or a final answer now."})
                    self.invalidate_cache()
                    continue
                queued, terminal = await self._verify_or_repair()
                if terminal is not None:
                    return terminal
                if queued:
                    continue
                self.trace.answer(clean_response, streamed=self._streamed_answer(clean_response))
                self.history.append({"role": "assistant", "content": clean_response})
                if not self._is_delegated and self._memory_on and len(clean_response) > 20:
                    task = asyncio.create_task(self.memory.add_memory(f"TASK: {task[:250]} => OUTCOME: {clean_response[:250]}", category="lesson", tier="recent", source="auto"))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                return self._finish(RunStatus.DONE, clean_response)
            return self._finish(RunStatus.MAX_STEPS, "Max steps reached.")
        finally:
            if self._run is not None and self._run.status == RunStatus.RUNNING:
                self._finish(RunStatus.FAILED, "Run failed: no terminal status was set.")
            self._set_status(False)

    def get_history_summary(self, max_messages: int = -1) -> str:
        if not self.history:
            return "No history."
        msgs = self.history if max_messages < 0 else self.history[-max_messages:]
        lines = []
        for m in msgs:
            role = m.get("role", "?")
            content = str(m.get("content", ""))
            preview = content[:800].replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
            if len(content) > 800:
                preview += "..."
            lines.append(f"[{role}] {preview}")
        return "\n".join(lines)
