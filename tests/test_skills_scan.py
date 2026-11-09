import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.skills_loader as loader


def _skill(root: Path, name: str, body: str = "Body.", extra: str = "") -> Path:
    path = root / name / loader.SKILL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} skill\n{extra}---\n{body}", encoding="utf-8")
    return path


def test_skill_directory_is_a_leaf(tmp_path):
    leaf = _skill(tmp_path, "alpha")
    _skill(leaf.parent / "nested", "hidden")
    found = list(loader._iter_skill_files(tmp_path))
    assert found == [leaf]


def test_root_level_markdown_is_scanned_but_readme_is_not(tmp_path):
    top = tmp_path / "loose.md"
    top.write_text("---\nname: loose\ndescription: loose skill\n---\nbody", encoding="utf-8")
    (tmp_path / "README.md").write_text("---\nname: readme\ndescription: x\n---\n", encoding="utf-8")
    assert list(loader._iter_skill_files(tmp_path)) == [top]


def test_dot_dirs_and_noise_dirs_are_skipped(tmp_path):
    _skill(tmp_path / ".hidden", "h")
    _skill(tmp_path / "node_modules", "n")
    _skill(tmp_path / "ok", "ok")
    names = [path.parent.name for path in loader._iter_skill_files(tmp_path)]
    assert names == ["ok"]


def test_frontmatter_requires_name_and_description(tmp_path):
    good = tmp_path / "a.md"
    good.write_text("---\nname: good\ndescription: fine\n---\n", encoding="utf-8")
    bad = tmp_path / "b.md"
    bad.write_text("---\nname: bad\n---\n", encoding="utf-8")
    assert loader._parse_frontmatter(good.read_text())["name"] == "good"
    assert loader._parse_frontmatter(bad.read_text()) is None


def test_invalid_skill_names_are_rejected(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("---\nname: Bad Name\ndescription: x\n---\n", encoding="utf-8")
    assert loader._parse_frontmatter(path.read_text()) is None


def test_list_resources_enumerates_support_dirs(tmp_path):
    base = tmp_path / "skill"
    base.mkdir()
    (base / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n", encoding="utf-8")
    (base / "references").mkdir()
    (base / "references" / "guide.md").write_text("x", encoding="utf-8")
    (base / "scripts").mkdir()
    (base / "scripts" / "run.py").write_text("x", encoding="utf-8")
    (base / "other").mkdir()
    (base / "other" / "ignored.txt").write_text("x", encoding="utf-8")
    assert loader.list_resources(base / "SKILL.md") == ["references/guide.md", "scripts/run.py"]


def test_rebuild_catalog_prefers_first_definition(tmp_path, monkeypatch):
    roots = tmp_path / "one", tmp_path / "two"
    _skill(roots[0], "dupe")
    _skill(roots[1], "dupe")
    monkeypatch.setattr(loader, "_external_roots", lambda: list(roots))
    monkeypatch.setattr(loader, "CATALOG_PATH", tmp_path / "catalog.json")
    catalog = loader.rebuild_catalog(force=True)
    assert list(catalog) == ["dupe"]
    assert str(roots[0]) in catalog["dupe"]["path"]


def test_rebuild_catalog_caches_until_forced(tmp_path, monkeypatch):
    root = tmp_path / "root"
    _skill(root, "alpha")
    monkeypatch.setattr(loader, "_external_roots", lambda: [root])
    monkeypatch.setattr(loader, "CATALOG_PATH", tmp_path / "catalog.json")
    first = loader.rebuild_catalog(force=True)
    built = loader.CATALOG_PATH.stat().st_mtime_ns
    second = loader.rebuild_catalog()
    assert second == first
    assert loader.CATALOG_PATH.stat().st_mtime_ns == built
    loader.rebuild_catalog(force=True)
    assert loader.CATALOG_PATH.stat().st_mtime_ns != built


def test_get_resource_confines_reads_to_the_skill_dir(tmp_path, monkeypatch):
    root = tmp_path / "root"
    path = _skill(root, "alpha")
    (path.parent / "references").mkdir()
    (path.parent / "references" / "guide.md").write_text("inside", encoding="utf-8")
    monkeypatch.setattr(loader, "_external_roots", lambda: [root])
    monkeypatch.setattr(loader, "CATALOG_PATH", tmp_path / "catalog.json")
    monkeypatch.setattr(loader.SkillRegistry, "_external_enabled", lambda self: True)
    loader.SkillRegistry._instance = None
    registry = loader.SkillRegistry()
    assert registry.get_resource("alpha", "references/guide.md") == "inside"
    assert registry.get_resource("alpha", "../escape.md").startswith("Error:")
    assert registry.get_resource("alpha", "/etc/passwd").startswith("Error:")

def _registry(tmp_path, monkeypatch, skills):
    root = tmp_path / "root"
    for name, extra in skills.items():
        _skill(root / "software-development", name, extra=extra)
    internal = tmp_path / "internal"
    internal.mkdir()
    monkeypatch.setattr(loader, "SKILLS_DIR", internal)
    monkeypatch.setattr(loader, "_external_roots", lambda: [root])
    monkeypatch.setattr(loader, "CATALOG_PATH", tmp_path / "catalog.json")
    loader.SkillRegistry._instance = None
    registry = loader.SkillRegistry()
    monkeypatch.setattr(registry, "_external_enabled", lambda: True)
    return registry


def test_frontmatter_reads_namespaced_topology(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, {
        "alpha": "metadata:\n  hermes:\n    tags: [one, two]\n"
                 "    category: software-development\n"
                 "    related_skills: [beta]\n",
    })
    skill = registry.skills["alpha"]
    assert skill.tags == ["one", "two"]
    assert skill.category == "software-development"
    assert skill.related == ["beta"]


def test_index_groups_by_category_and_marks_matches(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, {"alpha": "", "beta": ""})
    text = registry.index(desc_chars=40, matched={"alpha"})
    assert "software-development:" in text
    assert "[match]" in text
    assert text.index("alpha") < text.index("beta")


def test_index_degrades_by_dropping_descriptions_not_entries(tmp_path, monkeypatch):
    long = "word " * 60
    names = [f"alpha{index}" for index in range(10)]
    registry = _registry(tmp_path, monkeypatch, {name: f"description: {long}\n" for name in names})
    text = registry.index(budget_chars=400, desc_chars=200)
    assert all(name in text for name in names)
    assert "word word" not in text
    assert len(text) <= 400


def test_related_is_a_bounded_closure(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, {
        "alpha": "metadata:\n  hermes:\n    related_skills: [beta]\n",
        "beta": "metadata:\n  hermes:\n    related_skills: [gamma]\n",
        "gamma": "metadata:\n  hermes:\n    related_skills: [alpha]\n",
    })
    assert registry.related("alpha", depth=1) == ["beta"]
    assert registry.related("alpha", depth=5) == ["beta", "gamma"]


def test_search_ranks_name_then_tags(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, {
        "alpha": "metadata:\n  hermes:\n    tags: [deployment]\n",
        "beta": "metadata:\n  hermes:\n    tags: [alpha, other]\n",
    })
    assert [skill.name for skill in registry.search(["alpha"])] == ["alpha", "beta"]


def test_bundled_skill_md_loads_with_resources_and_category(tmp_path, monkeypatch):
    root = tmp_path / "internal"
    bundle = root / "software-development" / "review"
    (bundle / "references").mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\nname: review\ndescription: review code\nmetadata:\n  tags: [quality]\n---\nBody.\n",
        encoding="utf-8",
    )
    (bundle / "references" / "checklist.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(loader, "SKILLS_DIR", root)
    monkeypatch.setattr(loader, "AGENTS_DIR", root / "AGENTS")
    monkeypatch.setattr(loader, "_external_roots", lambda: [])
    monkeypatch.setattr(loader, "CATALOG_PATH", tmp_path / "catalog.json")
    loader.SkillRegistry._instance = None
    registry = loader.SkillRegistry()
    skill = registry.get("review")
    assert skill is not None
    assert skill.category == "software-development"
    assert skill.files == ["references/checklist.md"]
    assert registry.categories()["software-development"][0].name == "review"


def test_builtin_roles_all_load():
    roles = loader.get_all_roles()
    assert set(roles) >= {"system", "code", "architect", "researcher"}
    assert all(role.prompt.strip() for role in roles.values())


def test_builtin_skills_load_with_bodies():
    loader.SkillRegistry._instance = None
    try:
        registry = loader.SkillRegistry()
        names = {skill.name for skill in registry.visible()}
        assert {"systematic-debugging", "test-driven-development", "pdf"} <= names
        assert registry.get_body("test-driven-development").strip()
    finally:
        loader.SkillRegistry._instance = None
