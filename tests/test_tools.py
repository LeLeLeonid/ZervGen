import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.skills_loader as loader
import src.tools as tools
from src.config import GlobalSettings, MCPServerConfig
from src.core.mcp_manager import MCPServer, MCPManager, _expand
from src.tools import TOOL_REGISTRY, shell, shell_exec
from src.utils import GitManager


def run(cmd, timeout=60):
    return asyncio.run(shell_exec(cmd, timeout))


def test_success_is_ok_with_zero_exit():
    r = run("echo hello")
    assert r.ok is True
    assert r.exit_code == 0
    assert "hello" in r.stdout


def test_nonzero_exit_is_not_ok():
    r = run("exit 3")
    assert r.ok is False
    assert r.exit_code == 3


def test_failing_command_is_not_ok_even_though_text_lacks_Error():
    r = run("echo boom >&2; exit 1")
    assert r.ok is False
    assert "boom" in r.stderr
    assert not r.to_text().startswith("Error")


def test_success_stdout_starting_with_Error_is_ok():
    r = run("echo 'Error: this is legit output'")
    assert r.ok is True


def test_blocked_command_is_not_ok():
    r = run("rm -rf /")
    assert r.ok is False
    assert r.blocked is True


def test_timeout_is_not_ok():
    r = run("sleep 5", timeout=1)
    assert r.ok is False
    assert r.timed_out is True


def test_shell_still_returns_str():
    out = asyncio.run(shell("echo hi"))
    assert isinstance(out, str)
    assert "hi" in out


def test_shell_exec_not_exposed_to_model():
    assert "shell_exec" not in TOOL_REGISTRY
    assert "shell" in TOOL_REGISTRY
    assert "render_tool_index" not in TOOL_REGISTRY
    assert "get_tools_schema" not in TOOL_REGISTRY

def test_public_tool_names_are_short():
    assert {"read", "write", "edit", "append", "ls", "glob", "grep", "shell", "web"} <= set(TOOL_REGISTRY)
    assert not {"read_file", "write_file", "edit_file", "get_weather", "web_search"} & set(TOOL_REGISTRY)




def _root(monkeypatch, path):
    monkeypatch.setattr(tools, "_ALLOWED_ROOTS", [Path(path).resolve()])


def test_list_files_default_and_recursive(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.txt").write_text("beta")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_text("x")

    flat = asyncio.run(tools.list_files("."))
    assert "a.txt" in flat and "pkg" in flat
    assert "__pycache__" not in flat

    with_cache = asyncio.run(tools.list_files(".", ignore_dir=False))
    assert "__pycache__" in with_cache

    recursive = asyncio.run(tools.list_files(".", recursive=True))
    assert "pkg/b.txt" in recursive


def test_symlink_to_outside_is_skipped(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _root(monkeypatch, root)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret needle")
    (root / "inside.txt").write_text("needle inside")
    os.symlink(outside, root / "link.txt")

    grep = asyncio.run(tools.grep_files("needle", str(root)))
    assert "inside.txt" in grep
    assert "link.txt" not in grep

    globbed = asyncio.run(tools.glob_files("*", str(root)))
    assert "inside.txt" in globbed
    assert "link.txt" not in globbed

    listed = asyncio.run(tools.list_files(str(root)))
    assert "link.txt" not in listed


def test_read_outside_root_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _root(monkeypatch, root)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    assert "denied" in asyncio.run(tools.read_file(str(outside))).lower()


def test_write_outside_root_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _root(monkeypatch, root)

    result = asyncio.run(tools.write_file(str(tmp_path / "escape.txt"), "x"))
    assert "denied" in result.lower()
    assert not (tmp_path / "escape.txt").exists()


def test_write_refuses_to_clobber_an_unread_file(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    target = tmp_path / "exists.txt"
    target.write_text("original")

    refused = asyncio.run(tools.write_file(str(target), "new"))
    assert "read it first" in refused
    assert target.read_text() == "original"

    asyncio.run(tools.read_file(str(target)))
    assert asyncio.run(tools.write_file(str(target), "new")) == f"Written to {target}"
    assert target.read_text() == "new"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def test_prune_never_resets_the_work_tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "tracked.txt").write_text("committed")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")

    manager = GitManager(work, max_snapshots=1)
    (work / "tracked.txt").write_text("first edit")
    manager.snapshot("s1")
    (work / "tracked.txt").write_text("second edit")
    manager.snapshot("s2")
    (work / "tracked.txt").write_text("uncommitted work")

    manager._prune()

    assert (work / "tracked.txt").read_text() == "uncommitted work"


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
        assert {"file-ops", "web", "weather", "llm-wiki", "mcp", "skill-creator",
                "github", "memory", "testing", "delegation", "media", "compute", "provider"} <= names
        assert registry.get_body("file-ops").strip()
    finally:
        loader.SkillRegistry._instance = None


def test_role_path_refuses_traversal():
    assert loader._role_path("../system") is None
    assert loader._role_path("") is None
    assert loader._role_path("system") is not None


def test_post_validation_reads_json_strings():
    from types import SimpleNamespace

    skill = SimpleNamespace(post={"required_keys": ["name", "count"]})
    assert loader.SkillEngine.validate_post(skill, '{"name": "x", "count": 2}') is None
    assert "missing keys" in loader.SkillEngine.validate_post(skill, '{"name": "x"}')
    assert "prose" in loader.SkillEngine.validate_post(skill, "not json")
    assert loader.SkillEngine.validate_post(SimpleNamespace(post={}), "anything") is None


def test_pre_validation_checks_declared_tools_and_files(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)
    skill = SimpleNamespace(pre={"tools": ["shell"], "files": ["pytest.ini"]})
    assert "tool shell" in loader.SkillEngine.validate_pre(skill, "", {"read"})
    assert "file pytest.ini" in loader.SkillEngine.validate_pre(skill, "", {"shell"})
    (tmp_path / "pytest.ini").write_text("")
    assert loader.SkillEngine.validate_pre(skill, "", {"shell"}) is None
    assert loader.SkillEngine.validate_pre(SimpleNamespace(pre={}), "", set()) is None


def _server(name: str = "demo", **kwargs) -> MCPServer:
    return MCPServer(name, MCPServerConfig(**kwargs))


def test_env_reference_expands_from_environment(monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", "abc123")
    assert _expand("Bearer ${DEMO_TOKEN}", "headers") == "Bearer abc123"


def test_unset_env_reference_is_a_hard_error(monkeypatch):
    monkeypatch.delenv("DEMO_MISSING", raising=False)
    with pytest.raises(RuntimeError, match="DEMO_MISSING"):
        _expand("${DEMO_MISSING}", "demo.env")


def test_kind_infers_transport_from_fields():
    assert MCPServerConfig(command="npx", args=["-y", "pkg"]).kind == "stdio"
    assert MCPServerConfig(url="https://example.test/mcp").kind == "http"
    assert MCPServerConfig(url="https://example.test/sse").kind == "sse"
    assert MCPServerConfig(url="https://example.test/mcp", transport="sse").kind == "sse"
    assert MCPServerConfig(command="x", transport="http", url="https://example.test/x").kind == "http"


def test_tool_filters_admit_and_reject():
    server = _server(command="x", tools_include=["git_*"], tools_exclude=["git_push"])
    assert server._wanted("git_status") is True
    assert server._wanted("read_file") is False
    assert server._wanted("git_push") is False


def test_public_names_are_namespaced_by_server():
    manager = MCPManager(GlobalSettings())
    assert manager._public_name("alpha", "search") == "mcp__alpha__search"


def test_public_name_clamps_to_64_chars():
    manager = MCPManager(GlobalSettings())
    public = manager._public_name("server", "t" * 120)
    assert len(public) <= 64


def test_public_name_sanitizes_unsafe_characters():
    manager = MCPManager(GlobalSettings())
    assert manager._public_name("my server", "do/it") == "mcp__my_server__do_it"


def test_reindex_namespaces_every_tool_and_keeps_a_unique_alias():
    manager = MCPManager(GlobalSettings())
    first = _server("alpha", command="a")
    second = _server("beta", command="b")
    for server in (first, second):
        server._connected = True
        server.tools = {"search": object()}
    first.tools["read"] = object()
    manager.servers = {"alpha": first, "beta": second}
    manager._reindex()
    assert manager._calls["mcp__alpha__search"] == ("alpha", "search")
    assert manager._calls["mcp__beta__search"] == ("beta", "search")
    assert manager._calls["mcp__alpha__read"] == ("alpha", "read")
    assert "mcp_search" not in manager._calls
    assert manager._calls["mcp_read"] == ("alpha", "read")
    assert manager.tools_map["mcp__alpha__search"] == "alpha"


def test_reindex_skips_disconnected_servers():
    manager = MCPManager(GlobalSettings())
    server = _server(command="a")
    server._connected = False
    server.tools = {"search": object()}
    manager.servers = {"alpha": server}
    manager._reindex()
    assert manager.tools_map == {}


def test_failed_server_keeps_its_catalog_and_backs_off():
    from src.core.mcp_manager import BACKOFF_START

    server = _server(command="a")
    server.tools = {"search": object()}
    server._record_failure()
    assert server.state == "degraded"
    assert server.in_backoff
    assert 0 < server.retry_in <= BACKOFF_START
    server._record_failure()
    assert server.retry_in > BACKOFF_START
    assert server.tools, "catalog must survive a failed reconnect"


def test_server_status_reports_state_and_retry():
    manager = MCPManager(GlobalSettings())
    server = _server(command="a")
    manager.servers = {"alpha": server}
    server._record_failure()
    status = manager.server_status()["alpha"]
    assert status["state"] == "degraded"
    assert status["connected"] is False
    assert status["retry_in"] > 0


def test_degraded_server_keeps_serving_its_cached_names():
    manager = MCPManager(GlobalSettings())
    server = _server("alpha", command="a")
    server._connected = True
    server.tools = {"search": object()}
    manager.servers = {"alpha": server}
    manager._reindex()
    server._record_failure()
    manager._reindex()
    assert manager._calls["mcp__alpha__search"] == ("alpha", "search")


def test_agent_connects_servers_on_first_turn(tmp_path, monkeypatch):
    import asyncio

    from src.core.base_agent import StemAgent

    monkeypatch.chdir(tmp_path)
    settings = GlobalSettings()
    settings.memory_enabled = False
    settings.mcp_servers = {"alpha": MCPServerConfig(command="a", enabled=True)}
    agent = StemAgent(name="system", settings=settings, mode="BUILD")
    calls = []

    async def fake_connect():
        calls.append(1)
        return {}

    monkeypatch.setattr(MCPManager, "connect_all", lambda self: fake_connect())
    asyncio.run(agent._init_mcp())
    assert calls == [1]
    agent._mcp_initialized = False
    agent._mcp = None
    MCPManager._instance = None


def test_agent_skips_connect_when_disabled(tmp_path, monkeypatch):
    import asyncio

    from src.core.base_agent import StemAgent

    monkeypatch.chdir(tmp_path)
    settings = GlobalSettings()
    settings.memory_enabled = False
    settings.mcp_connect_on_start = False
    settings.mcp_servers = {"alpha": MCPServerConfig(command="a", enabled=True)}
    agent = StemAgent(name="system", settings=settings, mode="BUILD")
    called = []
    monkeypatch.setattr(MCPManager, "connect_all", lambda self: called.append(1))
    asyncio.run(agent._init_mcp())
    assert called == []
    MCPManager._instance = None
