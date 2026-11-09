import re
import threading
import logging
import time
import yaml
import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent / "skills"
AGENTS_DIR = SKILLS_DIR / "AGENTS"
CATALOG_PATH = Path("tmp/memory/skills_catalog.json")
CATALOG_TTL_SECONDS = 120

SKILL_FILE = "SKILL.md"
SUPPORT_DIRS = ("references", "templates", "assets", "scripts")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_DEPTH = 6
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", ".git"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _external_roots() -> List[Path]:
    try:
        from src.config import load_config
        roots = getattr(load_config(), "external_skill_roots", ["tmp"])
        return [Path(r).expanduser().resolve() for r in roots if str(r).strip()]
    except Exception:
        return [Path("tmp").resolve()]


def _parse_frontmatter(text: str) -> Optional[dict]:
    """Frontmatter of a skill file, or None when it is not a skill."""
    if not text.startswith("---"):
        return None
    try:
        end = text.index("---", 3)
        fm = yaml.safe_load(text[3:end]) or {}
    except Exception:
        return None
    if not isinstance(fm, dict) or not fm.get("name") or not fm.get("description"):
        return None
    tags = fm.get("tags", [])
    if isinstance(tags, str):
        fm["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        fm["tags"] = [str(t) for t in tags]
    else:
        fm["tags"] = []

    name = str(fm["name"]).strip()
    description = " ".join(str(fm["description"]).split())
    if len(name) > MAX_NAME or not _NAME_RE.match(name):
        logger.warning("Skill name rejected: %r", name)
        return None
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION]
    fm["name"], fm["description"] = name, description
    return fm


def _iter_skill_files(root: Path):
    """Yield skill files: a directory with SKILL.md is a leaf, never recursed."""
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        skill = directory / SKILL_FILE
        if skill.is_file():
            yield skill
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in SKIP_DIRS:
                continue
            if entry.is_dir() and not entry.is_symlink():
                stack.append((entry, depth + 1))
            elif depth == 0 and entry.suffix.lower() == ".md" and entry.name != "README.md":
                yield entry


def list_resources(path: Path) -> List[str]:
    """Bundled files a skill ships under its support directories."""
    base = Path(path).parent
    found = []
    for folder in SUPPORT_DIRS:
        area = base / folder
        if not area.is_dir():
            continue
        for item in sorted(area.rglob("*")):
            if item.is_file() and not item.is_symlink():
                found.append(str(item.relative_to(base)))
    return found[:60]


def _root_stamp(roots: List[Path]) -> str:
    """Cheap fingerprint so an unchanged tree is never rescanned."""
    parts = []
    for root in roots:
        try:
            parts.append(f"{root}:{root.stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{root}:missing")
    return "|".join(parts)


def rebuild_catalog(force: bool = False) -> dict:
    """Rescan every external root; first definition of a name wins."""
    roots = _external_roots()
    stamp = _root_stamp(roots)
    old: Dict[str, Any] = {}
    if CATALOG_PATH.exists():
        try:
            old = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    fresh = time.time() - float(old.get("_built") or 0) < CATALOG_TTL_SECONDS
    if not force and fresh and old.get("_stamp") == stamp and isinstance(old.get("skills"), dict):
        return old["skills"]

    catalog: Dict[str, Dict[str, Any]] = {}
    collisions = 0
    for root in roots:
        if not root.is_dir():
            continue
        for md in _iter_skill_files(root):
            try:
                fm = _parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if not fm:
                continue
            name = fm["name"]
            if name in catalog:
                collisions += 1
                continue
            tags = list(fm["tags"])
            if "favorite" in old.get(name, {}).get("tags", []) and "favorite" not in tags:
                tags.append("favorite")
            catalog[name] = {
                "path": str(md),
                "description": fm["description"],
                "tags": tags,
                "files": list_resources(md),
            }
    if collisions:
        logger.info("Skill catalog: %d duplicate name(s) ignored", collisions)
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps({"_stamp": stamp, "_built": time.time(), "skills": catalog}, indent=2)
    )
    return catalog


class SkillEngine:
    """Validate, grade, guide."""
    
    @staticmethod
    def validate_pre(skill: Any, args: Dict[str, Any]) -> Optional[str]:
        missing = [k for k, v in skill.pre.items() if k not in args or not args[k]]
        return f"PRE-FAIL: Missing {missing}" if missing else None

    @staticmethod
    def validate_post(skill: Any, result: Any) -> Optional[str]:
        if "required_keys" in skill.post:
            keys = skill.post["required_keys"]
            if isinstance(result, list) and result:
                if not all(k in result[0] for k in keys):
                    return f"POST-FAIL: Missing keys {keys}"
            elif isinstance(result, dict):
                if not all(k in result for k in keys):
                    return f"POST-FAIL: Missing keys {keys}"
        return None

    @staticmethod
    def build_context(skill: Any) -> str:
        ctx = f"--- CONTRACT ---\nPRE: {skill.pre}\nPOST: {skill.post}\n"
        if skill.procedure:
            ctx += "--- PROCEDURE ---\n" + "\n".join(f"- {s}" for s in skill.procedure) + "\n"
        return ctx

    @staticmethod
    async def grade(provider, task: str, output: str, procedure: list) -> float:
        if not procedure:
            return 5.0
        rubric = "\n".join(f"{i+1}. {s}" for i, s in enumerate(procedure))
        prompt = f"""Grade 1-5. First check the ORIGINAL TASK requirements one by one (met / unmet), then grade.
- Completeness vs original task + procedure
- Correctness of output
- Adherence to constraints
{f'Procedure:\n{rubric}' if rubric else ''}

ORIGINAL TASK: {task}
OUTPUT: {output}

Return ONLY:
CHECKLIST: <requirement>: <met|unmet>; ...
GRADE: [1-5]"""
        try:
            resp = await provider.generate_text([{"role":"user","content":prompt}], "Grade output.")
            m_grade = re.search(r"GRADE[:\s]*([1-5])", str(resp), re.IGNORECASE)
            grade = float(m_grade.group(1)) if m_grade else 3.0
            m_check = re.search(r"CHECKLIST:\s*(.+?)(?:\nGRADE:|$)", str(resp), re.DOTALL | re.IGNORECASE)
            if m_check:
                logger.info(f"Critic checklist: {m_check.group(1).strip()[:500]}")
            return grade
        except Exception:
            return 3.0


@dataclass
class SkillDef:
    """One skill: metadata eagerly, body and bundled files on demand."""
    name: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    pre: Dict[str, Any] = field(default_factory=dict)
    post: Dict[str, Any] = field(default_factory=dict)
    procedure: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    verification: str = "checklist"  # "checklist" or "heavy"
    body: str = ""
    path: str = ""
    files: List[str] = field(default_factory=list)
    external: bool = False

    @property
    def base_dir(self) -> str:
        """Directory the skill's relative paths resolve against."""
        return str(Path(self.path).parent) if self.path else ""

    def match_trigger(self, user_input: str) -> bool:
        if not self.tags:
            return False
        input_lower = user_input.lower()
        for tag in self.tags:
            pattern = r'\b' + re.escape(tag.lower()) + r'\b'
            if re.search(pattern, input_lower):
                return True
        return False
    
    @property
    def is_favorite(self) -> bool:
        return "favorite" in self.tags or not self.external

class SkillRegistry:
    """Loads YAML contracts, validates, topological dependency sort (GoS)."""
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
        self.skills: Dict[str, SkillDef] = {}
        self._load_all()
        self._load_external()   

    def _load_external(self) -> None:
        """External skills stay metadata-only until their body is requested."""
        for name, meta in rebuild_catalog().items():
            if name not in self.skills:
                self.skills[name] = SkillDef(
                    name=name,
                    description=meta["description"],
                    tags=meta["tags"],
                    body="",
                    path=meta["path"],
                    files=list(meta.get("files") or []),
                    external=True,
                )

    def _load_all(self):
        for md_file in AGENTS_DIR.glob("*.md"):
            self._load_file(md_file)
        for md_file in SKILLS_DIR.rglob("*.md"):
            if md_file.parent == AGENTS_DIR:
                continue
            if md_file.parent == SKILLS_DIR and md_file.stem == "system":
                continue
            self._load_file(md_file)

    def _load_file(self, path: Path):
        try:
            content = path.read_text(encoding="utf-8")
            if not content.startswith("---"):
                return
            parts = content.split("---", 2)
            if len(parts) < 3:
                return
            meta = yaml.safe_load(parts[1]) or {}
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            name = meta.get("name") or path.stem
            if not tags and not meta.get("name"):
                return
            contract = meta.get("contract", {})
            body = parts[2].strip()
            self.skills[name] = SkillDef(
                name=name,
                description=meta.get("description", ""),
                tags=tags,
                tools=meta.get("tools", []),
                pre=contract.get("pre", {}),
                post=contract.get("post", {}),
                procedure=meta.get("procedure", []),
                dependencies=meta.get("dependencies", []),
                verification=meta.get("verification", "checklist"),
                body=body,
                path=str(path),
                files=list_resources(path),
                external=False,
            )
        except Exception as e:
            logger.warning(f"Skill load failed: {path} -> {e}")

    def _external_enabled(self) -> bool:
        try:
            from src.config import load_config
            return bool(getattr(load_config(), "external_skills_enabled", False))
        except Exception:
            return False

    def resolve_dependencies(self, skill_name: str) -> List[str]:
        visited, order = set(), []
        def dfs(name: str):
            if name in visited:
                return
            visited.add(name)
            skill = self.skills.get(name)
            if skill:
                for dep in skill.dependencies:
                    dfs(dep)
                order.append(name)
        dfs(skill_name)
        return order

    def get(self, name: str) -> Optional[SkillDef]:
        """A skill, unless it is external and external skills are switched off."""
        skill = self.skills.get(name)
        if skill and skill.external and not self._external_enabled():
            return None
        return skill

    def is_external(self, name: str) -> bool:
        skill = self.skills.get(name)
        return bool(skill and skill.external)

    def visible(self) -> List[SkillDef]:
        """Skills the current configuration allows the model to see."""
        if self._external_enabled():
            return list(self.skills.values())
        return [skill for skill in self.skills.values() if not skill.external]

    def get_body(self, name: str) -> str:
        """The skill's instructions, read from disk the first time it is used."""
        skill = self.skills.get(name)
        if not skill:
            return f"Error: skill '{name}' not found."
        if skill.external and not self._external_enabled():
            return f"Error: external skills are disabled: '{name}'."
        if not skill.body and skill.path:
            try:
                text = Path(skill.path).read_text(encoding="utf-8", errors="replace")
                skill.body = text.split("---", 2)[2].strip() if text.startswith("---") else text
            except Exception as e:
                return f"Error: cannot read {skill.path}: {e}"
        return skill.body or "(empty)"

    def pin(self, name: str) -> str:
        """Mark an external skill as a favorite in the on-disk catalog."""
        skill = self.skills.get(name)
        if not skill or not skill.external:
            return f"Error: '{name}' is not an external skill."
        if "favorite" in skill.tags:
            return f"'{name}' is already a favorite."
        catalog = rebuild_catalog()
        entry = catalog.setdefault(name, {"path": skill.path, "description": skill.description, "tags": []})
        entry.setdefault("tags", []).append("favorite")
        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CATALOG_PATH.write_text(
            json.dumps(
                {"_stamp": _root_stamp(_external_roots()), "_built": time.time(), "skills": catalog},
                indent=2,
            )
        )
        skill.tags.append("favorite")
        return f"'{name}' pinned as favorite."

    def get_resource(self, name: str, relative: str) -> str:
        """Read one bundled file, confined to the skill's own directory."""
        skill = self.skills.get(name)
        if not skill or not skill.path:
            return f"Error: skill '{name}' has no bundled files."
        if skill.external and not self._external_enabled():
            return f"Error: external skills are disabled: '{name}'."
        base = Path(skill.path).parent.resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            return f"Error: '{relative}' escapes the skill directory."
        if not target.is_file():
            available = ", ".join(skill.files) or "none"
            return f"Error: no bundled file '{relative}'. Available: {available}."
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error: cannot read {relative}: {e}"

    def match_task(self, task: str) -> Optional[str]:
        task_lower = task.lower()
        for skill in self.visible():
            name = skill.name
            if name.lower() in task_lower:
                return name
            for dep in skill.dependencies:
                if dep in task_lower:
                    return name
        return None

    def find_by_tags(self, tags: List[str]) -> Optional[SkillDef]:
        tags_lower = [t.lower() for t in tags]
        patterns = [re.compile(r'\b' + re.escape(t) + r'\b') for t in tags_lower]
        for skill in self.visible():
            name_lower = skill.name.lower()
            if skill.name.lower() in tags_lower:
                return skill
            if any(p.search(name_lower) for p in patterns):
                return skill
            if any(p.search(tag.lower()) for tag in skill.tags for p in patterns):
                return skill
            if any(p.search(tool.lower()) for tool in skill.tools for p in patterns):
                return skill
        return None

    def reload(self):
        """Drop every skill and rescan, including the external catalog."""
        self.skills.clear()
        rebuild_catalog(force=True)
        self._load_all()
        self._load_external()

class RoleConfig:
    def __init__(self, name: str, content: str, meta: Dict):
        self.name = name
        self.prompt = content
        self.description = meta.get("description", "No description provided.")
        self.tools = meta.get("tools", [])


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


skill_index = SkillRegistry()
