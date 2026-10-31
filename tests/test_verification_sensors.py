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
