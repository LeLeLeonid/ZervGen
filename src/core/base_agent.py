import asyncio
import ast
import json
import logging
import inspect
import re
from typing import Any, Dict, List, Optional
from rich.console import Console
from rich.panel import Panel
from src.config import MODES, GlobalSettings, load_config
from src.core.memory import memory_core
from src.core.provider import AIProvider, get_provider
from src.skills_loader import load_role
from src.tools import TOOL_REGISTRY, get_tools_schema
from src.utils import get_system_context, sanitize_for_prompt, generate_random_delimiter, count_tokens, add_global_tokens, add_provider_tokens, ContextCompressor

logger = logging.getLogger(__name__)
console = Console()


class BaseAgent:
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
        self._is_delegated = False
        self._active_todos: List[str] = []
        self._user_active = False
        self._last_result = None
        self._repeat_count = 0
        self._cached_system_prompt: Optional[str] = None
        self._compressor = None
        self._load_tools()

    @classmethod
    async def create(cls, role: str, mode: str = "BUILD", settings=None, memory=None, initial_history: Optional[List[Dict]] = None) -> "BaseAgent":
        settings = settings or load_config()
        provider = get_provider(getattr(settings, 'provider', 'pollinations'), settings) or get_provider('pollinations', settings)
        return cls(name=role, provider=provider, skill_name=role, settings=settings, mode=mode, memory=memory, initial_history=initial_history)

    def _load_tools(self) -> None:
        role_cfg = load_role(self.skill_name)
        allowed = None
        if role_cfg and getattr(role_cfg, 'tools', None):
            allowed = set(role_cfg.tools)
        if allowed:
            self.tools = {k: v for k, v in TOOL_REGISTRY.items() if k in allowed}
            if "response" not in self.tools and "response" in TOOL_REGISTRY:
                self.tools["response"] = TOOL_REGISTRY["response"]
        else:
            self.tools = dict(TOOL_REGISTRY)

    async def _init_mcp(self) -> None:
        if self._mcp_initialized or not getattr(self.settings, 'mcp_enabled', True):
            return
        try:
            from src.core.mcp_manager import MCPManager
            self._mcp = MCPManager(self.settings)
            if not self._mcp.servers:
                connect_task = asyncio.create_task(self._mcp.connect_all())
                try:
                    await asyncio.wait_for(connect_task, timeout=60.0)
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
                MCPManager = type(self._mcp)
                MCPManager._instance = None
            except Exception:
                pass
        self._mcp = None
        self._mcp_initialized = False

    def request_interrupt(self) -> None:
        self.interrupt_event.set()

    def invalidate_cache(self):
        self._cached_system_prompt = None

    def _get_trigger_input(self) -> str:
        return self.history[-1]["content"] if self.history else ""

    async def _trim_history(self) -> None:
        if not self.settings.history_trim_enabled or len(self.history) < 20:
            return
        if self._compressor is None:
            self._compressor = ContextCompressor(self.provider)
        self.history = await self._compressor.compress(self.history)
        self._cached_system_prompt = None

    def _build_system_prompt(self, role_cfg, include_roles: bool = False, user_input: str = "") -> str:
        context = get_system_context()
        if self.memory:
            now_items = self.memory.get_now().get('items', [])
            long_term_items = self.memory.get_long_term().get('items', [])
            memories_str = f"NOW: {json.dumps(now_items[-5:], ensure_ascii=False)}\nLONG-TERM: {json.dumps(long_term_items[-10:], ensure_ascii=False)}"
        else:
            memories_str = ""

        prompt = f"{role_cfg.prompt if role_cfg else 'You are a ZervGen Agent.'}\n\n=== OP STATE ===\nROLE: {self.skill_name}\nMODE: {self.mode}\n--- CONTEXT ---\n{context}\n--- MEMORY ---\n{memories_str}\n"

        if self._mcp and self._mcp_initialized:
            mcp_schema = self._mcp.get_tools_schema(registered_tools=self.tools)
            if mcp_schema:
                prompt += f"--- MCP TOOLS ---\n{mcp_schema}\nCall via:\n```python\nresult = await mcp_toolname(arg=\"value\")\nreturn await response(text=result)\n```\n\n"

        if self._active_todos:
            prompt += "--- ACTIVE TODOS ---\n" + "\n".join(f"- {todo}" for todo in self._active_todos) + "\n\n"

        try:
            from src.core.memory import PeerCards
            cards = PeerCards().get_relevant(self.skill_name, user_input, limit=2)
            if cards:
                prompt += "--- PEER CARDS ---\n" + "\n\n".join(c.to_prompt_block() for c in cards) + "\n\n"
        except Exception:
            pass

        if user_input:
            from src.skills_loader import skill_index
            triggered = [s.context for s in skill_index.skills.values() if s.match_trigger(user_input)]
            if triggered:
                prompt += "--- TRIGGERED SKILLS ---\n" + "\n".join(triggered) + "\n\n"

        if include_roles:
            from src.skills_loader import get_roles_overview
            prompt += f"--- ROLES ---\n{get_roles_overview()}\n"

        return prompt

    def _log(self, role: str, content: str, event_type: str = "message", mode: str = "") -> None:
        if self.memory:
            self.memory.log_event_sync(role, content, event_type, mode or self.mode)
        if event_type in ("tool_call", "tool_result", "agent_spawn", "delegation_start"):
            console.print(f"[dim]{role}: {content[:100]}[/dim]")
        if event_type in ("input", "response", "agent_start", "delegation_result", "loop"):
            valid_roles = ("system", "user", "assistant")
            entry_role = "user" if event_type == "input" else (role if role in valid_roles else "assistant")
            self.history.append({"role": entry_role, "content": content})
        try:
            from src.core.memory import session_db
            entry_role = "user" if event_type in ("task","input") else "assistant"
            session_db.save_message(self.memory._session_id if self.memory else "default", entry_role, content, event_type)
        except Exception:
            pass

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name not in self.tools:
            return f"Error: Tool '{tool_name}' not found"
        func = self.tools[tool_name]
        self._log(self.name, f"TOOL: {tool_name}({args})", "tool_call", self.mode)
        try:
            tool_timeout = getattr(self.settings, 'tool_timeout', 60)
            result = await asyncio.wait_for(
                func(**args) if inspect.iscoroutinefunction(func) else asyncio.to_thread(func, **args),
                timeout=tool_timeout
            )
            result_str = str(result)
            if tool_name != "delegate_to":
                self._log(self.name, result_str[:300], "tool_result", self.mode)
            return result_str
        except asyncio.TimeoutError:
            error_msg = f"Error: Tool '{tool_name}' timed out"
        except Exception as e:
            error_msg = f"Error: Tool '{tool_name}' failed: {e}"
        self._log(self.name, f"TOOL ERROR: {tool_name} -> {error_msg}", "tool_error", self.mode)
        return error_msg

    def _extract_codes(self, text: str) -> List[str]:
        codes = []
        for match in re.finditer(r'```(?:python)?\s*\n(.*?)```', text, re.DOTALL):
            code = match.group(1).strip()
            if not code:
                continue
            lines = [l for l in code.split('\n') if l.strip() and not l.strip().startswith('#')]
            if not lines:
                continue
            codes.append(code)
        return codes

    def _validate_code(self, code: str) -> Optional[str]:
        try:
            ast.parse(code)
            return None
        except SyntaxError as e:
            return f"Syntax error: {e}"

    async def _run_code(self, code: str, full_response: str = "") -> str:
        if not code:
            return "Error: Empty code"

        validation_error = self._validate_code(code)
        if validation_error:
            return validation_error

        is_delegate = "delegate_to" in code and "await" in code

        raw_text = ""
        if full_response and "```" in full_response:
            raw_text = full_response[:full_response.index("```")].strip()
        first_sentence = ""
        if raw_text:
            for ch in (". ", "! ", "? "):
                idx = raw_text.find(ch)
                if idx != -1:
                    first_sentence = raw_text[:idx + 1].strip()
                    break
            if not first_sentence:
                first_sentence = raw_text[:80].strip()

        comments = [l.strip().lstrip("#").strip() for l in code.split("\n") if l.strip().startswith("#")]
        if raw_text and comments:
            comment_thought = " → ".join(c[:60] for c in comments[:3])
        elif raw_text:
            non_comments = [l.strip() for l in code.split("\n") if l.strip() and not l.strip().startswith("#")]
            comment_thought = non_comments[0][:80] if non_comments else ""
        else:
            comment_thought = ""

        thought = first_sentence or comment_thought or "Thinking..."
        accent = getattr(self.settings, 'accent_color', 'purple')
        self._update_status(f"[{accent}][italic]{self.name}: {thought}[/italic][/{accent}]")
        if not self._silent:
            console.print(f"  [{accent}]▶[/{accent}] [italic]{thought}[/italic]")

        self._log(self.name, f"EXEC: {code[:200]}...", "ptc_call", self.mode)

        fixed_lines = []
        for line in code.split("\n"):
            stripped = line.lstrip()
            indent = line[:len(line) - len(stripped)]
            if stripped and not stripped.startswith("#") and not stripped.startswith("await "):
                for tool_name in self.tools:
                    stripped = re.sub(
                        rf"(?<!\bawait\s)\b{re.escape(tool_name)}\s*\(",
                        f"await {tool_name}(",
                        stripped
                    )
            fixed_lines.append(f"{indent}{stripped}")
        code = "\n".join(fixed_lines)

        print_outputs = []
        def _captured_print(*args, **kwargs):
            output = " ".join(str(a) for a in args)
            print_outputs.append(output)
            print(output)

        g = {**self.tools, "json": json, "print": _captured_print, "_orchestrator": self}
        lines = code.split("\n")
        indented = "\n".join(f"    {l}" for l in lines)
        wrapped = f"async def __execute():\n{indented}"

        try:
            ast.parse(wrapped)
        except SyntaxError as e:
            err = f"Syntax Error: {e}"
            self._log(self.name, err, "ptc_error", self.mode)
            return err

        try:
            exec(wrapped, g)
            result = await g["__execute"]()
            output_str = "\n".join(print_outputs)

            if result is not None and not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, (dict, list)) else str(result)
            if result and result.strip() and output_str:
                result = f"{output_str}\n{result}"
            elif output_str:
                result = output_str
            elif result is None:
                result = ""

            if result and not is_delegate:
                self._log(self.name, str(result)[:500], "ptc_result", self.mode)
            return str(result) if result else ""
        except asyncio.CancelledError:
            return "Error: Execution cancelled"
        except asyncio.TimeoutError:
            return "Error: Execution timed out"
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            self._log(self.name, f"ERROR: {err_msg}", "ptc_error", self.mode)
            return f"Error: {err_msg}"

    def _get_max_steps(self) -> int:
        return self._max_steps_override or self.settings.max_steps

    def _update_status(self, text: str) -> None:
        if self._silent:
            return
        if self._status:
            self._status.__exit__(None, None, None)
        self._status = console.status(text)
        self._status.__enter__()

    def _start_status(self) -> None:
        if not self._silent:
            accent = getattr(self.settings, 'accent_color', 'purple')
            name_prefix = f"{self.name}: " if self._is_delegated else ""
            self._status = console.status(f"[{accent}][italic]{name_prefix}Thinking...[/italic][/{accent}]")
            self._status.__enter__()

    def _stop_status(self) -> None:
        if not self._silent and self._status:
            self._status.__exit__(None, None, None)
            self._status = None

    async def _get_provider_response(self, system_prompt: str) -> str:
        mode_def = MODES.get(self.mode, MODES["BUILD"])
        delim = generate_random_delimiter(16)
        full_prompt = f"{system_prompt}\n\n{delim}\nFOCUS: {mode_def['prompt']}\n{delim}"

        use_streaming = not self._silent and not self.settings.debug_mode

        if self.settings.debug_mode and not self._silent:
            self._stop_status()
            console.print(Panel(full_prompt, title="[purple]INPUT[/purple]"))

        provider_timeout = getattr(self.settings, 'provider_timeout', 120)

        if use_streaming:
            self._stop_status()
            def _stream_token(token: str) -> None:
                console.print(token, end="", highlight=False)
            response = await asyncio.wait_for(
                self.provider.generate_text(self.history, full_prompt, on_token=_stream_token),
                timeout=provider_timeout
            )
            console.print()
            self._start_status()
        else:
            response = await asyncio.wait_for(
                self.provider.generate_text(self.history, full_prompt),
                timeout=provider_timeout
            )

        if delim in response:
            response = response.replace(delim, "").strip()

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

        step = 0
        max_steps = self._get_max_steps()
        self._start_status()

        try:
            while step < max_steps:
                if self.interrupt_event.is_set():
                    self.interrupt_event.clear()
                    return "[STOP] Interrupted"

                await self._trim_history()
                if self._cached_system_prompt is None:
                    role_cfg = load_role(self.skill_name) or load_role("system")
                    self._cached_system_prompt = self._build_system_prompt(role_cfg, include_roles=self.skill_name == "system", user_input=self._get_trigger_input())
                system_prompt = self._cached_system_prompt

                try:
                    response = await self._get_provider_response(system_prompt)
                except asyncio.TimeoutError:
                    self._log(self.name, "Timeout, retrying", "ptc_error", self.mode)
                    await asyncio.sleep(2)
                    try:
                        response = await self._get_provider_response(system_prompt)
                    except Exception:
                        self._log(self.name, "Provider timeout after retry", "ptc_error", self.mode)
                        return "Error: Provider timeout"
                except Exception as e:
                    err = str(e).lower()
                    if "context" in err and "length" in err:
                        self._log(self.name, "Context overflow, compressing", "ptc_error", self.mode)
                        await self._trim_history()
                        role_cfg = load_role(self.skill_name) or load_role("system")
                        self._cached_system_prompt = self._build_system_prompt(role_cfg, include_roles=self.skill_name == "system", user_input=self._get_trigger_input())
                        try:
                            response = await self._get_provider_response(self._cached_system_prompt)
                        except Exception:
                            self._log(self.name, str(e), "ptc_error", self.mode)
                            return f"Error: {e}"
                    elif "429" in str(e) or "rate limit" in err:
                        self._log(self.name, "Rate limited, waiting 5s", "ptc_error", self.mode)
                        await asyncio.sleep(5)
                        try:
                            response = await self._get_provider_response(system_prompt)
                        except Exception:
                            self._log(self.name, str(e), "ptc_error", self.mode)
                            return f"Error: {e}"
                    else:
                        self._log(self.name, str(e), "ptc_error", self.mode)
                        return f"Error: {e}"

                codes = self._extract_codes(response)
                if codes:
                    if self.settings.debug_mode:
                        self._stop_status()
                        console.print(Panel(response, title="[green]OUTPUT[/green]"))
                        self._start_status()
                    all_results = [await self._run_code(code, response) for code in codes]
                    self.history.extend([{"role": "user", "content": f"[OUTPUT]\n{r}\n[/OUTPUT]"} for r in all_results])
                    self.invalidate_cache()
                    last = all_results[-1] if all_results else ""

                    if last == self._last_result:
                        self._repeat_count += 1
                        if self._repeat_count >= 3:
                            self._stop_status()
                            return f"[STOP] Loop detected: same result {self._repeat_count} times"
                    else:
                        self._repeat_count = 0
                        self._last_result = last

                    if last and last.strip() and not last.startswith("Error:") and not last.startswith("Syntax Error"):
                        self._stop_status()
                        return last
                    step += 1
                    continue
                else:
                    if not response or not response.strip():
                        self._stop_status()
                        return "[ERROR] Provider returned empty. Check API key/model."
                    try:
                        import dirtyjson
                        j = dirtyjson.loads(response.strip())
                        if isinstance(j, dict) and "agent_name" in j:
                            codes = [f'await delegate_to(agent_name="{j["agent_name"]}", task="{j.get("task","")}")\nreturn await response(text=result)']
                    except Exception:
                        pass
                    if not codes:
                        self._log(self.name, response, "response", self.mode)
                        self._stop_status()
                        return response.strip()
                    continue

            return "Max steps reached."
        finally:
            self._stop_status()

    def get_history_summary(self, max_messages: int = -1) -> str:
        if not self.history:
            return "No history."
        msgs = self.history if max_messages < 0 else self.history[-max_messages:]
        lines = []
        for m in msgs:
            role = m.get("role", "?")
            content = str(m.get("content", ""))
            preview = content[:80].replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
            if len(content) > 80:
                preview += "..."
            lines.append(f"[{role}] {preview}")
        return "\n".join(lines)
