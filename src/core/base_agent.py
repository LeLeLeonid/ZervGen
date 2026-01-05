from abc import ABC
from typing import List, Dict, Callable
import json
from pathlib import Path
from src.core.provider import AIProvider
from src.tools import TOOL_REGISTRY

class BaseAgent(ABC):
    def __init__(self, name: str, provider: AIProvider, skill_name: str):
        self.name = name
        self.provider = provider
        self.system_prompt = self._load_skill(skill_name)
        self.tools: Dict[str, Callable] = {}
        self.history: List[Dict] = []

    def _load_skill(self, skill_name: str) -> str:
        """Load skill from skills directory (new system)"""
        try:
            from ZervGen.src.skills_loader import load_skill
            skill_content = load_skill(skill_name)
            if skill_content:
                return skill_content
        except:
            pass

        try:
            path = Path("src/prompts") / f"{skill_name}.md"
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
        except:
            pass
        
        return f"You are {self.name}."

    def load_tools(self, tool_names: List[str]):
        for name in tool_names:
            if name in TOOL_REGISTRY:
                self.tools[name] = TOOL_REGISTRY[name]

    async def run(self, task: str) -> str:
        self.history.append({"role": "user", "content": f"TASK: {task}"})
        
        tool_desc = ", ".join(self.tools.keys())
        prompt = (
            f"{self.system_prompt}\n"
            f"AVAILABLE TOOLS: {tool_desc}\n"
            "To use a tool, output JSON: {\"tool\": \"tool_name\", \"args\": { ... }}\n"
            "To finish, output text."
        )

        response = await self.provider.generate_text(self.history, prompt)
        
        try:
            clean_resp = response.replace("```json", "").replace("```", "").strip()
            if "{" in clean_resp and "tool" in clean_resp:
                data = json.loads(clean_resp)
                tool_name = data.get("tool")
                args = data.get("args", {})
                
                if tool_name in self.tools:
                    result = await self.tools[tool_name](**args)
                    self.history.append({"role": "assistant", "content": response})
                    self.history.append({"role": "user", "content": f"TOOL RESULT: {result}"})
                    
                    final_response = await self.provider.generate_text(self.history, prompt)
                    return final_response
        except Exception:
            pass

        self.history.append({"role": "assistant", "content": response})
        return response