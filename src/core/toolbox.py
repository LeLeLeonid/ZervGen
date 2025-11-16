import json
import importlib
from pathlib import Path
from functools import partial
from ..tools.pollinations_client import PollinationsClient

class Toolbox:
    def __init__(self, client: PollinationsClient, project_root: Path):
        self.manifest_path = project_root / "tools.json"
        self.manifest = self._load_manifest()
        self.tools = self._load_tools(client)

    def _load_manifest(self) -> dict:
        with open(self.manifest_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _load_tools(self, client: PollinationsClient) -> dict:
        loaded_tools = {}
        for tool_name, config in self.manifest.items():
            module_name = config['module_name']
            module = importlib.import_module(f"src.tools.{module_name}")
            func = getattr(module, "execute")
            if "client" in func.__code__.co_varnames:
                loaded_tools[tool_name] = partial(func, client)
            else:
                loaded_tools[tool_name] = func
        return loaded_tools

    def get_tool(self, name: str):
        return self.tools.get(name)

    def get_manifest_for_prompt(self) -> str:
        docs = []
        for name, config in self.manifest.items():
            docs.append(f"- `{name}`: {config['description']}")
        return "\n".join(docs)