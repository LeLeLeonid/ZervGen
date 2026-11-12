import os
import re
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
TOOL_ALIASES = {
    "terminal": "shell",
    "web_extract": "fetch",
    "sh": "shell",
    "delegate_task": "delegate",
    "read_file": "read",
    "write_file": "write",
    "append_file": "append",
    "edit_file": "edit",
    "list_files": "ls",
    "glob_files": "glob",
    "grep_files": "grep",
    "search_memory": "recall",
    "add_memory": "remember",
    "load_skill": "skill",
}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _external_roots() -> List[Path]:
    try:
        from src.config import load_config
        roots = getattr(load_config(), "external_skill_roots", ["tmp"])
        return [Path(r).expanduser().resolve() for r in roots if str(r).strip()]
    except Exception:
        return [Path("tmp").resolve()]


def _as_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _meta(fm: dict) -> dict:
    merged: Dict[str, Any] = {}
    for holder in ("metadata", "meta"):
        nested = fm.get(holder)
        if not isinstance(nested, dict):
            continue
        for key, value in nested.items():
            if isinstance(value, dict):
                merged.update(value)
            else:
                merged[key] = value
    merged.update({k: v for k, v in fm.items() if k not in ("metadata", "meta")})
    return merged


def _contract(raw: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"pre": {}, "post": {}}
    if not isinstance(raw, dict):
        return out
    for key in ("pre", "post"):
        value = raw.get(key)
        if isinstance(value, dict):
            out[key] = {str(k): v for k, v in value.items()}
        elif isinstance(value, (list, tuple, str)):
            items = _as_list(value)
            out[key] = {"required_keys": items} if key == "post" else {item: True for item in items}
    return out


def _parse_frontmatter(text: str) -> Optional[dict]:
    if not text.startswith("---"):
        return None
    match = re.search(r"^---\s*$", text[3:], re.MULTILINE)
    if not match:
        return None
    try:
        raw = yaml.safe_load(text[3:3 + match.start()]) or {}
    except Exception:
        return None
    if not isinstance(raw, dict) or not raw.get("name") or not raw.get("description"):
        return None
    flat = _meta(raw)
    fm = dict(raw)
    fm["tags"] = _as_list(flat.get("tags") or flat.get("keywords"))
    fm["related"] = _as_list(flat.get("related_skills") or flat.get("related"))
    when_not = flat.get("when_not") or flat.get("when-not") or ""
    fm["when_not"] = " ".join(str(when_not).split()) if when_not else ""
    fm["category"] = str(flat.get("category") or "").strip().lower()
    fm["version"] = str(flat.get("version") or "").strip()
    fm["license"] = str(flat.get("license") or "").strip()
    fm["tools"] = _as_list(flat.get("tools") or flat.get("allowed-tools") or flat.get("allowed_tools"))
    fm["contract"] = _contract(flat.get("contract"))
    fm["procedure"] = _as_list(flat.get("procedure"))
    fm["dependencies"] = _as_list(flat.get("dependencies"))
    verification = str(flat.get("verification") or "checklist").strip().lower()
    fm["verification"] = verification if verification in ("checklist", "heavy") else "checklist"

    name = str(fm["name"]).strip()
    description = " ".join(str(fm["description"]).split())
    if len(name) > MAX_NAME or not _NAME_RE.match(name):
        logger.warning("Skill name rejected: %r", name)
        return None
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION]
    fm["name"], fm["description"] = name, description
    return fm


SKILL_FIELDS = {
    "name", "description", "tags", "keywords", "tools", "allowed-tools",
    "contract", "procedure", "dependencies", "verification", "version",
    "license", "related", "related_skills", "category", "metadata", "meta",
    "when_not", "when-not",
}


def validate_skill(fm: dict, path: Path) -> List[str]:
    problems: List[str] = []
    expected = path.parent.name if path.name == SKILL_FILE else path.stem
    if fm.get("name") != expected:
        problems.append(f"name '{fm.get('name')}' must match '{expected}'")
    if not fm.get("tags"):
        problems.append("tags is empty")
    if not isinstance(fm.get("tools"), list):
        problems.append("tools must be a list")
    if not isinstance(fm.get("related"), list):
        problems.append("related must be a list")
    contract = fm.get("contract") or {}
    for key in ("pre", "post"):
        if not isinstance(contract.get(key), dict):
            problems.append(f"contract.{key} must be a mapping")
    if not isinstance(fm.get("procedure"), list):
        problems.append("procedure must be a list")
    if not isinstance(fm.get("dependencies"), list):
        problems.append("dependencies must be a list")
    if fm.get("verification") not in ("checklist", "heavy"):
        problems.append("verification must be checklist or heavy")
    unknown = sorted(set(fm) - SKILL_FIELDS)
    if unknown:
        problems.append(f"unknown fields: {', '.join(unknown)}")
    return problems


def _category(path: Path, root: Optional[Path] = None) -> str:
    base = Path(root).resolve() if root else SKILLS_DIR.resolve()
    try:
        parts = Path(path).resolve().relative_to(base).parts
    except ValueError:
        return "general"
    if len(parts) >= 2:
        return parts[0]
    return "general"


def _iter_skill_files(root: Path):
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
    base = Path(path).parent
    root = base.resolve()
    found = []
    for folder in SUPPORT_DIRS:
        area = base / folder
        if not area.is_dir():
            continue
        for item in sorted(area.rglob("*")):
            try:
                relative = item.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts):
                continue
            if item.is_file() and not item.is_symlink():
                found.append(str(relative))
    return found[:60]


def _root_stamp(roots: List[Path]) -> str:
    parts = []
    for root in roots:
        try:
            parts.append(f"{root}:{root.stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{root}:missing")
    return "|".join(parts)


def rebuild_catalog(force: bool = False) -> dict:
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
            previous = (old.get("skills") or {}).get(name) or {}
            if "favorite" in (previous.get("tags") or []) and "favorite" not in tags:
                tags.append("favorite")
            catalog[name] = {
                "path": str(md),
                "description": fm["description"],
                "tags": tags,
                "files": list_resources(md),
                "category": (fm["category"] or _category(md, root)).lower(),
                "related": fm["related"],
            }
    if collisions:
        logger.info("Skill catalog: %d duplicate name(s) ignored", collisions)
    _write_catalog({"_stamp": stamp, "_built": time.time(), "skills": catalog})
    return catalog


def _write_catalog(payload: dict) -> None:
    """Best-effort atomic catalog write; never fatal, never leaves a half file."""
    try:
        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = CATALOG_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp, CATALOG_PATH)
    except OSError as exc:
        logger.debug("Skill catalog not persisted: %s", exc)


class SkillEngine:

    @staticmethod
    def validate_pre(skill: Any, task: str = "", active_tools: Optional[set] = None) -> Optional[str]:
        """Check the requirements a skill declares it cannot run without."""
        pre = skill.pre or {}
        missing = []
        if pre.get("tools") and active_tools is not None:
            missing += [f"tool {name}" for name in pre["tools"] if name not in active_tools]
        if pre.get("files"):
            missing += [f"file {path}" for path in pre["files"] if not Path(path).exists()]
        return f"PRE-FAIL: missing {', '.join(missing)}" if missing else None

    @staticmethod
    def validate_post(skill: Any, result: Any) -> Optional[str]:
        """Check the output shape a skill declares it must return."""
        if "required_keys" not in (skill.post or {}):
            return None
        keys = skill.post["required_keys"]
        data = result
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return "POST-FAIL: result is prose, expected an object with " + ", ".join(keys)
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict):
            missing = [k for k in keys if k not in data]
            if missing:
                return f"POST-FAIL: missing keys {missing}"
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
    name: str
    description: str = ""
    when_not: str = ""
    tags: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    pre: Dict[str, Any] = field(default_factory=dict)
    post: Dict[str, Any] = field(default_factory=dict)
    procedure: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    verification: str = "checklist"
    body: str = ""
    path: str = ""
    files: List[str] = field(default_factory=list)
    external: bool = False
    category: str = ""
    related: List[str] = field(default_factory=list)
    version: str = ""
    license: str = ""

    @property
    def base_dir(self) -> str:
        return str(Path(self.path).parent) if self.path else ""

    def match_trigger(self, user_input: str) -> bool:
        if not self.tags:
            return False
        words = set(re.findall(r"[a-z0-9]+", (user_input or "").lower()))
        if not words:
            return False
        for tag in self.tags:
            for word in re.findall(r"[a-z0-9]+", tag.lower()):
                if word in words:
                    return True
                if len(word) >= 4 and any(other.startswith(word) or word.startswith(other)
                                          for other in words if len(other) >= 4):
                    return True
        return False

    def suppressed(self, user_input: str) -> bool:
        """True when the request hits this skill's explicit negative trigger."""
        if not self.when_not:
            return False
        words = set(re.findall(r"[a-z0-9]+", (user_input or "").lower()))
        hits = set(re.findall(r"[a-z0-9]+", self.when_not.lower())) & words
        return len(hits) >= 2

    @property
    def is_favorite(self) -> bool:
        return "favorite" in self.tags or not self.external

class SkillRegistry:
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
        for name, meta in rebuild_catalog().items():
            if name not in self.skills:
                self.skills[name] = SkillDef(
                    name=name,
                    description=meta["description"],
                    when_not=str(meta.get("when_not") or ""),
                    tags=meta["tags"],
                    body="",
                    path=meta["path"],
                    files=list(meta.get("files") or []),
                    external=True,
                    category=meta.get("category", ""),
                    related=list(meta.get("related") or []),
                )

    def _load_all(self):
        for md_file in sorted(SKILLS_DIR.rglob("*.md")):
            relative = md_file.relative_to(SKILLS_DIR)
            if md_file.name != SKILL_FILE and len(relative.parts) != 2:
                continue
            self._load_file(md_file)

    def _load_file(self, path: Path):
        try:
            fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
            if not fm:
                return
            problems = validate_skill(fm, path)
            if problems:
                logger.warning("Skill %s rejected: %s", path, "; ".join(problems))
                return
            contract = fm["contract"]
            self.skills[fm["name"]] = SkillDef(
                name=fm["name"],
                description=fm["description"],
                when_not=fm.get("when_not", ""),
                tags=fm["tags"],
                tools=fm["tools"],
                pre=contract["pre"],
                post=contract["post"],
                procedure=fm["procedure"],
                dependencies=fm["dependencies"],
                verification=fm["verification"],
                body="",
                path=str(path),
                files=list_resources(path),
                category=fm["category"] or _category(path),
                related=fm["related"],
                version=fm["version"],
                license=fm["license"],
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
        skill = self.skills.get(name)
        if skill and skill.external and not self._external_enabled():
            return None
        return skill

    def is_external(self, name: str) -> bool:
        skill = self.skills.get(name)
        return bool(skill and skill.external)

    def visible(self) -> List[SkillDef]:
        if self._external_enabled():
            return list(self.skills.values())
        return [skill for skill in self.skills.values() if not skill.external]

    def categories(self) -> Dict[str, List[SkillDef]]:
        grouped: Dict[str, List[SkillDef]] = {}
        for skill in self.visible():
            grouped.setdefault(skill.category or "general", []).append(skill)
        for group in grouped.values():
            group.sort(key=lambda s: s.name)
        return grouped

    def related(self, name: str, depth: int = 1) -> List[str]:
        seen = {name}
        order: List[str] = []
        frontier = [name]
        for _ in range(max(0, depth)):
            following = []
            for item in frontier:
                skill = self.skills.get(item)
                if not skill:
                    continue
                for neighbour in skill.related:
                    if neighbour in seen:
                        continue
                    seen.add(neighbour)
                    order.append(neighbour)
                    following.append(neighbour)
            frontier = [item for item in following if item in self.skills]
        return [item for item in order if item in self.skills]

    def search(self, tags: list, limit: int = 5) -> List["SkillDef"]:
        wanted = set()
        for tag in tags:
            words = re.findall(r"[a-z0-9]+", str(tag).lower())
            wanted.update(words)
            wanted.add("".join(words))
        if not wanted:
            return []
        ranked = []
        for skill in self.visible():
            name_words = set(re.findall(r"[a-z0-9]+", skill.name.lower()))
            tag_words = set()
            for tag in skill.tags:
                words = re.findall(r"[a-z0-9]+", tag.lower())
                tag_words.update(words)
                tag_words.add("".join(words))
            desc_words = set(re.findall(r"[a-z0-9]+", skill.description.lower()))
            score = 4 * len(wanted & name_words) + 3 * len(wanted & tag_words) + len(wanted & desc_words)
            score += sum(1 for word in wanted if len(word) >= 4 and any(
                other.startswith(word) for other in name_words | tag_words))
            if score:
                ranked.append((score, skill.name, skill))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [skill for _, _, skill in ranked[:max(1, limit)]]

    @staticmethod
    def _render_index(skills: List[SkillDef], desc_chars: int, matched: set) -> str:
        lines, category = [], None
        for skill in skills:
            group = skill.category or "general"
            if group != category:
                category = group
                lines.append(f"{group}:")
            entry = f"  - {skill.name}"
            if desc_chars and skill.description:
                entry += ": " + skill.description[:desc_chars]
            if desc_chars and skill.when_not:
                entry += f" (not when: {skill.when_not[:desc_chars]})"
            if skill.name in matched:
                entry += "  [match]"
            lines.append(entry)
        return "\n".join(lines)

    def index(self, budget_chars: int = 7000, desc_chars: int = 90,
              max_skills: int = 150, matched: Optional[set] = None,
              skills: Optional[List[SkillDef]] = None) -> str:
        matched = set(matched or ())
        pool = sorted(skills if skills is not None else self.visible(),
                      key=lambda s: (s.category or "general", s.name))
        if not pool:
            return ""
        head = ("--- SKILL INDEX ---\n"
                "Load a skill with skill(name) when its description matches the task.")
        budget = max(400, int(budget_chars))
        shown = pool[:max(1, int(max_skills))]
        omitted = len(pool) - len(shown)
        tail = f"\n({omitted} more skills omitted; use find(tags))" if omitted else ""
        if matched:
            tail += "\n[match] = relevant to this request; load it before acting."
        for width in (desc_chars, 40, 0):
            text = f"{head}\n{self._render_index(shown, width, matched)}{tail}"
            if len(text) <= budget:
                return text
        while shown and len(head) + len(tail) + len(self._render_index(shown, 0, matched)) > budget:
            shown = shown[:-1]
        if not shown:
            return ""
        return f"{head}\n{self._render_index(shown, 0, matched)}{tail}"

    def get_body(self, name: str) -> str:
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
        body = skill.body or "(empty)"
        head = []
        if skill.dependencies:
            head.append("Depends on: " + ", ".join(skill.dependencies)
                        + ". Load each with skill(name) before using this one.")
        if skill.procedure:
            head.append("Procedure:\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(skill.procedure, 1)))
        return "\n\n".join(head + [body])

    def pin(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill or not skill.external:
            return f"Error: '{name}' is not an external skill."
        if "favorite" in skill.tags:
            return f"'{name}' is already a favorite."
        catalog = rebuild_catalog()
        entry = catalog.setdefault(name, {"path": skill.path, "description": skill.description, "tags": []})
        tags = entry.setdefault("tags", [])
        if "favorite" not in tags:
            tags.append("favorite")
        _write_catalog({"_stamp": _root_stamp(_external_roots()), "_built": time.time(), "skills": catalog})
        skill.tags.append("favorite")
        return f"'{name}' pinned as favorite."

    def aliases(self, name: str) -> List[tuple]:
        skill = self.skills.get(name)
        if not skill or not skill.path:
            return []
        try:
            body = Path(skill.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [(foreign, local) for foreign, local in TOOL_ALIASES.items()
                if re.search(r"\b" + re.escape(foreign) + r"\b", body)]

    def get_resource(self, name: str, relative: str) -> str:
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
        matches = self.search(tags, limit=1)
        return matches[0] if matches else None

    def reload(self):
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
        fm = _parse_frontmatter(raw)
        if not fm:
            logger.warning("Role %s rejected: missing or invalid frontmatter", path)
            return None
        if path.parent == AGENTS_DIR:
            problems = validate_skill(fm, path)
            if problems:
                logger.warning("Role %s rejected: %s", path, "; ".join(problems))
                return None
        return RoleConfig(path.stem, raw.split("---", 2)[2].strip(), fm)
    except Exception:
        return None


def _role_path(name: str) -> Optional[Path]:
    name = str(name or "").strip()
    if not name or len(name) > MAX_NAME or not _NAME_RE.match(name):
        return None
    for base in (AGENTS_DIR, SKILLS_DIR):
        candidate = (base / f"{name}.md").resolve()
        if base.resolve() not in candidate.parents:
            continue
        if candidate.is_file():
            return candidate
    return None


def load_role(name: str) -> Optional[RoleConfig]:
    path = _role_path(name)
    return _load_from_path(path) if path else None


def role_exists(name: str) -> bool:
    return _role_path(name) is not None


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
