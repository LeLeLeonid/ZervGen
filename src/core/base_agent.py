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
from typing import Any, Dict, List, Optional
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from src.config import MODES, GlobalSettings, load_config, ZG_PROTOCOL, RISK_ASK_TOOLS
from src.core.memory import memory_core
from src.core.provider import AIProvider, get_provider
from src.skills_loader import load_role, SkillEngine
from src.tools import TOOL_REGISTRY
from src.utils import get_system_context, sanitize_for_prompt, generate_random_delimiter, count_tokens, add_global_tokens, add_provider_tokens, ContextCompressor, ModelCatalog, safe_run, GitManager
from src.core.runtime import Run, ToolCall, ToolResult, Artifact, RunStatus, Task

logger = logging.getLogger(__name__)
console = Console()


class _StreamFilter:
    def __init__(self, thought_cb=None):
        self.buf = ""
        self.in_fence = False
        self.thought_cb = thought_cb

    def _push_thought(self, text: str):
        if not self.thought_cb or not text.strip():
            return
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if lines:
            self.thought_cb(lines[-1][:60])

    def __call__(self, token: str) -> None:
        self.buf += token
        while "```" in self.buf:
            pre, _, post = self.buf.partition("```")
            if not self.in_fence:
                sys.stdout.write(pre)
                sys.stdout.flush()
                self._push_thought(pre)
            self.in_fence = not self.in_fence
            self.buf = post

        if not self.in_fence:
            sys.stdout.write(self.buf)
            sys.stdout.flush()
            self._push_thought(self.buf)
            self.buf = ""


class StemAgent:
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
    ):
        self.name = name
        self.provider = provider
        self.skill_name = skill_name
        self.settings = settings or load_config()
        self.mode = mode.upper() if mode else "BUILD"
        self.memory = memory
        self.history: List[Dict[str, str]] = list(initial_history) if initial_history else []
        self.tools: Dict[str, Any] = {}
        self.interrupt_event = asyncio.Event()
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
        self._injected: List[Dict[str, str]] = []
        self._response_called = False
        self._response_value = ""
        self._run: Optional[Run] = None
        self._trace_parent_id = ""
        self._tripwire_errors: Dict[str, int] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._load_tools()
        self.tools["set_mode"] = self._tool_set_mode

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
            allowed.update({"response", "load_skill", "find_skill", "search_memory", "search_tgs", "list_skills", "list_mcp_servers", "set_external_skills"})
        self.tools = {k: v for k, v in TOOL_REGISTRY.items() if k in allowed}

    def _active_tools(self) -> dict:
        write_set = {"write_file", "append_file", "edit_file", "shell"}
        if self.mode == "BUILD":
            return dict(self.tools)
        return {k: v for k, v in self.tools.items() if k not in write_set and not k.startswith("mcp_")}

    async def _init_mcp(self) -> None:
        if self._mcp_initialized or not getattr(self.settings, 'mcp_enabled', True):
            return
        try:
            from src.core.mcp_manager import MCPManager
            self._mcp = MCPManager(self.settings)
            if not self._mcp.tools_map and not getattr(self._mcp, "_connect_attempted", False):
                self._mcp._connect_attempted = True
                connect_task = asyncio.create_task(self._mcp.connect_all())
                try:
                    await asyncio.wait_for(connect_task, timeout=120.0)
                except Exception:
                    connect_task.cancel()
                    try:
                        await connect_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for name, server in self._mcp.servers.items():
                    if not server._connected:
                        logger.warning(f"MCP server '{name}' failed to connect")
            if getattr(self.settings, "mcp_expose_direct_tools", False):
                for tool_name in self._mcp.tools_map:
                    mcp_name = f"mcp_{tool_name}"
                    self.tools[mcp_name] = self._make_mcp_wrapper(tool_name)
            if self._mcp.tools_map:
                logger.info(f"MCP ready: {len(self._mcp.tools_map)} tools from {len(self._mcp.servers)} servers")
            self._mcp_initialized = True
        except Exception as e:
            logger.warning(f"MCP init failed (will retry): {e}")

    def _make_mcp_wrapper(self, tool_name: str):
        mcp = self._mcp
        async def _mcp_call(**kwargs):
            args = kwargs if kwargs else {}
            if "arguments" in args and isinstance(args["arguments"], dict) and len(args) == 1:
                args = args["arguments"]
            return await mcp.execute_tool(tool_name, args)
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
    
    async def _permission_check(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        p_mode = getattr(self.settings, "permission_mode", "ASK")
        if p_mode == "AUTO":
            return None
        risky = tool_name in RISK_ASK_TOOLS or (
            tool_name.startswith("mcp_") and
            any(k in tool_name for k in ("write", "edit", "delete", "move", "send", "publish", "create"))
        )
        if not risky:
            return None
        if p_mode == "DENY":
            return f"Error: {tool_name} denied by permission policy."
        ok = await self.request_permission(
            f"Allow {tool_name}?", details=json.dumps(args, ensure_ascii=False, default=str)[:300], default=False)
        return None if ok else f"Error: user denied {tool_name}."
    
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
        logger.info("[RUN_EVENT] %s", json.dumps(payload, ensure_ascii=False, default=str))
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
        if not self.settings.history_trim_enabled:
            return
        limit = None
        try:
            provider_name = getattr(self.settings, 'provider', None)
            if provider_name:
                prov_cfg = getattr(self.settings, provider_name, None)
                if prov_cfg and hasattr(prov_cfg, 'model'):
                    model_name = prov_cfg.model
                    limit = ModelCatalog.get_context(provider_name, model_name)
        except Exception as e:
            logger.debug(f"ModelCatalog context fetch failed: {e}")
        if limit is None:
            limit = 128000
        threshold = int(limit * 0.85)
        current_tokens = count_tokens(self.history, "")[0]
        if current_tokens < threshold:
            return
        if self._compressor is None:
            self._compressor = ContextCompressor(self.provider)
        try:
            self.history = await self._compressor.compress(self.history)
        except Exception as e:
            logger.error(f"Compressor failed: {e}. Hard-trimming history.")
        if count_tokens(self.history, "")[0] > threshold:
            head = [m for m in self.history if m.get("role") == "system"]
            self.history = head + self.history[-10:]
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

    def _prompt_memory(self, user_input: str) -> str:
        if not self.memory:
            return ""
        try:
            limit = max(1, int(getattr(self.settings, "prompt_memory_limit", 4)))
            value = self.memory.inject_context(user_input, limit=limit, trusted_sources={"user", "tool_result", "delegation"})[:10]
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
        return "--- MCP ---\n" + "\n".join(active) if active else ""

    def _prompt_skills(self, user_input: str) -> str:
        try:
            from src.skills_loader import skill_index
            show_internal = bool(getattr(self.settings, "prompt_show_internal_skills"))
            show_external = bool(getattr(self.settings, "prompt_show_external_skills"))
            auto_internal = bool(getattr(self.settings, "prompt_auto_trigger_skills"))
            auto_external = bool(getattr(self.settings, "prompt_auto_trigger_external_skills"))
            visible = skill_index.visible()
            sections = []
            if auto_internal or auto_external:
                seen = self.memory._triggered_this_session if self.memory else set()
                for skill in visible:
                    external = skill_index.is_external(skill.name)
                    if external and not auto_external:
                        continue
                    if not external and not auto_internal:
                        continue
                    if not skill.match_trigger(user_input) or skill.name in seen:
                        continue
                    body = skill_index.get_body(skill.name)
                    if not body.startswith("Error:"):
                        sections.append(f"--- TRIGGERED SKILL: {skill.name} ---\n{body}")
                    if self.memory:
                        self.memory._triggered_this_session.add(skill.name)
            names = []
            for skill in visible:
                external = skill_index.is_external(skill.name)
                if external and not show_external:
                    continue
                if not external and not show_internal:
                    continue
                names.append(f"- {skill.name}: {skill.description[:100]}")
            if names:
                sections.append("--- SKILL INDEX ---\nUse load_skill(name) only when needed.\n" + "\n".join(names))
            return "\n\n".join(sections)
        except Exception as e:
            logger.debug("Prompt skills failed: %s", e)
            return ""

    def _prompt_tools(self) -> str:
        active = self._active_tools()
        if not active:
            return ""
        from src.tools import get_tools_schema
        return "--- AVAILABLE TOOLS ---\n" + get_tools_schema(active, compact=True)

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
            self._prompt_header(role_cfg),
            self._prompt_run(),
            f"--- CONTEXT ---\n{get_system_context()}",
            self._prompt_memory(user_input),
            self._prompt_peer_cards(user_input),
            self._prompt_mcp(),
            self._prompt_skills(user_input),
            self._prompt_tools(),
            f"--- EXECUTION PROTOCOL ---\n{ZG_PROTOCOL.strip()}",
            self._prompt_project_rules(),
        ]
        prompt = "\n\n".join(section for section in sections if section)
        limit = max(4000, int(getattr(self.settings, "prompt_max_chars", 14000)))
        if len(prompt) > limit:
            prompt = prompt[:limit]
        if prompt == self._prompt_obj_ref:
            return self._prompt_obj_ref
        self._prompt_obj_ref = prompt
        return prompt

    def _log(self, role: str, content: str, event_type: str = "message", mode: str = "") -> None:
        from src.utils import redact_fast, trace_line
        safe_content = redact_fast(content)
        logger.info(f"[{event_type.upper()}] {role}: {safe_content}")
        if self._run:
            self._run.emit(event_type, role, input=safe_content)
        if self.memory:
            self.memory.log_event_sync(role, safe_content, event_type, mode or self.mode)
        if event_type in ("input", "response", "agent_start", "delegation_result", "loop"):
            if event_type == "input":
                safe_role = "user"
            elif role in ("system", "user", "assistant"):
                safe_role = role
            else:
                safe_role = "assistant"
            self.history.append({"role": safe_role, "content": self._strip_ptc(safe_content)})

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name not in self.tools:
            return f"Error: Tool '{tool_name}' not found"
        if self.mode != "BUILD" and (tool_name in ("write_file", "append_file", "edit_file", "shell") or tool_name.startswith("mcp_")):
            return f"Error: Tool '{tool_name}' requires BUILD mode (current: {self.mode})"
        gate_err = await self._permission_check(tool_name, args)
        if gate_err:
            return gate_err
        if self._run and not self._run.budget.tool():
            return "Error: Tool-call budget exhausted"
        func = self.tools[tool_name]
        call_id = uuid.uuid4().hex[:12]
        call = ToolCall(call_id, tool_name, dict(args), self.name)
        if self._run:
            self._run.tool_calls.append(call)
            self._run.emit("tool_call", self.name, name=tool_name, status="running", input=args)
        self._log(self.name, f"TOOL: {tool_name}({args})", "tool_call", self.mode)
        started = time.monotonic()
        try:
            tool_timeout = getattr(self.settings, 'tool_timeout', 60)
            result = await asyncio.wait_for(func(**args) if inspect.iscoroutinefunction(func) else asyncio.to_thread(func, **args), timeout=tool_timeout)
            result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            duration = time.monotonic() - started
            if self._run:
                tool_result = ToolResult(call_id, True, result_str, duration=duration, ended_at=time.time())
                self._run.tool_results.append(tool_result)
                self._run.emit("tool_result", self.name, name=tool_name, status="ok", output=self._format_tool_output(tool_name, result_str), duration=duration)
                if tool_name in {"write_file", "append_file", "edit_file"}:
                    path = str(args.get("path", ""))
                    if path:
                        self._run.artifacts.append(Artifact(path=path))
            if tool_name != "delegate_to":
                ref = str(args.get("path") or args.get("url") or args.get("command") or args.get("location") or "")
                line = f"{tool_name} -> {ref} [{len(result_str)} chars]" if ref else f"{tool_name} [{len(result_str)} chars]"
                if tool_name in ("read_file", "write_file", "append_file", "edit_file", "fetch_url"):
                    blob = result_str if tool_name in ("read_file", "fetch_url") else str(args.get("content", ""))
                    if blob and len(blob) <= 500_000:
                        d = Path("tmp/trace")
                        d.mkdir(parents=True, exist_ok=True)
                        slug = re.sub(r"[^a-zA-Z0-9]+", "_", ref)[:40] or "blob"
                        p = d / f"{tool_name}_{slug}_{int(time.time())}.txt"
                        p.write_text(blob, encoding="utf-8", errors="replace")
                        line += f" [copy: {p}]"
                logger.info(f"[TOOL_RESULT] {self.name}: {line}")
            return result_str
        except asyncio.TimeoutError:
            error_msg = f"Error: Tool '{tool_name}' timed out"
        except TypeError as e:
            try:
                sig = str(inspect.signature(func)).replace(" -> str", "").replace("**kwargs", "")
                error_msg = f"TypeError: {e}\nExpected signature: {tool_name}{sig}"
            except Exception:
                error_msg = f"TypeError: {e}"
        except Exception as e:
            error_msg = f"Error: Tool '{tool_name}' failed: {e}"
        duration = time.monotonic() - started
        if self._run:
            self._run.tool_results.append(ToolResult(call_id, False, error=error_msg, duration=duration, ended_at=time.time()))
            self._run.emit("tool_result", self.name, name=tool_name, status="error", output=error_msg, duration=duration)
        self._log(self.name, f"TOOL ERROR: {tool_name} -> {error_msg}", "tool_error", self.mode)
        if self._record_error_tripwire(f"{tool_name}: {error_msg}"):
            return f"Error: TRIPWIRE repeated failure for {tool_name}. Escalate or change strategy."
        return error_msg

    def _extract_codes(self, text: str) -> List[str]:
        codes = []
        for match in re.finditer(r'```python\s*\n(.*?)\n\s*```', text, re.DOTALL):
            code = match.group(1).strip()
            if not code:
                continue
            non_comment = [l for l in code.split('\n') if l.strip() and not l.strip().startswith('#')]
            if not non_comment:
                continue
            xml_lines = sum(1 for l in non_comment if l.strip().startswith('<') and '>' in l)
            if xml_lines > len(non_comment) * 0.5:
                continue
            codes.append(code)
        return codes

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

    def _looks_like_code(self, text: str) -> bool:
        try:
            compile(text, "<ptc_probe>", "exec")
        except (SyntaxError, ValueError):
            return False
        return bool(re.search(r"\bawait\s+\w+\s*\(|^\s*\w+\s*=[^=]|\bprint\s*\(", text, re.MULTILINE))

    def _looks_like_xml(self, text: str) -> bool:
        for line in text.split('\n'):
            stripped = line.strip()
            if stripped.startswith('<') and '>' in stripped and not stripped.startswith('```'):
                return True
        return False

    def _looks_like_shell(self, text: str) -> List[tuple[str, str]]:
        blocks = []
        for match in re.finditer(r'```(bash|cmd|shell|powershell|sh)\s*\n(.*?)\n\s*```', text, re.DOTALL | re.IGNORECASE):
            lang = match.group(1).lower()
            code = match.group(2).strip()
            if code:
                blocks.append((lang, code))
        return blocks

    def _validate_ptc(self, code: str) -> Optional[str]:
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return f"Syntax Error: {e}"
        blocked_names = {"open", "eval", "exec", "compile", "__import__", "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr", "breakpoint", "input", "help", "object"}
        blocked_attrs = {"__class__", "__dict__", "__globals__", "__code__", "__closure__", "__subclasses__", "__mro__", "mro", "__getattribute__", "__getattr__", "_agent", "_run", "_mcp", "provider", "settings", "memory", "tools"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Yield, ast.YieldFrom, ast.With, ast.AsyncWith, ast.Global, ast.Nonlocal)):
                return "PTC blocked: unsupported syntax"
            if isinstance(node, ast.Name) and node.id in blocked_names:
                return f"PTC blocked: {node.id}"
            if isinstance(node, ast.Attribute):
                if node.attr in blocked_attrs or node.attr.startswith("_"):
                    return f"PTC blocked: {node.attr}"
                if isinstance(node.value, ast.Name) and node.value.id in {"os", "sys", "subprocess", "socket", "pathlib", "shutil", "ctypes", "builtins", "importlib"}:
                    return f"PTC blocked: {node.value.id}"
        return None

    def _extract_json_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        if not getattr(self.settings, "json_tool_fallback", True):
            return []
        candidates = []
        blocks = re.findall(r"```json\s*\n(.*?)\n\s*```", text, re.DOTALL | re.IGNORECASE)
        candidates.extend(blocks)
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            candidates.append(stripped)
        for raw in candidates:
            try:
                value = json.loads(raw)
            except Exception:
                continue
            items = value.get("tool_calls") if isinstance(value, dict) else value
            if items is None and isinstance(value, dict):
                items = [value]
            if not isinstance(items, list):
                items = [items]
            out = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get("tool") or item.get("name") or item.get("function")
                args = item.get("arguments", item.get("args", {}))
                if isinstance(name, dict):
                    args = name.get("arguments", args)
                    name = name.get("name")
                if not isinstance(name, str) or name not in self._active_tools():
                    continue
                if not isinstance(args, dict):
                    continue
                out.append({"name": name, "arguments": args})
            if out:
                return out
        return []

    def _log_ptc_error(self, message: str, full_response: str = "") -> None:
        captured = full_response.strip()
        if captured:
            message = f"{message}\nMODEL OUTPUT:\n{captured}"
        self._log(self.name, message, "ptc_error", self.mode)

    async def _run_code(self, code: str, full_response: str = "") -> str:
        if not code:
            return "Error: Empty code"
        validation_error = self._validate_ptc(code) if getattr(self.settings, "ptc_strict", True) else No
        if validation_error:
            self._log_ptc_error(validation_error, full_response)
            return validation_error
        if code.count('"""') % 2 != 0:
            code += '\n"""'
        if code.count("'''") % 2 != 0:
            code += "\n'''"
        is_delegate = bool(re.search(r'(?:^|\n)\s*await\s+delegate_to\s*\(', code))
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
        self._log(self.name, f"EXEC: {code}", "ptc_call", self.mode)
        async_names = {n for n, f in self._active_tools().items() if inspect.iscoroutinefunction(f)}
        fixed_code = self._auto_await_tools(code, async_names)
        fixed_code = self._auto_return(fixed_code)
        fixed_code = re.sub(r'\basyncio\.run\s*\(\s*([^)]+)\s*\)', r'await \1', fixed_code)
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
        class _ToolRuntimeView:
            def __init__(self, owner):
                object.__setattr__(self, "owner", owner)
            def __getattr__(self, k):
                return getattr(self.owner, k)
            def __setattr__(self, k, v):
                setattr(self.owner, k, v)
            async def request_permission(self, question, details="", default=False):
                return await self.owner.request_permission(question, details, default)
        safe_asyncio = SimpleNamespace(
            gather=asyncio.gather,
            wait_for=asyncio.wait_for,
            create_task=asyncio.create_task,
            ensure_future=asyncio.ensure_future,
            shield=asyncio.shield,
            as_completed=asyncio.as_completed,
        )
        g = {**self._active_tools(), "json": json, "asyncio": safe_asyncio, "print": _captured_print, "_orchestrator": _ToolRuntimeView(self), "__builtins__": safe_builtins}
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
            if result and not is_delegate:
                self._log(self.name, str(result), "ptc_result", self.mode)
            return str(result) if result else ""
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
            self._log(self.name, f"ERROR: {err_msg}", "ptc_error", self.mode)
            err = f"Error: {err_msg}"
            self._log_ptc_error(err, full_response)
            self._record_error_tripwire(f"ptc:{err}")
            return err

    def _get_max_steps(self) -> int:
        return self._max_steps_override or self.settings.max_steps

    def _live_thought(self, text: str):
        self._set_status(True, f"▶ {text}")

    def _set_status(self, enabled: bool, label: str = "") -> None:
        if self._silent:
            return
        cb = getattr(self, "status_cb", None)
        if cb:
            cb(label if (enabled and label) else ("thinking..." if enabled else ""))
            return
        if enabled:
            if self._status is None:
                self._status = console.status("", spinner="dots")
                self._status.__enter__()
        else:
            if self._status is not None:
                self._status.__exit__(None, None, None)
                self._status = None

    def inject_user_message(self, text: str) -> None:
        self._injected.append({"role": "user", "content": text})

    async def _handle_llm_turn(self, system_prompt: str) -> str:
        mode_def = MODES.get(self.mode, MODES["BUILD"])
        delim = generate_random_delimiter(16)
        full_prompt = f"{system_prompt}\n\n{delim}\nFOCUS: {mode_def['prompt']}\n{delim}"
        provider_timeout = getattr(self.settings, 'provider_timeout', 120)
        valid_roles = {"system", "user", "assistant"}
        safe_history = [{"role": m["role"] if m["role"] in valid_roles else "assistant", "content": m["content"]} for m in self.history]
        use_streaming = not self._silent and not self.settings.debug_mode
        self._last_streamed = use_streaming
        if self.settings.debug_mode and not self._silent:
            self._set_status(False)
            console.print(Panel(Text(full_prompt), title="[purple]INPUT[/purple]"))
        response = None
        try:
            if use_streaming:
                self._set_status(False)
                response = await asyncio.wait_for(self.provider.generate_text(safe_history, full_prompt, on_token=_StreamFilter(thought_cb=self._live_thought)), timeout=provider_timeout)
                self._set_status(True)
            else:
                response = await asyncio.wait_for(self.provider.generate_text(safe_history, full_prompt), timeout=provider_timeout)
            from src.core.provider import _record_success
            _record_success(self.provider.name if hasattr(self.provider, 'name') else (self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider))
        except Exception as e:
            from src.core.provider import _record_failure
            p_name = self.provider.name if hasattr(self.provider, 'name') else (self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider)
            _record_failure(p_name)
            raise
        response = response.strip()
        self._emit_run("model_output", self.name, "llm", "received", output=response[:2000])
        if response.startswith(delim) and response.endswith(delim):
            response = response[len(delim):-len(delim)].strip()
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
        commands = list(getattr(self.settings, "verification_commands", []) or [])
        if self.mode != "BUILD" or not commands or "shell" not in self._active_tools():
            return ""
        failures = []
        for command in commands:
            result = await self._execute_tool("shell", {"command": command})
            if str(result).startswith("Error"):
                failures.append(f"{command}: {result}")
        return "\n".join(failures)

    async def run(self, task: str) -> str:
        self.interrupt_event.clear()
        self._tripwire_errors = {}
        if not task or not isinstance(task, str):
            return "Error: Invalid task"
        task = task.strip()
        if not task:
            return "Error: Empty task"
        if self.provider is None:
            return "Error: No provider configured"
        await self._init_mcp()
        sanitized = sanitize_for_prompt(task)
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
        self._set_status(True)
        self._run = Run.create(task, self.name, max_steps=max_steps, parent_run_id=getattr(getattr(self, "_parent_run", None), "id", None))
        self._run.status = RunStatus.RUNNING
        self._run.budget.max_tool_calls = getattr(self.settings, "max_tool_calls", 200)
        self._run.budget.max_delegations = getattr(self.settings, "max_delegations", 12)
        self._run.budget.max_parallel_agents = getattr(self.settings, "max_parallel_agents", 4)
        self._run.plan.add(Task(id="root", objective=task, agent=self.name))
        self._run.metadata["cost_usd"] = 0.0
        deadline = time.monotonic() + max(0, int(getattr(self.settings, "run_timeout", 0)))
        try:
            while step < max_steps:
                step += 1
                if deadline and time.monotonic() > deadline:
                    self._run.status = RunStatus.FAILED
                    self._log(self.name, "Run timeout exceeded", "ptc_error", self.mode)
                    return "Error: Run timeout exceeded."
                self._run.budget.steps = step
                if self.interrupt_event.is_set():
                    self.interrupt_event.clear()
                    self._run.status = RunStatus.STOPPED
                    self._run.ended_at = time.time()
                    self._log(self.name, "Stopped by user", "ptc_error", self.mode)
                    return "Stopped by user."
                if self._injected:
                    for msg in self._injected:
                        self.history.append(msg)
                    self._injected.clear()
                    self.invalidate_cache()
                await self._trim_history()
                if self._cached_system_prompt is None:
                    role_cfg = load_role(self.skill_name) or load_role("system")
                    self._cached_system_prompt = self._build_system_prompt(role_cfg, include_roles=self.skill_name == "system", user_input=task)
                response = None
                attempt = 0
                while response is None:
                    attempt += 1
                    try:
                        response = await self._handle_llm_turn(self._cached_system_prompt)
                    except asyncio.TimeoutError:
                        max_retries = max(1, int(getattr(self.settings, "max_retries", 3)))
                        if attempt >= max_retries:
                            return f"Error: Provider timeout after {max_retries} attempts."
                        self._log(self.name, f"Timeout, retry {attempt}/{max_retries}", "ptc_error", self.mode)
                        await asyncio.sleep(2 * attempt)
                    except Exception as e:
                        err = str(e).lower()
                        if "context" in err and "length" in err:
                            self._log(self.name, "Context overflow, compressing", "ptc_error", self.mode)
                            await self._trim_history()
                            self.invalidate_cache()
                            max_retries = max(1, int(getattr(self.settings, "max_retries", 3)))
                            if attempt >= max_retries:
                                return f"Error: {e}"
                        elif "429" in str(e) or "rate limit" in err:
                            max_retries = max(1, int(getattr(self.settings, "max_retries", 3)))
                            if attempt >= max_retries:
                                return f"Error: {e}"
                            self._log(self.name, f"Rate limited, retry {attempt}/{max_retries}", "ptc_error", self.mode)
                            await asyncio.sleep(5 * attempt)
                        elif "empty" in err:
                            max_retries = max(1, int(getattr(self.settings, "max_retries", 3)))
                            if attempt >= max_retries:
                                return f"Error: {e}"
                            self._log(self.name, f"Provider empty response, retry {attempt}/{max_retries}", "ptc_error", self.mode)
                            await asyncio.sleep(3 * attempt)
                        else:
                            import traceback
                            self._log(self.name, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}", "ptc_error", self.mode)
                            return f"Error: {e}"
                codes = self._extract_codes(response)
                shell_blocks = self._looks_like_shell(response) if getattr(self.settings, "legacy_shell_blocks_enabled", False) else []
                if not codes:
                    json_calls = self._extract_json_tool_calls(response)
                    if json_calls:
                        outputs = []
                        for call in json_calls:
                            result = await self._execute_tool(call["name"], call["arguments"])
                            outputs.append(f"{call['name']} -> {result}")
                            if self._response_called:
                                self._response_called = False
                                self._set_status(False)
                                return self._response_value
                        self._log(self.name, "\n".join(outputs), "ptc_result", self.mode)
                        self.history.append({"role": "assistant", "content": response})
                        self.history.append({"role": "assistant", "content": "\n".join(outputs)})
                        self.invalidate_cache()
                        continue
                for lang, cmd in shell_blocks:
                    self._log(self.name, f"SHELL ({lang}): {cmd}", "ptc_call", self.mode)
                    shell_result = await self._execute_tool("shell", {"command": cmd})
                    self.history.append({"role": "assistant", "content": f"[SHELL {lang}]\n{cmd}\n[OUTPUT]\n{shell_result}"})
                if self.settings.debug_mode and not self._silent:
                    self._set_status(False)
                    console.print(Panel(Text(response), title="[green]OUTPUT[/green]"))
                if codes:
                    prose = response.split("```", 1)[0].strip()
                    if prose and not self._silent and not self.settings.debug_mode and not getattr(self, "_last_streamed", False):
                        from rich.markdown import Markdown
                        console.print(Markdown(prose))
                    self._set_status(True)
                    combined_code = "\n\n".join(codes)
                    raw_output = await self._run_code(combined_code, response)
                    self.history.append({"role": "assistant", "content": response})
                    if raw_output and str(raw_output).startswith("PTC ") and self._record_error_tripwire(str(raw_output)):
                        self._run.status = RunStatus.FAILED
                        self._run.ended_at = time.time()
                        self._emit_run("run_end", self.name, status="failed", output=raw_output)
                        return raw_output
                    if raw_output and (not self.history or self.history[-1].get("content") != raw_output):
                        self.history.append({"role": "assistant", "content": raw_output})
                    if self._response_called:
                        self._response_called = False
                        self._repeat_count = 0
                        self._last_stable_result = None
                        self._log(self.name, self._response_value or "(empty)", "response", self.mode)
                        self._set_status(False)
                        return self._response_value
                    stable = str(raw_output)
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
                    continue
                else:
                    if not response or not response.strip():
                        self._empty_count = getattr(self, "_empty_count", 0) + 1
                        self._empty_total = getattr(self, "_empty_total", 0) + 1
                        if self._empty_total >= 9:
                            return "[ERROR] Provider returned empty repeatedly. Halted."
                        if self._empty_count >= 3:
                            self._empty_count = 0
                            self._log(self.name, "Provider returned empty 3x.", "ptc_error", self.mode)
                            self.history.append({"role": "user", "content": "[LOOP] Emit a ```python block or a final answer now."})
                        self.invalidate_cache()
                        continue
                    self._empty_count = 0
                    self._empty_total = 0
                    clean_response = self._strip_ptc(response)
                    if self._looks_like_code(clean_response):
                        self._log(self.name, "AUTO-PTC: unfenced code detected, executing", "ptc_call", self.mode)
                        raw_output = await self._run_code(clean_response, response)
                        self.history.append({"role": "assistant", "content": response})
                        if raw_output and (not self.history or self.history[-1].get("content") != raw_output):
                            self.history.append({"role": "assistant", "content": raw_output})
                        if self._response_called:
                            self._response_called = False
                            self._set_status(False)
                            return self._response_value
                        self.invalidate_cache()
                        continue
                    if self._looks_like_xml(response):
                        self._log(self.name, "Model output appeared to be raw XML/HTML, re-prompting with PTC format reminder", "ptc_error", self.mode)
                        self.history.append({"role": "user", "content": "Your previous response was not in PTC format. Use ```python ...``` code blocks for tool calls. Please try again."})
                        self.invalidate_cache()
                        continue
                    sensor_error = await self._run_verification_sensors()
                    if sensor_error:
                        self._log(self.name, sensor_error, "state", self.mode)
                        self.history.append({"role": "user", "content": f"Verification failed:\n{sensor_error}\nFix the failure and verify again."})
                        self.invalidate_cache()
                        continue
                    self._log(self.name, clean_response, "response", self.mode)
                    if not self._is_delegated and self.memory and len(clean_response) > 20:
                        task = asyncio.create_task(self.memory.add_memory(f"TASK: {task[:250]} => OUTCOME: {clean_response[:250]}", category="lesson", tier="recent", source="auto"))
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)
                    return clean_response
            self._run.status = RunStatus.FAILED
            return "Max steps reached."
        finally:
            if self._run and self._run.status == RunStatus.RUNNING:
                self._run.status = RunStatus.DONE
            if self._run:
                self._run.ended_at = time.time()
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
