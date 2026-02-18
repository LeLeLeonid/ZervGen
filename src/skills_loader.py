import threading
import yaml
from pathlib import Path
from typing import Dict, List, Optional

SKILLS_DIR = Path(__file__).parent / "skills"
AGENTS_DIR = SKILLS_DIR / "AGENTS"


class RoleConfig:
    """Configuration for an agent role (AGENTS folder)."""
    def __init__(self, name: str, content: str, meta: Dict):
        self.name = name
        self.prompt = content
        self.description = meta.get("description", "No description provided.")
        self.tools = meta.get("tools", [])


class SkillConfig:
    """Configuration for a skill (INTEGRATION/UTILITIES folders)."""
    def __init__(self, name: str, tags: List[str], context: str, description: str = ""):
        self.name = name
        self.tags = [t.lower() for t in tags]
        self.context = context
        self.description = description


class SkillIndex:
    """Singleton index of all skills with tag mapping."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.skills: Dict[str, SkillConfig] = {}
        self.tag_map: Dict[str, List[str]] = {}
        self._load_all()

    def _load_all(self):
        """Load all skills from INTEGRATION/ and UTILITIES/."""
        search_dirs = [
            SKILLS_DIR / "INTEGRATION",
            SKILLS_DIR / "UTILITIES",
        ]
        for dir_path in search_dirs:
            if not dir_path.exists():
                continue
            for md_file in dir_path.glob("*.md"):
                skill = self._parse_skill(md_file)
                if skill:
                    self.skills[skill.name] = skill
                    for tag in skill.tags:
                        if tag not in self.tag_map:
                            self.tag_map[tag] = []
                        if skill.name not in self.tag_map[tag]:
                            self.tag_map[tag].append(skill.name)

    def _parse_skill(self, path: Path) -> Optional[SkillConfig]:
        """Parse skill file with frontmatter."""
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
        """Find best matching skill for tags (case-insensitive)."""
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
        
        best_name = max(scores, key=scores.get)
        return self.skills.get(best_name)

    def reload(self):
        """Reload all skills."""
        self.skills.clear()
        self.tag_map.clear()
        self._load_all()


skill_index = SkillIndex()


def _load_from_path(path: Path) -> Optional[RoleConfig]:
    """Load role/agent from path."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                meta = yaml.safe_load(parts[1]) or {}
                content = parts[2].strip()
                return RoleConfig(path.stem, content, meta)
        return RoleConfig(path.stem, raw, {"description": "Legacy role", "tools": []})
    except Exception:
        return None


def load_role(name: str) -> Optional[RoleConfig]:
    """Load agent role by name."""
    agent_path = AGENTS_DIR / f"{name}.md"
    result = _load_from_path(agent_path)
    if result:
        return result
    path = SKILLS_DIR / f"{name}.md"
    return _load_from_path(path)


def role_exists(name: str) -> bool:
    """Check if role exists."""
    return (AGENTS_DIR / f"{name}.md").exists() or (SKILLS_DIR / f"{name}.md").exists()


def get_all_roles() -> Dict[str, RoleConfig]:
    """Get all agent roles."""
    roles = {}
    if AGENTS_DIR.exists():
        for f in AGENTS_DIR.glob("*.md"):
            role = _load_from_path(f)
            if role:
                roles[f.stem] = role
    if SKILLS_DIR.exists():
        for f in SKILLS_DIR.glob("*.md"):
            if f.stem == "system" or f.stem in roles:
                continue
            role = _load_from_path(f)
            if role:
                roles[f.stem] = role
    return roles


def get_roles_overview() -> str:
    """Get overview of all roles."""
    roles = get_all_roles()
    if not roles:
        return "No specialized roles available."
    return "\n".join(f"- {name}: {config.description}" for name, config in roles.items())
