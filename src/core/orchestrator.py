import json
import re
import platform
import subprocess
import time
from pathlib import Path
from rich.prompt import Confirm
from rich.console import Console
from src.core.provider import AIProvider
from src.config import GlobalSettings
from src.providers.pollinations import PollinationsProvider
from src.tools import TOOL_REGISTRY, get_tools_schema, download_and_open_image, extract_json_from_text
from src.utils import get_system_context, scan_available_agents, sys_logger, rate_limiter, quota_manager
from src.core.mcp_manager import MCPManager
from src.core.memory import memory_core

console = Console()

class Orchestrator:
    def __init__(self, provider: AIProvider, settings: GlobalSettings):
        self.brain = provider
        self.settings = settings
        self.history = []
        self.media_engine = PollinationsProvider(self.settings.pollinations)
        self.max_steps = self.settings.max_steps
        self.mcp = MCPManager(self.settings)
        self.mcp_initialized = False
        
        self._cached_system_prompt = None
        self._cached_tools_schema = None
        self._cached_mcp_tools = None

    async def _ensure_mcp(self):
        if not self.mcp_initialized:
            try:
                await self.mcp.connect_all()
                self.mcp_initialized = True
                sys_logger.log("system", "mcp_initialized", {"status": "success"})
            except Exception as e:
                sys_logger.log("system", "mcp_connect_fail", {"error": str(e)})
                console.print(f"[yellow][WARNING] MCP initialization failed: {e}[/yellow]")
                self.mcp_initialized = False

    def _build_system_prompt(self) -> str:
        if self._cached_system_prompt:
            return self._cached_system_prompt
        try:
            from ZervGen.src.skills_loader import load_skill
            base_prompt = load_skill("system")
            if not base_prompt:
                raise Exception("System skill not found")
        except Exception as e:
            try:
                with open("src/skills/system.md", "r", encoding="utf-8") as f:
                    base_prompt = f.read()
            except Exception as e2:
                sys_logger.log("system", "prompt_load_fail", {"error": str(e2)})
                base_prompt = "You are ZervGen Supervisor."

        context = get_system_context()
        agents_list = scan_available_agents()
        
        if not self._cached_tools_schema:
            self._cached_tools_schema = get_tools_schema()
        local_tools = self._cached_tools_schema
        
        if not self._cached_mcp_tools:
            self._cached_mcp_tools = self.mcp.get_tools_schema()
        mcp_tools = self._cached_mcp_tools
        
        memories = memory_core.get_recent_memories()
        
        # Get memory stats for context
        try:
            stats = memory_core.evolver.get_stats()
            memory_stats = f"Memory Stats: {stats['total_memories']} memories, {stats['successful_queries']} successful queries"
        except:
            memory_stats = "Memory system active"
        
        all_tools = f"{local_tools}\n{mcp_tools}" if mcp_tools else local_tools

        self._cached_system_prompt = (
            f"{context}\n\n"
            f"{base_prompt}\n\n"
            f"--- GRAPHRAG MEMORY ---\n{memories}\n------------------------\n\n"
            f"MEMORY SYSTEM: {memory_stats}\n"
            f"AVAILABLE AGENTS: {agents_list}\n"
            f"AVAILABLE TOOLS:\n{all_tools}\n\n"
            f"INSTRUCTIONS:\n"
            f"- Use 'search_memory' for semantic search\n"
            f"- Use 'remember' to store important information\n"
            f"- Use 'recall' to search memory\n"
            f"- Use 'evolve_memory' to trigger self-learning\n"
            f"- Use 'memory_stats' to check system status"
        )
        return self._cached_system_prompt

    def _trim_history(self):
        limit = self.settings.history_limit
        if len(self.history) > limit:
            self.history = [self.history[0]] + self.history[-(limit-1):]
            sys_logger.log("system", "history_trimmed", {"new_length": len(self.history)})

    async def process(self, user_input: str) -> str:
        if not await rate_limiter.acquire("api_requests", 1):
            return "Error: Rate limit exceeded. Please wait a moment before making more requests."
        
        if not quota_manager.increment_usage("api_calls_per_day", 1):
            return "Error: Daily API quota exceeded. Please try again tomorrow."
        
        await self._ensure_mcp()
        
        self.history.append({"role": "user", "content": user_input})
        sys_logger.log("user", "input", {"content": user_input})
        memory_core.add_memory(f"User query: {user_input}", "queries", {"type": "user_input"})
        
        system_prompt = self._build_system_prompt()
        step = 0
        last_json = None
        final_response = ""
        
        sys_logger.log("system", "orchestration_start", {"user_input_length": len(user_input)})
        
        while step < self.max_steps:
            self._trim_history()

            try:
                response = await self.brain.generate_text(self.history, system_prompt)
                sys_logger.log("system", "llm_response", {"step": step, "response_length": len(response)})
            except Exception as e:
                error_msg = f"Critical Brain Failure: {e}"
                sys_logger.log("system", "brain_failure", {"error": str(e), "step": step})
                memory_core.add_memory(f"Error occurred: {error_msg}", "errors", {"type": "brain_failure"})
                return error_msg

            json_str = extract_json_from_text(response)
            
            if json_str and json_str == last_json:
                loop_msg = "[SYSTEM] Loop detected. Change arguments."
                self.history.append({"role": "user", "content": loop_msg})
                sys_logger.log("system", "loop_detected", {"step": step})
                step += 1
                continue
            
            if json_str: last_json = json_str

            if self.settings.debug_mode and json_str:
                console.print(f"\n[dim cyan][DEBUG THOUGHT]: {json_str}[/dim cyan]")

            text_part = re.sub(r'```json.*?```', '', response, flags=re.DOTALL).strip()
            if json_str:
                text_part = text_part.replace(json_str, "").strip()

            final_output = text_part if text_part else ""

            if json_str:
                try:
                    action_data = json.loads(json_str)
                    tool_name = action_data.get("tool")
                    args = action_data.get("args", {})
                    
                    sys_logger.log("system", "tool_requested", {"tool": tool_name, "args": args, "step": step})
                    
                    if self.settings.require_approval:
                        console.print(f"\n[bold yellow]✋ APPROVAL REQUIRED[/bold yellow]")
                        console.print(f"Tool: [cyan]{tool_name}[/cyan]")
                        console.print(f"Args: [dim]{args}[/dim]")
                        if not Confirm.ask("Execute?"):
                            denied_msg = f"[SYSTEM] User denied {tool_name}"
                            self.history.append({"role": "user", "content": denied_msg})
                            sys_logger.log("system", "tool_denied", {"tool": tool_name})
                            continue

                    result = ""
                    
                    if tool_name == "generate_image":
                        if not await rate_limiter.acquire("api_requests", 1):
                            result = "Error: Rate limit exceeded for image generation."
                        else:
                            prompt = args.get("prompt", user_input)
                            url = await self.media_engine.generate_image(prompt)
                            local_path = await download_and_open_image(url)
                            result = f"Image Generated: {url} (Saved to {local_path})"
                    
                    elif tool_name in TOOL_REGISTRY:
                        func = TOOL_REGISTRY[tool_name]
                        import inspect
                        if inspect.iscoroutinefunction(func):
                            result = await func(**args)
                        else:
                            result = func(**args)
                    
                    elif tool_name in self.mcp.tools_map:
                        result = await self.mcp.execute_tool(tool_name, args)
                    else:
                        result = f"Error: Tool '{tool_name}' not found."
                        sys_logger.log("system", "tool_not_found", {"tool": tool_name})

                    if len(str(result)) > 8000:
                        result = str(result)[:8000] + "... [TRUNCATED]"

                    observation = f"[OBSERVATION]\nTool: {tool_name}\nResult: {result}"
                    
                    if not self.settings.debug_mode:
                        print(f"\n[Step {step+1}] Executed {tool_name}...")
                    
                    self.history.append({"role": "assistant", "content": response})
                    self.history.append({"role": "user", "content": observation})
                    sys_logger.log("system", "tool_execution", {
                        "tool": tool_name,
                        "result_length": len(result),
                        "step": step,
                        "success": "Error:" not in result
                    })

                except json.JSONDecodeError:
                    error_msg = "[SYSTEM ERROR] Invalid JSON."
                    self.history.append({"role": "assistant", "content": response})
                    self.history.append({"role": "user", "content": error_msg})
                    sys_logger.log("system", "json_decode_error", {"step": step})
                except Exception as e:
                    error_msg = f"[SYSTEM ERROR] Tool failure: {e}"
                    self.history.append({"role": "assistant", "content": response})
                    self.history.append({"role": "user", "content": error_msg})
                    sys_logger.log("system", "tool_exception", {"tool": tool_name, "error": str(e), "step": step})

            if not json_str:
                self.history.append({"role": "assistant", "content": response})
                sys_logger.log("assistant", "final_answer", {"content": response, "step": step})
                final_response = response
                memory_core.add_memory(
                    f"Response to '{user_input[:50]}...': {response[:200]}...",
                    "responses",
                    {"type": "successful", "input_length": len(user_input), "response_length": len(response)}
                )
                
                memory_core.save_session(self.history)
                
                # Trigger self-evolution periodically (every 10 interactions)
                if len(self.history) % 20 == 0:
                    try:
                        evolution_result = memory_core.evolve()
                        sys_logger.log("system", "evolution_triggered", {"result": evolution_result})
                    except Exception as e:
                        sys_logger.log("system", "evolution_failed", {"error": str(e)})
                
                sys_logger.log("system", "orchestration_complete", {"steps": step})
                return response

            step += 1

        sys_logger.log("system", "max_steps_reached", {"max_steps": self.max_steps})
        return final_response if final_response else "Max steps reached without completion."
