import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.runtime import PermissionPolicy, ToolResult, ToolRuntime


async def _echo(text: str = "") -> str:
    return text


async def _accept_path(path: str = "") -> str:
    return path


async def _accept_command(command: str = "") -> str:
    return command


async def _boom() -> str:
    raise RuntimeError("kaboom")


async def _slow() -> str:
    await asyncio.sleep(5)
    return "late"


def _runtime(tmp_path, tools, policy=None, **kwargs):
    return ToolRuntime(
        tools=tools,
        policy=policy or PermissionPolicy(permission_mode="AUTO", mode="BUILD"),
        spill_dir=tmp_path,
        **kwargs,
    )


def test_missing_tool_returns_error_result(tmp_path):
    runtime = _runtime(tmp_path, {})
    result = asyncio.run(runtime.execute("nope", {}))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "not found" in result.error


def test_successful_call_records_one_result(tmp_path):
    run_calls = []
    runtime = _runtime(tmp_path, {"echo": _echo}, on_event=lambda kind, **kw: run_calls.append(kind))
    result = asyncio.run(runtime.execute("echo", {"text": "hi"}))
    assert result.success is True
    assert result.output == "hi"
    assert run_calls == ["tool_call", "tool_result"]


def test_sync_tool_runs_off_loop(tmp_path):
    import time

    def blocking(text: str = "") -> str:
        time.sleep(0.05)
        return text

    runtime = _runtime(tmp_path, {"blocking": blocking})
    result = asyncio.run(runtime.execute("blocking", {"text": "done"}))
    assert result.success is True
    assert result.output == "done"


def test_timeout_returns_error_result(tmp_path):
    runtime = _runtime(tmp_path, {"slow": _slow})
    result = asyncio.run(runtime.execute("slow", {}, timeout=0.05))
    assert result.success is False
    assert "timed out" in result.error


def test_exception_becomes_error_result(tmp_path):
    runtime = _runtime(tmp_path, {"boom": _boom})
    result = asyncio.run(runtime.execute("boom", {}))
    assert result.success is False
    assert "kaboom" in result.error


def test_deny_policy_blocks_call(tmp_path):
    policy = PermissionPolicy(permission_mode="DENY", mode="BUILD")
    runtime = _runtime(tmp_path, {"shell": _echo}, policy=policy)
    result = asyncio.run(runtime.execute("shell", {"command": "ls"}))
    assert result.success is False
    assert "denied" in result.error


def test_plan_mode_blocks_build_tools(tmp_path):
    policy = PermissionPolicy(permission_mode="AUTO", mode="PLAN")
    runtime = _runtime(tmp_path, {"write_file": _echo}, policy=policy)
    result = asyncio.run(runtime.execute("write_file", {"path": "x", "content": "y"}))
    assert result.success is False


def test_plan_mode_blocks_mcp_tools(tmp_path):
    policy = PermissionPolicy(permission_mode="AUTO", mode="PLAN")
    runtime = _runtime(tmp_path, {"mcp_execute": _echo}, policy=policy)
    result = asyncio.run(runtime.execute("mcp_execute", {}))
    assert result.success is False


def test_ask_policy_needs_approval(tmp_path):
    async def approve(question, details, default):
        return True

    async def refuse(question, details, default):
        return False

    policy = PermissionPolicy(permission_mode="ASK", mode="BUILD")
    allowed = _runtime(tmp_path, {"shell": _accept_command}, policy=policy, on_permission=approve)
    assert asyncio.run(allowed.execute("shell", {"command": "ls"})).success is True

    denied = _runtime(tmp_path, {"shell": _accept_command}, policy=policy, on_permission=refuse)
    assert asyncio.run(denied.execute("shell", {"command": "ls"})).success is False


def test_path_outside_roots_denied(tmp_path):
    inside = tmp_path / "box"
    inside.mkdir()
    policy = PermissionPolicy(permission_mode="AUTO", mode="BUILD", roots=[str(inside)])
    runtime = _runtime(tmp_path, {"read_file": _accept_path}, policy=policy)
    assert asyncio.run(runtime.execute("read_file", {"path": str(inside / "a.txt")})).success is True
    outside = asyncio.run(runtime.execute("read_file", {"path": "/etc/passwd"}))
    assert outside.success is False
    assert "outside" in outside.error


def test_output_is_capped_and_spilled(tmp_path):
    async def big() -> str:
        return "x" * 60_000

    runtime = _runtime(tmp_path, {"big": big})
    result = asyncio.run(runtime.execute("big", {}))
    assert result.success is True
    assert "truncated" in result.output
    assert list(tmp_path.glob("big_*.txt"))


def test_budget_exhaustion_blocks_call(tmp_path):
    from src.core.runtime import Run

    run = Run.create("goal", "agent")
    run.budget.max_tool_calls = 0
    runtime = _runtime(tmp_path, {"echo": _echo})
    runtime.run = run
    result = asyncio.run(runtime.execute("echo", {"text": "hi"}))
    assert result.success is False
    assert "budget" in result.error


def test_run_records_call_and_result(tmp_path):
    from src.core.runtime import Run

    run = Run.create("goal", "agent")
    runtime = _runtime(tmp_path, {"echo": _echo})
    runtime.run = run
    asyncio.run(runtime.execute("echo", {"text": "hi"}))
    assert len(run.tool_calls) == 1
    assert len(run.tool_results) == 1
    assert run.tool_calls[0].name == "echo"
