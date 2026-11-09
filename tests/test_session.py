import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.runtime import RunCoordinator, RunStatus, Session


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
