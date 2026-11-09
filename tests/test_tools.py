import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.tools as tools
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


# --- filesystem confinement ---


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
