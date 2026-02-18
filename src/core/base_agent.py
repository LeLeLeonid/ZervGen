import asyncio
import json
import logging
import inspect
from typing import Any, Dict, List, Optional
from rich.console import Console
from rich.panel import Panel
from src.config import MODES, GlobalSettings, load_config
from src.core.memory import memory_core
from src.core.provider import AIProvider, get_provider
from src.skills_loader import load_role
from src.tools import TOOL_REGISTRY
from src.utils import extract_json_from_text, get_system_context, sanitize_for_prompt, generate_random_delimiter, count_tokens, format_token_display, detect_loop, add_global_tokens, get_global_tokens, reset_global_tokens

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
        self.memory = memory or memory_core
        self.history: List[Dict[str, str]] = list(initial_history) if initial_history else []
        self.tools: Dict[str, Any] = dict(TOOL_REGISTRY)
        self.last_title = "Ready"
        self.interrupt_event = asyncio.Event()
        self._mcp = None
        self._mcp_initialized = False
        self._max_steps_override: Optional[int] = None
        self._silent = silent
        self._status = None
        self._is_delegated = False
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
            name=role,
            provider=provider,
            skill_name=role,
            settings=settings,
            mode=mode,
            memory=memory or memory_core,
            initial_history=initial_history
        )

    def _load_tools(self) -> None:
        role_cfg = load_role(self.skill_name)
        if role_cfg and role_cfg.tools:
            self.tools = {k: v for k, v in TOOL_REGISTRY.items() if k in role_cfg.tools or k in ["response", "find_skill"]}
        else:
            self.tools = dict(TOOL_REGISTRY)

    async def _init_mcp(self) -> None:
        if not getattr(self.settings, 'mcp_enabled', True) or self._mcp_initialized:
            return
        try:
            from src.core.mcp_manager import MCPManager
            self._mcp = MCPManager(self.settings)
            await self._mcp.connect_all()
            for tool_name in self._mcp.tools_map:
                def make_mcp_tool(name):
                    async def mcp_tool(**kwargs):
                        return await self._mcp.execute_tool(name, kwargs)
                    return mcp_tool
                self.tools[tool_name] = make_mcp_tool(tool_name)
            self._mcp_initialized = True
            logger.info(f"MCP initialized: {len(self._mcp.tools_map)} tools available")
        except Exception as e:
            logger.warning(f"MCP initialization failed: {e}")
            self._mcp = None

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
        max_msgs = self.settings.history_trim_size
        if len(self.history) > max_msgs:
            del self.history[:-max_msgs]

    def _build_system_prompt(self, role_cfg, include_roles: bool = False) -> str:
        context = get_system_context()
        memories = self.memory.get_recent_memories(limit=5) if self.memory else []
        mode_def = MODES.get(self.mode, MODES["BUILD"])
        memories_str = json.dumps(memories, ensure_ascii=False) if memories else ""
        
        tools_schema = []
        for name, func in self.tools.items():
            sig = str(inspect.signature(func)).replace(" -> str", "")
            doc = (inspect.getdoc(func) or "Tool.").split('\n')[0][:80]
            tools_schema.append(f"- {name}{sig}: {doc}")
        
        prompt = (
            f"{role_cfg.prompt if role_cfg else 'You are a ZervGen Agent.'}\n\n"
            f"=== OP STATE ===\nROLE: {self.skill_name}\nMODE: {self.mode}\n"
            f"--- CONTEXT ---\n{context}\n"
            f"--- MEMORY ---\n{memories_str}\n"
        )
        if include_roles:
            from src.skills_loader import get_roles_overview
            prompt += f"--- ROLES ---\n{get_roles_overview()}\n"
        prompt += (
            f"--- TOOLS ---\n" + "\n".join(tools_schema) + "\n\n"
            f"--- PROTOCOL ---\n"
            f"1. Analyze task silently.\n"
            f"2. Output JSON action.\n"
            f"3. FORMAT: {{\"title\": \"...\", \"tool\": \"name\", \"args\": {{}}}}\n"
            f"4. MULTI: {{\"title\": \"...\", \"tool\": [{{\"name\": \"...\", \"args\": {{}}}}]}}\n"
            f"5. FINAL: Use 'response' tool."
        )
        return prompt

    def _log_event(self, role: str, content: str, event_type: str = "message") -> None:
        if self.memory and not self._is_delegated:
            try:
                self.memory.log_event_sync(role, content, event_type)
            except Exception as e:
                logger.debug(f"Event logging failed: {e}")

    def _update_status(self, title: str = None) -> None:
        if self._silent or not self._status:
            return
        display_title = title or self.last_title
        # token_str = format_token_display(get_global_tokens())
        token_str = False
        if token_str:
            self._status.update(f"⏳ [bold purple]{display_title}[/bold purple] | {token_str}")
        else:
            self._status.update(f"⏳ [bold purple]{display_title}[/bold purple]")

    async def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name in self.tools:
            func = self.tools[tool_name]
            try:
                tool_timeout = getattr(self.settings, 'tool_timeout', 60)
                if asyncio.iscoroutinefunction(func):
                    result = await asyncio.wait_for(func(**args), timeout=tool_timeout)
                else:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(func, **args),
                        timeout=tool_timeout
                    )
                return str(result)
            except asyncio.TimeoutError:
                return f"Error: Tool '{tool_name}' timed out"
            except Exception as e:
                return f"Error: Tool '{tool_name}' failed: {e}"
        return f"Error: Tool '{tool_name}' not found"

    async def _execute_tools_parallel(self, tool_calls: List[Dict[str, Any]]) -> List[str]:
        async def execute_single(call: Dict[str, Any]) -> str:
            return await self._execute_tool(call.get("tool"), call.get("args", {}))
        tasks = [execute_single(call) for call in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [str(r) if not isinstance(r, Exception) else f"Error: {r}" for r in results]

    def _get_max_steps(self) -> int:
        return self._max_steps_override or self.settings.max_steps

    async def _handle_step_result(self, tool_name: str, args: Dict[str, Any], result: str, json_str: str) -> None:
        self.history.append({"role": "assistant", "content": json_str})
        self.history.append({"role": "user", "content": result})

    async def _run_critic(self, proposed_response: str) -> Optional[str]:
        critic_prompt = f"""CRITIC: Self-Interrogation before response.

PROPOSED RESPONSE:
{proposed_response[:500]}

Answer these questions honestly:
1. Did I actually execute/test the code? (YES/NO)
2. Did I check for edge cases? (YES/NO)
3. Does this match the original task requirements? (YES/NO)
4. Is the response complete and actionable? (YES/NO)

If any answer is NO, briefly explain what needs to be done.
Output ONLY: APPROVED or FIX_NEEDED: [brief reason]"""
        
        try:
            temp_history = [{"role": "user", "content": critic_prompt}]
            response = await asyncio.wait_for(
                self.provider.generate_text(temp_history, "You are a critical reviewer."),
                timeout=30
            )
            if "FIX_NEEDED" in response.upper():
                return response
            return None
        except Exception:
            return None

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
        self.history.append({"role": "user", "content": sanitized})
        self._log_event("user", sanitized, "task")

        step = 0
        self.last_title = "Loading..."
        max_steps = self._get_max_steps()
        
        if self._is_delegated and self.memory:
            try:
                self.memory.log_full(
                    role=self.name,
                    content=f"Starting task: {sanitized[:200]}",
                    tool="start",
                    args={"task": sanitized},
                    title="Agent Started"
                )
            except Exception:
                pass

        if not self._silent:
            self._status = console.status(f"⏳ [bold purple]{self.last_title}[/bold purple]")
            self._status.__enter__()

        try:
            while step < max_steps:
                if self.interrupt_event.is_set():
                    self.interrupt_event.clear()
                    return "[STOP] Interrupted"

                loop_content = detect_loop(self.history)
                if loop_content:
                    if not self._silent:
                        console.print("[bold red]Loop detected! Breaking cycle.[/bold red]")
                    return f"[LOOP DETECTED] Agent was repeating: {loop_content[:100]}..."

                self._trim_history()
                
                role_cfg = load_role(self.skill_name) or load_role("system")
                system_prompt = self._build_system_prompt(role_cfg)
                mode_def = MODES.get(self.mode, MODES["BUILD"])

                delim = generate_random_delimiter(16)
                full_prompt = f"{system_prompt}\n\n{delim}\nFOCUS: {mode_def['prompt']}\n{delim}"

                if self.provider is None:
                    return "Error: No provider configured"

                try:
                    if self.settings.debug_mode and not self._silent:
                        console.print(Panel(full_prompt, title="[purple]INPUT[/purple]"))

                    provider_timeout = getattr(self.settings, 'provider_timeout', 120)
                    response = await asyncio.wait_for(
                        self.provider.generate_text(self.history, full_prompt),
                        timeout=provider_timeout
                    )

                    in_tok, out_tok = count_tokens(self.history, response)
                    add_global_tokens(in_tok + out_tok)
                    self._update_status()

                    if self.settings.debug_mode and not self._silent:
                        console.print(Panel(response, title="[green]OUTPUT[/green]"))

                except asyncio.TimeoutError:
                    return "Error: Provider timeout"
                except Exception as e:
                    return f"Error: {e}"

                json_str = extract_json_from_text(response)
                if not json_str:
                    self.last_title = "Direct Response"
                    self._update_status()
                    self.history.append({"role": "assistant", "content": response})
                    return response

                try:
                    data = json.loads(json_str)
                    self.last_title = data.get("title", "Executing...")
                    tool_data = data.get("tool")
                    self._update_status()
                    
                    if isinstance(tool_data, list):
                        tool_calls = [{"tool": t.get("name"), "args": t.get("args", {})} for t in tool_data]
                        results = await self._execute_tools_parallel(tool_calls)
                        
                        response_results = []
                        for i, r in enumerate(results):
                            if tool_calls[i].get("tool") == "response":
                                response_results.append(r)
                        
                        if response_results:
                            final = response_results[0]
                            if getattr(self.settings, 'critic_enabled', False):
                                critic_result = await self._run_critic(final)
                                if critic_result:
                                    self.history.append({"role": "user", "content": f"CRITIC FEEDBACK:\n{critic_result}"})
                                    continue
                            self.history.append({"role": "assistant", "content": final})
                            return str(final)
                        
                        summary = "\n".join(f"- {tool_calls[i].get('tool', 'tool')}: {r[:100]}..." for i, r in enumerate(results))
                        self.history.append({"role": "assistant", "content": json_str})
                        self.history.append({"role": "user", "content": f"RESULTS:\n{summary}"})
                        step += 1
                        continue

                    if isinstance(tool_data, str):
                        tool_name = tool_data
                        args = data.get("args", {})
                    elif isinstance(tool_data, dict):
                        tool_name = tool_data.get("name")
                        args = tool_data.get("args") or data.get("args", {})
                        if not tool_name:
                            for key in tool_data.keys():
                                if key not in ("args", "name"):
                                    tool_name = key
                                    args = tool_data[key] if isinstance(tool_data[key], dict) else {}
                                    break
                    elif "name" in data and data["name"] not in ("response", None):
                        # Format: {"name": "delegate_to", "args": {...}}
                        tool_name = data.get("name")
                        args = data.get("args", {})
                    else:
                        tool_name = data.get("tool")
                        args = data.get("args", {})

                    if tool_name == "response" or tool_name is None:
                        final = args.get("text") or args.get("content") or args.get("message")
                        if not final:
                            for v in args.values():
                                if isinstance(v, str) and len(v) > 10:
                                    final = v
                                    break
                        if not final:
                            final = "Task completed."
                        
                        if getattr(self.settings, 'critic_enabled', False):
                            critic_result = await self._run_critic(final)
                            if critic_result:
                                self.history.append({"role": "user", "content": f"CRITIC FEEDBACK:\n{critic_result}"})
                                continue
                        
                        if self._is_delegated and self.memory:
                            try:
                                self.memory.log_full(
                                    role=self.name,
                                    content=final[:1000] if final else "",
                                    tool="response",
                                    args={},
                                    title="Task Completed"
                                )
                            except Exception:
                                pass
                        
                        self.history.append({"role": "assistant", "content": final})
                        return str(final)

                    result = await self._execute_tool(tool_name, args)
                    if self.memory:
                        try:
                            self.memory.log_full(
                                role=self.name,
                                content=result,
                                tool=tool_name,
                                args=args,
                                title=self.last_title
                            )
                        except Exception:
                            pass

                    await self._handle_step_result(tool_name, args, result, json_str)
                    step += 1

                except Exception as e:
                    self.history.append({"role": "user", "content": f"ERROR: {e}"})
                    step += 1

            return "Max steps reached."
        finally:
            if not self._silent and self._status:
                self._status.__exit__(None, None, None)
                self._status = None

    async def run_with_context(self, task: str, context: Dict = None) -> str:
        if context:
            if "history" in context:
                self.history = list(context["history"])
            if "mode" in context:
                self.mode = context["mode"].upper()
        return await self.run(task)

    def add_to_history(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def clear_history(self) -> None:
        self.history.clear()

    def get_history_summary(self) -> str:
        if not self.history:
            return "No history."
        return "\n".join(f"[{m['role']}]: {m['content'][:50]}..." for m in self.history[-10:])
