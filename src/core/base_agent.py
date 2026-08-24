import asyncio
import ast
import hashlib
import json
import logging
import inspect
import re
import subprocess
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from src.config import MODES, GlobalSettings, load_config, ZG_PROTOCOL
from src.core.memory import memory_core
from src.core.provider import AIProvider, get_provider
from src.skills_loader import load_role, SkillEngine
from src.tools import TOOL_REGISTRY
from src.utils import get_system_context, sanitize_for_prompt, generate_random_delimiter, count_tokens, add_global_tokens, add_provider_tokens, ContextCompressor, ModelCatalog, safe_run, GitManager

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
        """Change this agent's operating mode (BUILD, ASK, PLAN, DEBUG)."""
        old = self.mode
        self.mode = mode.upper()
        return f"Mode changed: {old} -> {self.mode}"

    @classmethod
    async def create(cls, role: str, mode: str = "BUILD", settings=None, memory=None, initial_history: Optional[List[Dict]] = None) -> "StemAgent":
        settings = settings or load_config()
        provider = get_provider(getattr(settings, 'provider', 'pollinations'), settings) or get_provider('pollinations', settings)
        return cls(name=role, provider=provider, skill_name=role, settings=settings, mode=mode, memory=memory, initial_history=initial_history)

    def _load_tools(self) -> None:
        role_cfg = load_role(self.skill_name)
        allowed = None
        if role_cfg and getattr(role_cfg, 'tools', None):
            allowed = set(role_cfg.tools)
            if "all" in allowed:
                allowed = set(TOOL_REGISTRY.keys())
        if allowed:
            self.tools = {k: v for k, v in TOOL_REGISTRY.items() if k in allowed}
            for t in ("response", "load_skill", "find_skill", "scan_tools"):
                if t not in self.tools and t in TOOL_REGISTRY:
                    self.tools[t] = TOOL_REGISTRY[t]
        else:
            self.tools = dict(TOOL_REGISTRY)
    
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
                except asyncio.TimeoutError:
                    connect_task.cancel()
                    try:
                        await connect_task
                    except (asyncio.CancelledError, Exception):
                        pass
                except Exception:
                    connect_task.cancel()
                    try:
                        await connect_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for name, server in self._mcp.servers.items():
                    if not server._connected:
                        logger.warning(f"MCP server '{name}' failed to connect")
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

    def _build_system_prompt(self, role_cfg, include_roles: bool = False, user_input: str = "") -> str:
        context = get_system_context()
        if self.memory:
            memories_str = self.memory.inject_context(user_input or "", limit=8, trusted_sources={"user", "tool_result", "delegation"})
        else:
            memories_str = ""

        prompt = f"{role_cfg.prompt if role_cfg else 'You are a ZervGen Agent.'}\n=== OP STATE ===\nROLE: {self.skill_name}\nMODE: {self.mode}\n--- CONTEXT ---\n{context}\n--- MEMORY ---\n{memories_str}\n"

        if self._active_todos:
            prompt += "--- ACTIVE TODOS ---\n" + "\n".join(f"- {todo}" for todo in self._active_todos) + "\n\n"

        try:
            from src.core.memory import PeerCards
            cards = PeerCards().get_relevant(self.skill_name, user_input, limit=2)
            if cards:
                prompt += "--- PEER CARDS ---\n" + "\n\n".join(c.to_prompt_block() for c in cards) + "\n\n"
        except Exception:
            pass

        if getattr(self.settings, "mcp_enabled", True) and self._mcp:
            connected = []
            dead = []
            for name, srv in self._mcp.servers.items():
                if srv.connected:
                    keys = list(srv.tools.keys())
                    shown = ", ".join(keys[:5])
                    connected.append(f"- {name} ({len(keys)}): {shown}{'…' if len(keys) > 5 else ''}")
                else:
                    err = (srv._last_error or "unknown")[:60]
                    dead.append(f"- {name} (DEAD: {err})")
            if connected or dead:
                prompt += "--- MCP SERVERS ---\n"
                if connected:
                    prompt += "ACTIVE:\n" + "\n".join(connected) + "\n"
                if dead:
                    prompt += "DEAD:\n" + "\n".join(dead) + "\n"
                prompt += "Run: mcp_execute(server, tool, args). Discover full schema: mcp_execute(server, 'list').\n\n"
        triggered_skills = []
        if user_input:
            from src.skills_loader import skill_index
            seen = self.memory._triggered_this_session if self.memory else set()
            triggered_skills = [s for s in skill_index.skills.values() if s.match_trigger(user_input) and s.name not in seen]
            if triggered_skills:
                for skill in triggered_skills:
                    prompt += f"--- TRIGGERED SKILL: {skill.name} ---\n{skill.body}\n\n"
                    if self.memory:
                        self.memory._triggered_this_session.add(skill.name)

        try:
            from src.skills_loader import skill_index
            triggered_names = {s.name for s in triggered_skills}
            index_lines = [f"- {s.name}: {s.description[:60]}" for s in skill_index.skills.values() if s.name not in triggered_names]
            if index_lines:
                prompt += "--- AVAILABLE SKILLS ---\nUse load_skill('name') to read full instructions.\n" + "\n".join(index_lines) + "\n\n"
        except Exception: pass
        
        try:
            from src.utils import discover_context_files, redact_fast
            prompt += f"--- REASONING PROTOCOL ---{ZG_PROTOCOL}\n"
            ctx_files = discover_context_files(".")
            if ctx_files:
                prompt += f"--- CONTEXT FILES ---\n{redact_fast(ctx_files)}\n"
                logging.getLogger(__name__).info("Context files loaded (%d chars)" % len(ctx_files))
            else:
                logging.getLogger(__name__).info("discover_context_files empty - AGENTS.md not found or blocked")
        except Exception as e:
            logging.getLogger(__name__).warning(f"discover_context_files failed: {e}")

        current_hash = hashlib.md5(prompt.encode()).hexdigest()
        if current_hash == self._prompt_hash and self._prompt_obj_ref is not None:
            return self._prompt_obj_ref
        self._prompt_hash = current_hash
        self._prompt_obj_ref = prompt
        return prompt

    def _log(self, role: str, content: str, event_type: str = "message", mode: str = "") -> None:
        from src.utils import redact_fast, trace_line
        safe_content = redact_fast(content)
        logger.info(f"[{event_type.upper()}] {role}: {safe_content}")
        trace_line(role, event_type, safe_content)
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
        if self.mode != "BUILD" and (
            tool_name in ("write_file", "append_file", "edit_file", "shell")
            or tool_name.startswith("mcp_")
        ):
            return f"Error: Tool '{tool_name}' requires BUILD mode (current: {self.mode})"
        func = self.tools[tool_name]
        self._log(self.name, f"TOOL: {tool_name}({args})", "tool_call", self.mode)
        try:
            tool_timeout = getattr(self.settings, 'tool_timeout', 60)
            result = await asyncio.wait_for(
                func(**args) if inspect.iscoroutinefunction(func) else asyncio.to_thread(func, **args),
                timeout=tool_timeout
            )
            result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            if tool_name != "delegate_to":
                self._log(self.name, result_str, "tool_result", self.mode)
            return self._format_tool_output(tool_name, result_str)
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
        self._log(self.name, f"TOOL ERROR: {tool_name} -> {error_msg}", "tool_error", self.mode)
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
                if (id(node) not in excluded and id(node) not in awaited
                        and isinstance(node.func, ast.Name) and node.func.id in names):
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

    async def _run_code(self, code: str, full_response: str = "") -> str:
        if not code:
            return "Error: Empty code"
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
        g = {**self._active_tools(), "json": json, "print": _captured_print, "_orchestrator": self}
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
            return "Error: Execution cancelled"
        except asyncio.TimeoutError:
            if is_delegate:
                return "Error: delegated agent timed out. Retry with a smaller sub-task, or do the work yourself with native tools."
            return "Error: Execution timed out"
        except NameError as e:
            err_str = str(e)
            if "mcp_" in err_str:
                missing = re.search(r"mcp_\w+", err_str)
                tool = missing.group(0) if missing else "unknown"
                return f"MCP Error: {tool} unavailable. Retry once; if it persists use native equivalents (fetch_url, read_file, shell)."
            return f"NameError: {err_str}"
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
            return f"Error: {err_msg}"

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
        safe_history = [
            {"role": m["role"] if m["role"] in valid_roles else "assistant", "content": m["content"]}
            for m in self.history
        ]
        use_streaming = not self._silent and not self.settings.debug_mode
        self._last_streamed = use_streaming
        if self.settings.debug_mode and not self._silent:
            self._set_status(False)
            console.print(Panel(Text(full_prompt), title="[purple]INPUT[/purple]"))
        response = None
        try:
            if use_streaming:
                self._set_status(False)
                response = await asyncio.wait_for(
                    self.provider.generate_text(safe_history, full_prompt, on_token=_StreamFilter(thought_cb=self._live_thought)), timeout=provider_timeout)
                self._set_status(True)
            else:
                response = await asyncio.wait_for(self.provider.generate_text(safe_history, full_prompt), timeout=provider_timeout)
            from src.core.provider import _record_success
            _record_success(self.provider.name if hasattr(self.provider, 'name') else (
                self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider
            ))
        except Exception as e:
            from src.core.provider import _record_failure
            p_name = self.provider.name if hasattr(self.provider, 'name') else (
                self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider
            )
            _record_failure(p_name)
            raise
        response = response.strip()
        if response.startswith(delim) and response.endswith(delim):
            response = response[len(delim):-len(delim)].strip()
        in_tok, out_tok = count_tokens(self.history, response)
        add_global_tokens(in_tok + out_tok)
        if self.provider:
            p_name = self.provider.name if hasattr(self.provider, 'name') else (
                self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider
            )
            add_provider_tokens(p_name, in_tok, out_tok)
        return response

    async def run(self, task: str) -> str:
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
        try:
            while step < max_steps:
                step += 1
                if self.interrupt_event.is_set():
                    self.interrupt_event.clear()
                    self.history.append({"role": "user", "content": "[LOOP] Try something different!"})
                    self.invalidate_cache()
                    continue
                if self._injected:
                    for msg in self._injected:
                        self.history.append(msg)
                    self._injected.clear()
                    self.invalidate_cache()
                await self._trim_history()
                if self._cached_system_prompt is None:
                    role_cfg = load_role(self.skill_name) or load_role("system")
                    self._cached_system_prompt = self._build_system_prompt(role_cfg, include_roles=self.skill_name == "system", user_input=self._get_trigger_input())
                response = None
                attempt = 0
                while response is None:
                    attempt += 1
                    try:
                        response = await self._handle_llm_turn(self._cached_system_prompt)
                    except asyncio.TimeoutError:
                        if attempt >= 3:
                            return "Error: Provider timeout after 3 attempts."
                        self._log(self.name, f"Timeout, retry {attempt}/3", "ptc_error", self.mode)
                        await asyncio.sleep(2 * attempt)
                    except Exception as e:
                        err = str(e).lower()
                        if "context" in err and "length" in err:
                            self._log(self.name, "Context overflow, compressing", "ptc_error", self.mode)
                            await self._trim_history()
                            self.invalidate_cache()
                            if attempt >= 3:
                                return f"Error: {e}"
                        elif "429" in str(e) or "rate limit" in err:
                            if attempt >= 4:
                                return f"Error: {e}"
                            self._log(self.name, f"Rate limited, retry {attempt}/4", "ptc_error", self.mode)
                            await asyncio.sleep(5 * attempt)
                        else:
                            self._log(self.name, str(e) or repr(e), "ptc_error", self.mode)
                            return f"Error: {e}"
                codes = self._extract_codes(response)
                if self.settings.debug_mode and not self._silent:
                    self._set_status(False)
                    console.print(Panel(Text(response), title="[green]OUTPUT[/green]"))
                #TODO Make IF codes then combined_code -> execute AND raw_text in response or print AND loop goes further 
                if codes:
                    prose = response.split("```", 1)[0].strip()
                    if prose and not self._silent and not self.settings.debug_mode and not getattr(self, "_last_streamed", False):
                        from rich.markdown import Markdown
                        console.print(Markdown(prose))
                    self._set_status(True)
                    combined_code = "\n\n".join(codes)
                    raw_output = await self._run_code(combined_code, response)
                    self.history.append({"role": "assistant", "content": response})
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
                        self._set_status(False)
                        return "[ERROR] Provider returned empty. Check API key/model."
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
                    self._log(self.name, clean_response, "response", self.mode)
                    if not self._is_delegated and self.memory and len(clean_response) > 20:
                        asyncio.create_task(self.memory.add_memory(f"TASK: {task[:250]} => OUTCOME: {clean_response[:250]}", category="lesson", tier="recent", source="auto"))
                    return clean_response
            return "Max steps reached."
        finally:
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