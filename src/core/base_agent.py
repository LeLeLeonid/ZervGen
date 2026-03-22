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
from src.tools import TOOL_REGISTRY
from src.utils import get_system_context, sanitize_for_prompt, generate_random_delimiter, count_tokens, add_global_tokens, add_provider_tokens

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
        self.tools: Dict[str, Any] = dict(TOOL_REGISTRY)
        self.interrupt_event = asyncio.Event()
        self._mcp = None
        self._mcp_initialized = False
        self._max_steps_override: Optional[int] = None
        self._silent = silent
        self._status = None
        self._is_delegated = False
        self._auto_mode = getattr(settings, 'auto_mode', False) if settings else False
        self._active_todos: List[str] = []
        self._user_active = False
        self._load_tools()

    @classmethod
    async def create(cls, role: str, mode: str = "BUILD", settings=None, memory=None, initial_history: Optional[List[Dict]] = None) -> "BaseAgent":
        settings = settings or load_config()
        provider_name = getattr(settings, 'provider', 'pollinations')
        provider = get_provider(provider_name, settings)
        if not provider:
            logger.warning(f"Provider '{provider_name}' not found, using pollinations")
            provider = get_provider('pollinations', settings)
        return cls(
            name=role, provider=provider, skill_name=role, settings=settings,
            mode=mode, memory=memory, initial_history=initial_history
        )

    def _load_tools(self) -> None:
        role_cfg = load_role(self.skill_name)
        if role_cfg and role_cfg.tools is not None:
            self.tools = {k: v for k, v in TOOL_REGISTRY.items() if k in role_cfg.tools}
        else:
            self.tools = dict(TOOL_REGISTRY)

    async def _init_mcp(self) -> None:
        if not getattr(self.settings, 'mcp_enabled', True):
            return
        try:
            from src.core.mcp_manager import MCPManager
            self._mcp = MCPManager(self.settings)
            await self._mcp.connect_all()
            for tool_name in self._mcp.tools_map:
                from functools import partial
                self.tools[tool_name] = partial(self._mcp.execute_tool, tool_name)
            self._mcp_initialized = True
            logger.info(f"MCP initialized: {len(self._mcp.tools_map)} tools available")
        except Exception as e:
            logger.warning(f"MCP initialization failed: {e}")
            self._mcp = None
            self._mcp_initialized = False

    async def cleanup(self) -> None:
        if self._mcp and self._mcp_initialized:
            try:
                await self._mcp.cleanup()
            except Exception:
                pass
        self._mcp = None
        self._mcp_initialized = False

    def request_interrupt(self) -> None:
        self.interrupt_event.set()

    def _trim_history(self) -> None:
        if not self.settings.history_trim_enabled:
            return
        if self.memory and hasattr(self.memory, 'get_semantic_slice'):
            self.history = self.memory.get_semantic_slice(self.history, max_tokens=4000)
            return
        max_msgs = self.settings.history_trim_size
        if len(self.history) > max_msgs:
            keep = max_msgs // 2
            system_msgs = [m for m in self.history if m.get("role") == "system"]
            recent = self.history[-keep:]
            existing_system = any(m.get("role") == "system" for m in recent)
            self.history = [system_msgs[0]] + recent if system_msgs and not existing_system else recent

    def _get_tools_schema(self) -> List[str]:
        schema = []
        for name, func in self.tools.items():
            try:
                sig = str(inspect.signature(func)).replace(" -> str", "")
            except (ValueError, TypeError):
                sig = "(...)"
            try:
                doc = (inspect.getdoc(func) or "Tool.").split('\n')[0][:80]
            except Exception:
                doc = "Tool."
            schema.append(f"- {name}{sig}: {doc}")
        return schema

    def _build_system_prompt(self, role_cfg, include_roles: bool = False) -> str:
        context = get_system_context()
        if self.memory:
            now_items = self.memory.get_now().get('items', [])
            long_term_items = self.memory.get_long_term().get('items', [])
            memories_str = f"NOW: {json.dumps(now_items[-5:], ensure_ascii=False)}\nLONG-TERM: {json.dumps(long_term_items[-10:], ensure_ascii=False)}"
        else:
            memories_str = ""

        tools_schema = self._get_tools_schema()

        prompt = f"{role_cfg.prompt if role_cfg else 'You are a ZervGen Agent.'}\n\n=== OP STATE ===\nROLE: {self.skill_name}\nMODE: {self.mode}\n--- CONTEXT ---\n{context}\n--- MEMORY ---\n{memories_str}\n"

        if self._active_todos:
            prompt += "--- ACTIVE TODOS ---\n" + "\n".join(f"- {todo}" for todo in self._active_todos) + "\n\n"

        if include_roles:
            from src.skills_loader import get_roles_overview
            prompt += f"--- ROLES ---\n{get_roles_overview()}\n"

        prompt += f"""--- TOOLS ---
{chr(10).join(tools_schema)}

--- PROTOCOL ---
Use ```python code blocks to call tools. Execute all sequentially.
ALWAYS use 'return await response(...)' to return final answer.
Example:
```python
result = await web_search("latest news")
return await response(text=result)
```
"""
        return prompt

    def _log(self, role: str, content: str, event_type: str = "message", mode: str = "") -> None:
        if self.memory:
            self.memory.log_event_sync(role, content, event_type, mode or self.mode)
        if event_type in ("tool_call", "tool_result", "agent_spawn", "delegation_start", "delegation_result"):
            console.print(f"[dim]{role}: {content[:100]}[/dim]")
        if event_type in ("input", "response", "ptc_call", "ptc_result", "ptc_error", "agent_start", "delegation_result", "loop"):
            entry_role = "user" if event_type == "input" else role
            self.history.append({"role": entry_role, "content": content})

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
        return [match.group(1).strip() for match in re.finditer(r'```(?:python)?\s*\n?(.*?)```', text, re.DOTALL) if match.group(1).strip()]

    def _validate_code(self, code: str) -> Optional[str]:
        try:
            ast.parse(code)
            return None
        except SyntaxError as e:
            return f"Syntax error: {e}"

    async def _run_code(self, code: str) -> str:
        if not code:
            return "Error: Empty code"

        validation_error = self._validate_code(code)
        if validation_error:
            return validation_error

        is_delegate = "delegate_to" in code and "await" in code
        self._log(self.name, f"EXEC: {code[:200]}...", "ptc_call", self.mode)

        print_outputs = []
        def _captured_print(*args, **kwargs):
            output = " ".join(str(a) for a in args)
            print_outputs.append(output)
            print(output)

        g = {**self.tools, "json": json, "print": _captured_print}
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
            result = output_str + "\n" + str(result) if result else output_str if print_outputs else ("" if result is None else str(result))
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
        if self._status:
            self._status.__exit__(None, None, None)
            self._status = console.status(text)
            self._status.__enter__()

    def _start_status(self) -> None:
        if not self._silent:
            name_prefix = f"{self.name}: " if self._is_delegated else ""
            self._status = console.status(f"[bold purple]{name_prefix}Thinking...[/bold purple]")
            self._status.__enter__()

    def _stop_status(self) -> None:
        if not self._silent and self._status:
            self._status.__exit__(None, None, None)
            self._status = None

    async def _get_provider_response(self, system_prompt: str) -> str:
        mode_def = MODES.get(self.mode, MODES["BUILD"])
        delim = generate_random_delimiter(16)
        full_prompt = f"{system_prompt}\n\n{delim}\nFOCUS: {mode_def['prompt']}\n{delim}"

        if self.settings.debug_mode and not self._silent:
            self._stop_status()
            console.print(Panel(full_prompt, title="[purple]INPUT[/purple]"))
            self._start_status()

        provider_timeout = getattr(self.settings, 'provider_timeout', 120)
        response = await asyncio.wait_for(
            self.provider.generate_text(self.history, full_prompt),
            timeout=provider_timeout
        )

        first_line = response.strip().split('\n')[0][:100] if response.strip() else "Thinking..."
        if first_line.strip().startswith("```"):
            first_line = "Thinking..."

        name_prefix = f"{self.name}: " if self._is_delegated else ""
        self._update_status(f"[bold purple]{name_prefix}{first_line}[/bold purple]")

        in_tok, out_tok = count_tokens(self.history, response)
        add_global_tokens(in_tok + out_tok)
        if self.provider:
            p_name = self.provider.name if hasattr(self.provider, 'name') else (
                self.provider.META.name if hasattr(self.provider, 'META') else self.settings.provider
            )
            add_provider_tokens(p_name, in_tok, out_tok)

        if self.settings.debug_mode and not self._silent:
            self._stop_status()
            console.print(Panel(response, title="[green]OUTPUT[/green]"))
            self._start_status()

        return response

    async def run(self, task: str) -> str:
        if not task or not isinstance(task, str):
            return "Error: Invalid task"
        self._load_tools()
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

                self._trim_history()
                role_cfg = load_role(self.skill_name) or load_role("system")
                system_prompt = self._build_system_prompt(role_cfg, include_roles=self.skill_name == "system")

                try:
                    response = await self._get_provider_response(system_prompt)
                except asyncio.TimeoutError:
                    self._log(self.name, "Provider timeout", "ptc_error", self.mode)
                    return "Error: Provider timeout"
                except Exception as e:
                    self._log(self.name, str(e), "ptc_error", self.mode)
                    return f"Error: {e}"

                codes = self._extract_codes(response)
                if codes:
                    all_results = [await self._run_code(code) for code in codes]
                    self.history.extend([{"role": self.name, "content": r} for r in all_results])
                    if all_results:
                        self._stop_status()
                        return all_results[-1]
                    step += 1
                    continue
                else:
                    if not response or not response.strip():
                        step += 1
                        continue
                    self._log(self.name, response, "response", self.mode)
                    self._stop_status()
                    return response

            return "Max steps reached."
        finally:
            self._stop_status()

    async def run_with_context(self, task: str, context: Dict = None) -> str:
        if context:
            if "history" in context:
                self.history = list(context["history"])
            if "mode" in context:
                self.mode = context["mode"].upper()
        return await self.run(task)

    def add_to_history(self, role: str, content: str) -> None:
        self._log(role, content, "manual")

    def clear_history(self) -> None:
        self.history.clear()

    def get_history_summary(self) -> str:
        return "\n".join(f"[{m['role']}]: {m['content'][:50]}..." for m in self.history[-10:]) if self.history else "No history."

    async def summarize_context(self, max_messages: int = 20) -> str:
        if len(self.history) <= max_messages:
            return "No summarization needed."

        system_msg = self.history[0] if self.history and self.history[0].get("role") == "system" else None
        to_summarize = self.history[1:] if system_msg else self.history

        if len(to_summarize) <= max_messages:
            return "No summarization needed."

        summary_prompt = "Summarize this conversation concisely, preserving key information:"
        for msg in to_summarize:
            role = msg.get("role", "user")
            content = msg.get("content", "")[:500]
            summary_prompt += f"{role}: {content}\n\n"
        summary_prompt += "Output a brief summary (2-4 sentences) that captures the essence of what was discussed."

        try:
            response = await self.provider.generate_text(
                [{"role": "user", "content": summary_prompt}],
                "You are a concise assistant."
            )
            in_tok, out_tok = count_tokens([{"content": summary_prompt}], response)
            add_global_tokens(in_tok + out_tok)
            add_provider_tokens(self.settings.provider, in_tok, out_tok)

            if system_msg:
                self.history = [system_msg, {"role": "user", "content": f"[SUMMARY] {response}"}]
            else:
                self.history = [{"role": "user", "content": f"[SUMMARY] {response}"}]

            return f"Context summarized: {response[:200]}..."
        except Exception as e:
            return f"Summarization failed: {e}"

    async def auto_loop(self, prompt: str = None) -> str:
        from src.config import AUTO_MODE_PROMPT
        from rich.console import Console
        auto_console = Console()
        prompt = prompt or AUTO_MODE_PROMPT
        while self._auto_mode:
            if self.interrupt_event.is_set():
                self.interrupt_event.clear()
                break
            if self._user_active:
                await asyncio.sleep(1)
                continue
            try:
                result = await self.run(prompt)
                if result and len(result) > 10:
                    if result.startswith("Error"):
                        auto_console.print(f"[red]Auto Error: {result[:200]}[/red]")
                    else:
                        auto_console.print(f"[green]Auto Result: {result[:300]}...[/green]")
                        self._log(self.name, f"Auto: {result[:200]}", "auto_result")
                else:
                    auto_console.print(f"[yellow]Auto: No significant result[/yellow]")
            except Exception as e:
                auto_console.print(f"[red]Auto Exception: {e}[/red]")
                self._log(self.name, f"Auto error: {e}", "auto_error")
        return "Auto mode stopped."

    def toggle_auto(self, enabled: bool = None) -> bool:
        self._auto_mode = not self._auto_mode if enabled is None else enabled
        return self._auto_mode
