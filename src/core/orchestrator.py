import json
import re
import platform
import subprocess
import os
import time
from pathlib import Path
from src.core.provider import AIProvider
from src.config import GlobalSettings
from src.providers.pollinations import PollinationsProvider
from src.tools import TOOL_REGISTRY, get_tools_schema, download_and_open_image, extract_json_from_text
from src.utils import get_system_context, scan_available_agents
from src.logger import sys_logger

class Orchestrator:
    def __init__(self, provider: AIProvider, settings: GlobalSettings):
        self.brain = provider
        self.settings = settings
        self.history = []
        self.media_engine = PollinationsProvider(self.settings.pollinations)
        self.max_steps = 10

    def _build_system_prompt(self) -> str:
        try:
            with open("src/prompts/system.md", "r", encoding="utf-8") as f:
                base_prompt = f.read()
        except:
            base_prompt = "You are ZervGen Supervisor."

        context = get_system_context()
        agents_list = scan_available_agents()
        tools_list = get_tools_schema()

        full_prompt = (
            f"{context}\n\n"
            f"{base_prompt}\n\n"
            f"AVAILABLE AGENTS: {agents_list}\n"
            f"AVAILABLE TOOLS: {tools_list}"
        )
        return full_prompt

    async def process(self, user_input: str) -> str:
        self.history.append({"role": "user", "content": user_input})
        sys_logger.log("user", "input", {"content": user_input})
        
        system_prompt = self._build_system_prompt()
        step = 0
        
        while step < self.max_steps:
            try:
                response = await self.brain.generate_text(self.history, system_prompt)
            except Exception as e:
                return f"Critical Brain Failure: {e}"

            json_str = extract_json_from_text(response)
            
            if not json_str:
                self.history.append({"role": "assistant", "content": response})
                sys_logger.log("assistant", "final_answer", {"content": response})
                return response

            self.history.append({"role": "assistant", "content": json_str})
            sys_logger.log("assistant", "thought", {"content": json_str})

            try:
                action_data = json.loads(json_str)
                tool_name = action_data.get("tool")
                args = action_data.get("args", {})
                
                result = ""
                
                if tool_name == "generate_image":
                    prompt = args.get("prompt", "")
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
                else:
                    result = f"Error: Tool '{tool_name}' not found."

                observation = f"[OBSERVATION]\nTool: {tool_name}\nResult: {result}"
                self.history.append({"role": "user", "content": observation})
                sys_logger.log("system", "tool_execution", {"tool": tool_name, "result": result})
                
                # Feedback loop: Print intermediate step to CLI (optional, handled by CLI usually)
                print(f"\n[Step {step+1}] Executed {tool_name}...")

            except json.JSONDecodeError:
                error_msg = "[SYSTEM ERROR] Invalid JSON format. Please correct syntax."
                self.history.append({"role": "user", "content": error_msg})
                sys_logger.log("system", "error", {"msg": "JSON Decode Error"})
            except Exception as e:
                error_msg = f"[SYSTEM ERROR] Tool execution failed: {e}"
                self.history.append({"role": "user", "content": error_msg})
                sys_logger.log("system", "error", {"msg": str(e)})

            step += 1

        return "Max steps reached. Task may be incomplete."