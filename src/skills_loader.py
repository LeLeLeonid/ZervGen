import threading
import yaml
from pathlib import Path
from typing import Dict, List, Optional

SKILLS_DIR = Path(__file__).parent / "skills"
AGENTS_DIR = SKILLS_DIR / "AGENTS"


class RoleConfig:
    def __init__(self, name: str, content: str, meta: Dict):
        self.name = name
        self.prompt = content
        self.description = meta.get("description", "No description provided.")
        self.tools = meta.get("tools", [])


class SkillConfig:
    def __init__(self, name: str, tags: List[str], context: str, description: str = ""):
        self.name = name
        self.tags = [t.lower() for t in tags]
        self.context = context
        self.description = description


class SkillIndex:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True
        self.skills: Dict[str, SkillConfig] = {}
        self.tag_map: Dict[str, List[str]] = {}
        self._load_all()

    def _load_all(self):
        for dir_path in (SKILLS_DIR / "INTEGRATION", SKILLS_DIR / "UTILITIES"):
            if not dir_path.exists():
                continue
            for md_file in dir_path.glob("*.md"):
                skill = self._parse_skill(md_file)
                if skill:
                    self.skills[skill.name] = skill
                    for tag in skill.tags:
                        self.tag_map.setdefault(tag, []).append(skill.name)

    def _parse_skill(self, path: Path) -> Optional[SkillConfig]:
        try:
            content = path.read_text(encoding="utf-8")
            if not content.startswith("---"):
                return None
            parts = content.split("---", 2)
            if len(parts) < 3:
                return None
            meta = yaml.safe_load(parts[1]) or {}
            return SkillConfig(
                name=path.stem,
                tags=meta.get("tags", []),
                context=parts[2].strip(),
                description=meta.get("description", "")
            )
        except Exception:
            return None

    def find_by_tags(self, tags: List[str]) -> Optional[SkillConfig]:
        tags_lower = [t.lower() for t in tags if t]
        if not tags_lower:
            return None
        scores: Dict[str, int] = {}
        for tag in tags_lower:
            if tag in self.tag_map:
                for skill_name in self.tag_map[tag]:
                    scores[skill_name] = scores.get(skill_name, 0) + 1
        if not scores:
            return None
        return self.skills.get(max(scores, key=scores.get))

    def reload(self):
        self.skills.clear()
        self.tag_map.clear()
        self._load_all()


skill_index = SkillIndex()


def _load_from_path(path: Path) -> Optional[RoleConfig]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                meta = yaml.safe_load(parts[1]) or {}
                return RoleConfig(path.stem, parts[2].strip(), meta)
        return RoleConfig(path.stem, raw, {"description": "Legacy role", "tools": []})
    except Exception:
        return None


def load_role(name: str) -> Optional[RoleConfig]:
    agent_path = AGENTS_DIR / f"{name}.md"
    if agent_path.exists():
        return _load_from_path(agent_path)
    path = SKILLS_DIR / f"{name}.md"
    return _load_from_path(path)


def role_exists(name: str) -> bool:
    return (AGENTS_DIR / f"{name}.md").exists() or (SKILLS_DIR / f"{name}.md").exists()


def get_all_roles() -> Dict[str, RoleConfig]:
    roles = {}
    for f in AGENTS_DIR.glob("*.md"):
        role = _load_from_path(f)
        if role:
            roles[f.stem] = role
    for f in SKILLS_DIR.glob("*.md"):
        if f.stem != "system" and f.stem not in roles:
            role = _load_from_path(f)
            if role:
                roles[f.stem] = role
    return roles


def get_roles_overview() -> str:
    roles = get_all_roles()
    return "\n".join(f"- {name}: {config.description}" for name, config in roles.items()) if roles else "No specialized roles available."
