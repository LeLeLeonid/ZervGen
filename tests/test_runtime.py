import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.runtime import (Compactor, PermissionPolicy, RunCoordinator, RunStatus,
                              Session, ToolResult, ToolRuntime)


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


def test_call_mode_blocks_build_tools(tmp_path):
    policy = PermissionPolicy(permission_mode="AUTO", mode="CALL")
    runtime = _runtime(tmp_path, {"write": _echo}, policy=policy)
    result = asyncio.run(runtime.execute("write", {"path": "x", "content": "y"}))
    assert result.success is False


def test_call_mode_blocks_mcp_tools(tmp_path):
    policy = PermissionPolicy(permission_mode="AUTO", mode="CALL")
    runtime = _runtime(tmp_path, {"mcp__server__tool": _echo}, policy=policy)
    result = asyncio.run(runtime.execute("mcp__server__tool", {}))
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
    runtime = _runtime(tmp_path, {"read": _accept_path}, policy=policy)
    assert asyncio.run(runtime.execute("read", {"path": str(inside / "a.txt")})).success is True
    outside = asyncio.run(runtime.execute("read", {"path": "/etc/passwd"}))
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




def test_append_and_reload_preserves_branch(tmp_path):
    session = Session(session_id="s1", root=tmp_path)
    session.messages.append({"role": "user", "content": "hello"})
    session.messages.append({"role": "assistant", "content": "hi"})
    assert [m["content"] for m in session.active_branch()] == ["hello", "hi"]

    reopened = Session(session_id="s1", root=tmp_path)
    assert [m["content"] for m in reopened.active_branch()] == ["hello", "hi"]


def test_seed_messages_are_persisted(tmp_path):
    session = Session(session_id="s2", root=tmp_path, seed=[{"role": "user", "content": "seed"}])
    reopened = Session(session_id="s2", root=tmp_path)
    assert reopened.active_branch() == [{"role": "user", "content": "seed"}]


def test_rollback_moves_leaf(tmp_path):
    session = Session(session_id="s3", root=tmp_path)
    first = session.record({"role": "user", "content": "one"})
    session.record({"role": "assistant", "content": "two"})
    assert session.rollback(first) is True
    assert [m["content"] for m in session.active_branch()] == ["one"]
    assert session.rollback("missing") is False


def test_compact_keeps_verbatim_tail(tmp_path):
    session = Session(session_id="s4", root=tmp_path)
    for i in range(10):
        session.record({"role": "user", "content": f"m{i}"})
    session.compact("checkpoint text", keep=3)
    branch = session.active_branch()
    assert branch[0]["role"] == "system"
    assert "checkpoint text" in branch[0]["content"]
    assert [m["content"] for m in branch[1:]] == ["m7", "m8", "m9"]


def test_compactor_keeps_user_words_and_drops_assistant_noise(tmp_path):
    seen = {}

    class Provider:
        async def generate_text(self, messages, system=""):
            seen["prompt"] = messages[0]["content"]
            return "checkpoint"

    session = Session(session_id="s7", root=tmp_path)
    history = [
        {"role": "user", "content": "build the parser"},
        {"role": "assistant", "content": "ASSISTANT-NOISE"},
        {"role": "user", "content": "also add tests"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "keep going"},
        {"role": "assistant", "content": "working"},
    ]
    compacted = asyncio.run(Compactor(Provider(), session).compact(history, budget=1, keep=2))

    assert compacted[0]["role"] == "system"
    assert "<context-summary>\ncheckpoint\n</context-summary>" in compacted[0]["content"]
    assert "<user-specification>" in compacted[0]["content"]
    assert "build the parser" in compacted[0]["content"]
    assert "ASSISTANT-NOISE" not in seen["prompt"]
    assert [m["content"] for m in compacted[1:]] == ["keep going", "working"]


def test_reset_rebuilds_from_messages(tmp_path):
    session = Session(session_id="s5", root=tmp_path)
    session.messages.append({"role": "user", "content": "gone"})
    session.reset([{"role": "user", "content": "fresh"}])
    assert [m["content"] for m in session.active_branch()] == ["fresh"]


def test_ignores_corrupt_lines(tmp_path):
    path = tmp_path / "s6.jsonl"
    path.write_text("not json\n{\"id\": \"a\", \"role\": \"user\", \"content\": \"ok\"}\n")
    session = Session(session_id="s6", root=tmp_path)
    assert [m["content"] for m in session.active_branch()] == ["ok"]


def test_coordinator_finishes_once():
    coordinator = RunCoordinator()
    coordinator.begin()
    assert coordinator.status is RunStatus.RUNNING
    assert coordinator.finish(RunStatus.DONE) is RunStatus.DONE
    assert coordinator.finish(RunStatus.FAILED) is None
    assert coordinator.status is RunStatus.DONE
    assert coordinator.busy is False


def test_coordinator_doorbell_drains():
    coordinator = RunCoordinator()
    coordinator.begin()
    coordinator.ring("  later  ")
    coordinator.ring("")
    assert coordinator.doorbell.is_set()
    assert coordinator.drain() == ["later"]
    assert not coordinator.doorbell.is_set()
    assert coordinator.drain() == []
