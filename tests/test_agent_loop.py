import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.base_agent import StemAgent
from src.core.orchestrator import _looks_failed
from src.core.runtime import RunStatus


class FakeProvider:
    name = "fake"

    def __init__(self, replies, delay=0.0):
        self.replies = list(replies)
        self.delay = delay
        self.calls = 0
        self._last_cost = 0.0

    async def generate_text(self, history, system_prompt, on_token=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.replies:
            return "Final answer."
        reply = self.replies.pop(0)
        if on_token is not None:
            on_token(reply)
        return reply


def _agent(tmp_path, monkeypatch, replies, delay=0.0, **settings):
    monkeypatch.chdir(tmp_path)
    from src.config import load_config

    cfg = load_config()
    cfg.max_steps = 5
    cfg.tool_timeout = 5
    cfg.provider_timeout = 5
    cfg.prompt_auto_trigger_skills = False
    cfg.memory_enabled = False
    for key, value in settings.items():
        setattr(cfg, key, value)
    agent = StemAgent(name="system", provider=FakeProvider(replies, delay=delay), skill_name="system",
                      settings=cfg, mode="BUILD")
    agent._silent = True
    agent._set_status = lambda *args, **kwargs: None
    agent._init_mcp = _no_mcp
    return agent


async def _no_mcp():
    return None


def test_plain_answer_ends_done(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["All done."])
    output = asyncio.run(agent.run("say hi"))
    assert output == "All done."
    assert agent._run.status is RunStatus.DONE


def test_ptc_then_answer_ends_done(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["```python\nreturn 21 * 2\n```", "The answer is 42."])
    output = asyncio.run(agent.run("compute"))
    assert output == "The answer is 42."
    assert agent._run.status is RunStatus.DONE
    assert agent.provider.calls == 2


def test_provider_failure_ends_failed(tmp_path, monkeypatch):
    class Broken:
        name = "fake"
        _last_cost = 0.0

        async def generate_text(self, history, system_prompt, on_token=None):
            raise RuntimeError("network down")

    agent = _agent(tmp_path, monkeypatch, [])
    agent.provider = Broken()
    agent.settings.max_retries = 1
    asyncio.run(agent.run("work"))
    assert agent._run.status is RunStatus.FAILED


def test_interrupt_ends_stopped(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["never reached"], delay=0.2)

    async def drive():
        task = asyncio.create_task(agent.run("long job"))
        await asyncio.sleep(0.05)
        agent.interrupt_event.set()
        task.cancel()
        return await task

    asyncio.run(drive())
    assert agent._run.status is RunStatus.STOPPED


def test_max_steps_is_reported(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["```python\nreturn 1\n```"] * 10, max_steps=2)
    asyncio.run(agent.run("loop forever"))
    assert agent._run.status is RunStatus.MAX_STEPS


def test_finish_sets_status_once(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, [])
    from src.core.runtime import Run

    agent._run = Run.create("goal", "system")
    agent.coordinator.begin()
    agent._finish(RunStatus.DONE, "ok")
    agent._finish(RunStatus.FAILED, "later")
    assert agent._run.status is RunStatus.DONE


def test_session_history_is_the_agent_history(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["first", "second"])
    asyncio.run(agent.run("one"))
    assert agent.history is agent.session.messages
    assert [m["content"] for m in agent.history] == ["one", "first"]


def test_shorthand_call_runs_without_ptc(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    agent = _agent(tmp_path, monkeypatch, ['::tool read_file {"path": "note.txt"}', "Read it."])
    output = asyncio.run(agent.run("read note"))
    assert output == "Read it."
    assert agent._run.status is RunStatus.DONE
    assert agent.provider.calls == 2


def test_response_is_a_loop_verb_not_a_tool(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ['```python\nawait response(text="final")\n```'])
    output = asyncio.run(agent.run("answer now"))
    assert output == "final"
    names = [c.name for c in agent._run.tool_calls]
    assert "response" not in names


def test_one_answer_renders_one_line(tmp_path, monkeypatch, capsys):
    agent = _agent(tmp_path, monkeypatch, ["Just prose."])
    agent._silent = False
    asyncio.run(agent.run("say it"))
    painted = [l for l in capsys.readouterr().out.splitlines() if "answered" in l]
    assert len(painted) == 1


def test_trace_bus_collapses_call_and_result(tmp_path, monkeypatch, capsys):
    from src.utils import TraceBus

    bus = TraceBus(role="t", enabled=True, root=tmp_path / "trace")
    bus.bind("run1")
    bus.step(1)
    bus.tool_call("c1", "read_file", {"path": "a.py"})
    bus.tool_result("c1", output="x" * 340, duration=0.012)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert "read_file" in lines[0] and "path=a.py" in lines[0] and "340ch" in lines[0]


def test_trace_bus_drops_duplicate_within_step(tmp_path, monkeypatch, capsys):
    from src.utils import TraceBus

    bus = TraceBus(role="t", enabled=True, root=tmp_path / "trace")
    bus.bind("run2")
    bus.step(1)
    bus.emit("state", "t", "same")
    bus.emit("state", "t", "same")
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    bus.step(2)
    bus.emit("state", "t", "same")
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_xml_tool_call_runs_through_the_same_loop(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    agent = _agent(tmp_path, monkeypatch,
                   ['<tool name="read_file" path="note.txt"/>', "Read it."])
    output = asyncio.run(agent.run("read note"))
    assert output == "Read it."
    assert [c.name for c in agent._run.tool_calls] == ["read_file"]


def test_positional_shorthand_binds_tool_parameters(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    agent = _agent(tmp_path, monkeypatch, ['::read_file("note.txt", 0, 5)', "Read it."])
    asyncio.run(agent.run("read note"))
    call = agent._run.tool_calls[0]
    assert call.name == "read_file"
    assert call.arguments == {"path": "note.txt", "offset": 0, "max_chars": 5}


def test_shorthand_response_ends_the_turn_without_a_tool_row(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["::response(text=\"final\")"])
    output = asyncio.run(agent.run("answer now"))
    assert output == "final"
    assert [c.name for c in agent._run.tool_calls] == []


class NativeProvider:
    name = "fake"
    supports_native_tools = True

    def __init__(self, replies, calls):
        self.replies = list(replies)
        self.scripted = list(calls)
        self.native_tools = []
        self.native_calls = []
        self._last_cost = 0.0
        self.calls = 0

    async def generate_text(self, history, system_prompt, on_token=None):
        self.calls += 1
        self.native_calls = self.scripted.pop(0) if self.scripted else []
        return self.replies.pop(0) if self.replies else ""


def test_native_calls_enter_the_same_dispatch(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    agent = _agent(tmp_path, monkeypatch, [])
    agent.provider = NativeProvider(
        ["", "Read note.txt (5 chars)."],
        [[{"name": "read_file", "arguments": {"path": "note.txt"}}]],
    )
    agent.settings.tool_protocol = "native"
    output = asyncio.run(agent.run("read note"))
    assert output == "Read note.txt (5 chars)."
    names = [c.name for c in agent._run.tool_calls]
    assert "read_file" in names
    assert agent.provider.native_tools


def test_ptc_protocol_sends_no_native_tools(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["done"])
    agent.provider = NativeProvider(["done"], [])
    agent.settings.tool_protocol = "ptc"
    asyncio.run(agent.run("hi"))
    assert agent.provider.native_tools == []


def test_prompt_reports_degraded_server_with_cached_tools(tmp_path, monkeypatch):
    from types import SimpleNamespace

    agent = _agent(tmp_path, monkeypatch, [])
    agent.settings.prompt_show_mcp_tools = True
    agent._mcp = SimpleNamespace(servers={"alpha": SimpleNamespace(
        state="degraded", connected=False, tools={"search": object(), "add": object()})})
    text = agent._prompt_mcp()
    assert "alpha: degraded (2 cached tools)" in text
    assert "mcp_execute" in text


# --- PTC sandbox ---
def _sandbox_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    agent = StemAgent(name="system", provider=None, skill_name="system", mode="BUILD")
    agent._silent = True
    agent._set_status = lambda *args, **kwargs: None
    agent._log = lambda *args, **kwargs: None
    return agent


def _blocked(agent, code):
    return agent._validate_ptc(code) is not None


def test_import_is_blocked(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    assert _blocked(agent, "import os\nos.system('id')")


def test_import_after_first_node_is_blocked(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    assert _blocked(agent, "x = 1\ny = 2\nimport os")


def test_private_attribute_is_blocked(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    assert _blocked(agent, "json.__builtins__['__import__']('os').system('id')")
    assert _blocked(agent, "print.__self__")


def test_dunder_class_chain_is_blocked(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    assert _blocked(agent, "().__class__.__base__.__subclasses__()")
    assert _blocked(agent, "type(1).__mro__")


def test_escape_helpers_are_blocked(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    assert _blocked(agent, "getattr(object, 'x')")
    assert _blocked(agent, "open('/etc/passwd').read()")
    assert _blocked(agent, "__import__('os')")


def test_format_field_traversal_is_blocked(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    assert _blocked(agent, "'{0.__globals__[__builtins__]}'.format(json.dumps)")
    assert _blocked(agent, "'{0.__class__.__mro__}'.format(1)")
    # splitting the field so the literal check cannot see it must still fail
    assert _blocked(agent, "('{' + '0.__class__' + '}').format(1)")
    assert _blocked(agent, "'%s %s'.format_map({'a': 1})")


def test_normal_script_passes(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    assert agent._validate_ptc("data = json.dumps({'a': 1})\nprint(len(data))") is None


def test_syntax_error_is_reported(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    error = agent._validate_ptc("def broken(:\n    pass")
    assert error and error.startswith("Syntax Error")


def test_execution_rejects_escape(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    output = asyncio.run(agent._run_code("import os", ""))
    assert output.startswith("PTC blocked")


def test_execution_prints_and_returns(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    output = asyncio.run(agent._run_code("print('hi')\nreturn 2 + 2", ""))
    assert output == "hi\n4"


def test_json_namespace_exposes_only_data_helpers(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    output = asyncio.run(agent._run_code("return json.dumps({'k': 1})", ""))
    assert output == '{"k": 1}'
    rejected = asyncio.run(agent._run_code("return json.__builtins__", ""))
    assert rejected.startswith("PTC blocked")


# --- verification sensors ---


def _sensor_agent(commands):
    agent = object.__new__(StemAgent)
    agent.mode = "BUILD"
    agent._active_tools = lambda: {"shell": object()}
    agent.settings = type("S", (), {"verification_commands": commands})()
    return agent


def test_failing_command_is_reported():
    agent = _sensor_agent(["exit 1"])
    failures = asyncio.run(agent._run_verification_sensors())
    assert failures, "exit 1 must be reported as a failure"


def test_blocked_command_is_reported():
    agent = _sensor_agent(["rm -rf /"])
    failures = asyncio.run(agent._run_verification_sensors())
    assert failures


def test_passing_commands_report_nothing():
    agent = _sensor_agent(["true", "echo ok"])
    failures = asyncio.run(agent._run_verification_sensors())
    assert failures == ""


def test_output_starting_with_Error_but_exit_zero_passes():
    agent = _sensor_agent(["echo 'Error: not really'; exit 0"])
    failures = asyncio.run(agent._run_verification_sensors())
    assert failures == ""


def test_looks_failed_markers():
    assert _looks_failed("Error: boom")
    assert _looks_failed("BLOCKED: nope")
    assert _looks_failed("Timeout after 60s")
    assert _looks_failed("Exit code: 1\nSTDOUT: x")
    assert not _looks_failed("Exit code: 0\nSTDOUT: x")
    assert not _looks_failed("all good")


def _artifact_agent(paths, attempts=3):
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
    assert "bad.py" in _artifact_agent([path])._file_sensor_failures()


def test_invalid_json_artifact_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    assert "invalid JSON" in _artifact_agent([path])._file_sensor_failures()


def test_missing_artifact_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "missing" in _artifact_agent([tmp_path / "gone.py"])._file_sensor_failures()


def test_valid_artifacts_are_silent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "ok.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert _artifact_agent([path])._file_sensor_failures() == ""


def test_escalation_after_max_sensor_attempts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "bad.py"
    path.write_text("def broken(:\n", encoding="utf-8")
    agent = _artifact_agent([path], attempts=2)

    handled, terminal = asyncio.run(agent._verify_or_repair())
    assert handled and terminal is None
    assert "Verification failed" in agent.history[-1]["content"]

    handled, terminal = asyncio.run(agent._verify_or_repair())
    assert handled and terminal is not None
    assert "DECISION REQUIRED" in terminal
    incidents = (tmp_path / "tmp/state/incidents.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(incidents) == 2
