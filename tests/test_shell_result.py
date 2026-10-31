import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools import TOOL_REGISTRY, shell, shell_exec


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
