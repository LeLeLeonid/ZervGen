import os
import yaml
from pathlib import Path
from typing import Dict, Optional

SKILLS_DIR = Path(__file__).parent / "skills"

class RoleConfig:
    def __init__(self, name: str, content: str, meta: Dict):
        self.name = name
        self.prompt = content
        self.description = meta.get("description", "No description provided.")
        self.tools = meta.get("tools", [])

def load_role(name: str) -> Optional[RoleConfig]:
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        return None
    
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    
    if raw.startswith("---"):
        try:
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                meta = yaml.safe_load(parts[1])
                content = parts[2].strip()
                return RoleConfig(name, content, meta)
        except Exception as e:
            print(f"[System] Warning: Failed to parse YAML for {name}: {e}")
            
    return RoleConfig(name, raw, {"description": "Legacy role", "tools": []})

def get_all_roles() -> Dict[str, RoleConfig]:
    roles = {}
    if not SKILLS_DIR.exists():
        return roles
    
    for f in SKILLS_DIR.glob("*.md"):
        if f.stem == "system":
            continue
        
        role = load_role(f.stem)
        if role:
            roles[f.stem] = role
    return roles

def get_roles_overview() -> str:
    roles = get_all_roles()
    if not roles:
        return "No specialized roles available."
    
    schema = []
    for name, config in roles.items():
        schema.append(f"- {name}: {config.description}")
    
    return "\n".join(schema)