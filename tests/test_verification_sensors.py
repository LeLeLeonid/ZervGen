import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.base_agent import StemAgent
from src.core.orchestrator import _looks_failed


def make_agent(commands):
    agent = object.__new__(StemAgent)
    agent.mode = "BUILD"
    agent._active_tools = lambda: {"shell": object()}
    agent.settings = type("S", (), {"verification_commands": commands})()
    return agent


def test_failing_command_is_reported():
    agent = make_agent(["exit 1"])
    failures = asyncio.run(agent._run_verification_sensors())
    assert failures, "exit 1 must be reported as a failure"


def test_blocked_command_is_reported():
    agent = make_agent(["rm -rf /"])
    failures = asyncio.run(agent._run_verification_sensors())
    assert failures


def test_passing_commands_report_nothing():
    agent = make_agent(["true", "echo ok"])
    failures = asyncio.run(agent._run_verification_sensors())
    assert failures == ""


def test_output_starting_with_Error_but_exit_zero_passes():
    agent = make_agent(["echo 'Error: not really'; exit 0"])
    failures = asyncio.run(agent._run_verification_sensors())
    assert failures == ""


def test_looks_failed_markers():
    assert _looks_failed("Error: boom")
    assert _looks_failed("BLOCKED: nope")
    assert _looks_failed("Timeout after 60s")
    assert _looks_failed("Exit code: 1\nSTDOUT: x")
    assert not _looks_failed("Exit code: 0\nSTDOUT: x")
    assert not _looks_failed("all good")


def _sensor_agent(paths, attempts=3):
    agent = object.__new__(StemAgent)
    agent.name = "system"
    agent.mode = "BUILD"
    agent.history = []
    agent._sensor_attempts = 0
    agent._run = type("R", (), {"artifacts": [type("A", (), {"path": str(p)})() for p in paths]})()
    agent.settings = type("S", (), {
        "verification_commands": [],
        "sensors_enabled": True,
        "sensor_max_attempts": attempts,
    })()
    agent._active_tools = lambda: set()
    agent._log = lambda *args, **kwargs: None
    agent.invalidate_cache = lambda: None
    agent._finish = lambda status, message: message
    return agent


def test_broken_python_artifact_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "bad.py"
    path.write_text("def broken(:\n", encoding="utf-8")
    assert "bad.py" in _sensor_agent([path])._file_sensor_failures()


def test_invalid_json_artifact_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    assert "invalid JSON" in _sensor_agent([path])._file_sensor_failures()


def test_missing_artifact_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "missing" in _sensor_agent([tmp_path / "gone.py"])._file_sensor_failures()


def test_valid_artifacts_are_silent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "ok.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert _sensor_agent([path])._file_sensor_failures() == ""


def test_escalation_after_max_sensor_attempts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "bad.py"
    path.write_text("def broken(:\n", encoding="utf-8")
    agent = _sensor_agent([path], attempts=2)

    handled, terminal = asyncio.run(agent._verify_or_repair())
    assert handled and terminal is None
    assert "Verification failed" in agent.history[-1]["content"]

    handled, terminal = asyncio.run(agent._verify_or_repair())
    assert handled and terminal is not None
    assert "DECISION REQUIRED" in terminal
    incidents = (tmp_path / "tmp/state/incidents.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(incidents) == 2
