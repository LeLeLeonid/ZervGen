from pathlib import Path
from typing import Dict, Optional

SKILLS_DIR = Path(__file__).parent / "skills"

def load_skill(skill_name: str) -> Optional[str]:
    """Load a skill definition by name"""
    if not SKILLS_DIR.exists():
        return None
    
    skill_file = SKILLS_DIR / f"{skill_name}.md"
    if skill_file.exists():
        with open(skill_file, "r", encoding="utf-8") as f:
            return f.read()
    return None

def get_all_skills() -> Dict[str, str]:
    """Get all available skills"""
    skills = {}
    if not SKILLS_DIR.exists():
        return skills
    
    for skill_file in SKILLS_DIR.glob("*.md"):
        name = skill_file.stem
        skills[name] = load_skill(name)
    return skills

def get_skills_schema() -> str:
    """Get formatted schema of all skills"""
    skills = get_all_skills()
    if not skills:
        return "No skills available."
    
    schema = ["# AVAILABLE SKILLS\n"]
    for name, content in skills.items():
        # Extract first line as description
        lines = content.strip().split('\n')
        description = lines[0] if lines else "No description"
        schema.append(f"- {name}: {description}")
    
    return "\n".join(schema)