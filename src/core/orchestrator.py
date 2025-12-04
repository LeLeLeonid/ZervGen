import json
import re
import platform
import datetime
import pkgutil
import os
import subprocess
import time
from pathlib import Path
from src.core.provider import AIProvider
from src.config import GlobalSettings
from src.providers.pollinations import PollinationsProvider
from src.tools import TOOL_REGISTRY, get_tools_schema, download_and_open_image, extract_json_from_text

class Orchestrator:
    def __init__(self, provider: AIProvider, settings: GlobalSettings):
        self.brain = provider
        self.settings = settings
        self.history = []
        self.media_engine = PollinationsProvider(self.settings.pollinations)

    def _get_system_context(self) -> str:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        os_info = f"{platform.system()} {platform.release()}"
        return f"CONTEXT: [Time: {now}] [OS: {os_info}]"

    def _scan_available_agents(self) -> str:
        agents = []
        agents_path = Path("src/agents")
        if agents_path.exists():
            for _, name, _ in pkgutil.iter_modules([str(agents_path)]):
                agents.append(name)
        return ", ".join(agents)

    def _build_system_prompt(self) -> str:
        try:
            with open("src/prompts/system.md", "r", encoding="utf-8") as f:
                base_prompt = f.read()
        except:
            base_prompt = "You are ZervGen Supervisor."

        context = self._get_system_context()
        agents_list = self._scan_available_agents()
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
        
        system_prompt = self._build_system_prompt()
        response = await self.brain.generate_text(self.history, system_prompt)
        
        json_str = extract_json_from_text(response)
        text_part = re.sub(r'```json.*?```', '', response, flags=re.DOTALL).strip()
        if json_str:
            text_part = text_part.replace(json_str, "").strip()

        final_output = text_part if text_part else ""

        if json_str:
            try:
                action_data = json.loads(json_str)
                tool_name = action_data.get("tool")
                args = action_data.get("args", {})
                
                if tool_name == "generate_image":
                    prompt = args.get("prompt", "")
                    url = await self.media_engine.generate_image(prompt)
                    local_path = await download_and_open_image(url)
                    result = f"\n[Image Generated: {url}]"
                
                elif tool_name in TOOL_REGISTRY:
                    func = TOOL_REGISTRY[tool_name]
                    import inspect
                    if inspect.iscoroutinefunction(func):
                        result = await func(**args)
                    else:
                        result = func(**args)
                else:
                    result = f"Error: Tool '{tool_name}' not found."

                final_output += f"\n\n[Action: {tool_name}]\nResult: {result}"
                
            except json.JSONDecodeError:
                final_output += "\n[System Error: Failed to parse action JSON]"
            except Exception as e:
                final_output += f"\n[System Error: {e}]"

        self.history.append({"role": "assistant", "content": final_output})
        return final_output