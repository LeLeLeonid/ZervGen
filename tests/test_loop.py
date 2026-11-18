import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.cli as cli_module
from src.cli import ZervGenCLI
from src.config import GlobalSettings
from src.core.base_agent import StemAgent
from src.core.orchestrator import _looks_failed
from src.core.runtime import RunStatus
from src.utils import (
    CONTEXT_RESERVE_MIN,
    CONTEXT_RESERVE_SHARE,
    ContextCompressor,
    context_ceiling,
    count_tokens_tiktoken,
    truncate_to_tokens,
)


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
    agent = _agent(tmp_path, monkeypatch, ['::tool read {"path": "note.txt"}', "Read it."])
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
    bus.tool_call("c1", "read", {"path": "a.py"})
    bus.tool_result("c1", output="x" * 340, duration=0.012)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert "read" in lines[0] and "path=a.py" in lines[0] and "340ch" in lines[0]


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
                   ['<tool name="read" path="note.txt"/>', "Read it."])
    output = asyncio.run(agent.run("read note"))
    assert output == "Read it."
    assert [c.name for c in agent._run.tool_calls] == ["read"]


def test_positional_shorthand_binds_tool_parameters(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    agent = _agent(tmp_path, monkeypatch, ['::read("note.txt", 0, 5)', "Read it."])
    asyncio.run(agent.run("read note"))
    call = agent._run.tool_calls[0]
    assert call.name == "read"
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
        [[{"name": "read", "arguments": {"path": "note.txt"}}]],
    )
    agent.settings.tool_protocol = "native"
    output = asyncio.run(agent.run("read note"))
    assert output == "Read note.txt (5 chars)."
    names = [c.name for c in agent._run.tool_calls]
    assert "read" in names
    assert agent.provider.native_tools


def test_ptc_protocol_sends_no_native_tools(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, ["done"])
    agent.provider = NativeProvider(["done"], [])
    agent.settings.tool_protocol = "ptc"
    asyncio.run(agent.run("hi"))
    assert agent.provider.native_tools == []


def test_prompt_keeps_tools_when_the_cap_is_tight(tmp_path, monkeypatch):
    from src.skills_loader import load_role

    agent = _agent(tmp_path, monkeypatch, [])
    agent.settings.prompt_max_chars = 4000
    prompt = agent._build_system_prompt(load_role("system"), user_input="fix")
    assert "--- TOOLS ---" in prompt
    assert "--- EXECUTION PROTOCOL ---" in prompt




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


def test_pytest_sensor_flags_a_failing_suite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert 1 == 2\n", encoding="utf-8")
    agent = _artifact_agent([tmp_path / "tests" / "test_a.py"])
    agent.settings.sensor_tests_timeout = 120
    failure = asyncio.run(agent._test_sensor_failures())
    assert failure.startswith("tests:")
    assert "test_ok" in failure


def test_pytest_sensor_is_silent_when_clean_or_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    agent = _artifact_agent([tmp_path / "tests" / "test_a.py"])
    agent.settings.sensor_tests_timeout = 120
    assert asyncio.run(agent._test_sensor_failures()) == ""

    agent.settings.sensor_tests_enabled = False
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert 1 == 2\n", encoding="utf-8")
    assert asyncio.run(agent._test_sensor_failures()) == ""


def test_trace_records_model_tokens_and_cost_per_step(tmp_path, monkeypatch):
    import json

    agent = _agent(tmp_path, monkeypatch, ["done"])
    asyncio.run(agent.run("hi"))
    path = next((tmp_path / "tmp/state/trace").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    llm = [row for row in rows if row.get("kind") == "llm"]
    assert llm and llm[0]["provider"] == "fake"
    assert llm[0]["input_tokens"] > 0 and llm[0]["output_tokens"] > 0
    assert "elapsed_ms" in llm[0] and "model" in llm[0]


def test_media_paths_ignores_missing_and_duplicates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.utils import Media

    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")
    found = Media.paths("created a.png, then a.png again and b.mp4, not gone.png")
    assert found == ["a.png", "b.mp4"]


def test_activity_renderer_collapses_a_run_of_tool_rows(tmp_path):
    from io import StringIO
    from rich.console import Console
    from src.utils import ActivityRenderer

    sink = StringIO()
    renderer = ActivityRenderer(console=Console(file=sink, force_terminal=False), clock=False, media=False)
    for index in range(3):
        renderer.event("t", "tool_result", f"read path=f{index}.py", {"name": "read"})
    renderer.flush()
    out = sink.getvalue()
    assert "activity" in out and "3 tools" in out
    assert out.count("read path=") == 0
    expanded = renderer.expand(-1)
    assert expanded.count("read ") == 3


def test_activity_renderer_expands_short_runs(tmp_path):
    from io import StringIO
    from rich.console import Console
    from src.utils import ActivityRenderer

    sink = StringIO()
    renderer = ActivityRenderer(console=Console(file=sink, force_terminal=False), clock=False, media=False)
    renderer.event("t", "tool_result", "read path=a.py", {"name": "read"})
    renderer.flush()
    assert "read path=a.py" in sink.getvalue()


def test_legacy_tool_names_still_parse():
    from src.utils import parse_turn
    names = {"read", "write", "shell"}
    assert parse_turn("::read_file(\"a.py\")", names, {}).calls[0].name == "read"
    assert parse_turn('<tool name="write_file" path="a.py" content="x"/>', names, {}).calls[0].name == "write"
    assert parse_turn('{"tool_calls": [{"name": "shell", "arguments": {"command": "ls"}}]}', names, {}).calls[0].name == "shell"


def test_shorthand_survives_a_stray_prefix():
    from src.utils import parse_turn, strip_ptc
    from src.tools import TOOL_REGISTRY
    specs = dict(TOOL_REGISTRY)
    names = set(specs) | {"response"}

    prefixed = '<::write {"path": "g.py", "content": "print(1)"}\n::shell("python g.py")'
    calls = parse_turn(prefixed, names, specs).calls
    assert [c.name for c in calls] == ["write", "shell"]
    assert calls[1].args == {"command": "python g.py"}

    assert parse_turn('<::shell("ls")', names, specs).calls[0].name == "shell"
    assert strip_ptc('<::shell("ls")', names) == ""
    assert parse_turn("The answer is 42. Use ::notation carefully.", names, specs).kind == "answer"

def test_stagnation_names_spinning_and_oscillation(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, [])

    def turn(call: str, result: str) -> str:
        return f"{call}\n---\n{result}"

    same = turn('shell({"command": "pytest -q"})', "3 failed in tests/test_auth.py")
    assert [agent._stagnation(same) for _ in range(3)] == ["", "", "spinning"]

    a = turn('edit({"path": "src/a.py", "find": "x", "replace": "y"})', "Made 1 replacement(s) in src/a.py")
    b = turn('shell({"command": "pytest -q"})', "SyntaxError: invalid syntax (a.py, line 44)")
    assert [agent._stagnation(x) for x in (a, b, a, b)] == ["", "", "", "oscillation"]

    moving = [turn(f'shell({{"command": "grep {i} src/"}})', "10 matches") for i in range(5)]
    assert all(agent._stagnation(x) == "" for x in moving)

    agent._turn_signatures = []
    assert [agent._stagnation(same) for _ in range(4)] == ["", "", "spinning", "spinning"]


def test_repeating_the_same_call_stops_instead_of_spinning(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    agent = _agent(tmp_path, monkeypatch, ['::read("note.txt")'] * 12, max_steps=20)
    output = asyncio.run(agent.run("read it forever"))
    assert agent._run.status is RunStatus.FAILED
    assert "Stopped" in output and "read" in output
    assert agent.provider.calls < 12


def test_prune_state_caps_files_and_traces(tmp_path, monkeypatch):
    from src import utils

    monkeypatch.setattr(utils, "STATE_LIMIT", {"runs.jsonl": 4})
    monkeypatch.setattr(utils, "KEEP_TRACES", 2)
    state = tmp_path / "state"
    (state / "trace").mkdir(parents=True)
    (state / "runs.jsonl").write_text("x\n" * 10, encoding="utf-8")
    for index in range(5):
        (state / "trace" / f"{index}.jsonl").write_text("{}", encoding="utf-8")
    utils.prune_state(tmp_path)
    assert len((state / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 4
    assert len(list((state / "trace").glob("*.jsonl"))) == 2


def test_media_render_never_repeats_a_file(tmp_path):
    from io import StringIO
    from rich.console import Console
    from src.utils import Media

    target = tmp_path / "pic.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    media = Media(console=Console(file=StringIO()), enabled=True)
    media.protocol = "none"
    assert media.render(str(target)) is True
    assert media.render(str(target)) is False
    assert media.render(str(tmp_path / "missing.png")) is False


def test_failed_run_is_not_relabelled_done(tmp_path, monkeypatch):
    import json

    agent = _agent(tmp_path, monkeypatch, [])
    from src.core.runtime import Run

    agent._run = Run.create("goal", "system")
    agent.coordinator.begin()
    agent.coordinator.finish(RunStatus.FAILED, agent._run)
    agent._finish(RunStatus.DONE, "late")
    assert agent._run.status is RunStatus.FAILED
    summary = json.loads((tmp_path / "tmp/state/runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert summary["status"] == "failed"


ECHO = object()


class ContextFakeProvider:
    def __init__(self, reply="SUMMARY", fail=False):
        self.reply = reply
        self.fail = fail
        self.calls = 0

    async def generate_text(self, messages, system_prompt):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return {"content": self.reply}


class FakeCompressor:
    def __init__(self, result):
        self.result = result

    async def compress(self, msgs, keep_tokens=20000):
        return msgs if self.result is ECHO else self.result


def make_history(n=30):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    return msgs


def build_history(n=120):
    filler = "x" * 4000
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"u{i} {filler}"})
        msgs.append({"role": "assistant", "content": f"a{i} {filler}"})
        msgs.append({"role": "tool", "content": f"t{i} {filler}"})
    return msgs


def make_agent(history, compressor_result, budget=0):
    agent = object.__new__(StemAgent)
    agent.history = history
    agent._cached_system_prompt = None
    agent._compressor = FakeCompressor(compressor_result)
    agent.provider = ContextFakeProvider()
    agent.settings = type("S", (), {
        "history_trim_enabled": True,
        "provider": None,
        "max_token_budget": budget,
    })()
    return agent


def test_ceiling_bounds_the_window():
    assert context_ceiling(1_000_000) == 1_000_000 - int(1_000_000 * CONTEXT_RESERVE_SHARE)
    assert context_ceiling(32_000) == 32_000 - CONTEXT_RESERVE_MIN
    for window in (0, 1, 2048, 4096, 8192, 32_000, 128_000, 1_000_000):
        assert 0 <= context_ceiling(window) <= window


def test_truncate_respects_budget():
    out = truncate_to_tokens("word " * 10_000, 100)
    assert count_tokens_tiktoken(out) <= 110
    assert out.endswith("[truncated]")
    assert truncate_to_tokens("short text", 100) == "short text"
    assert truncate_to_tokens("", 100) == ""


def test_compressor_returns_list_and_keeps_system():
    provider = ContextFakeProvider()
    out = asyncio.run(ContextCompressor(provider).compress(make_history(), keep_tokens=10))
    assert isinstance(out, list)
    assert provider.calls == 1
    assert out[0]["role"] == "system"
    assert "SUMMARY" in " ".join(str(m.get("content", "")) for m in out)


def test_compressor_failure_returns_original_and_keeps_previous_summary():
    out = asyncio.run(ContextCompressor(ContextFakeProvider(fail=True)).compress(make_history(), keep_tokens=10))
    assert isinstance(out, list) and any(m.get("role") == "system" for m in out)
    comp = ContextCompressor(ContextFakeProvider(reply="X" * 10000))
    asyncio.run(comp.compress(make_history(), keep_tokens=10))
    assert len(comp._prev) == 10000


def test_trim_survives_none_and_keeps_system():
    agent = make_agent(build_history(), None)
    asyncio.run(agent._trim_history())
    assert isinstance(agent.history, list)


def test_trim_fallback_never_orphans_a_tool_result():
    agent = make_agent(build_history(), ECHO)
    asyncio.run(agent._trim_history())
    assert agent.history[0]["role"] == "system"
    body = [m for m in agent.history if m.get("role") != "system"]
    assert body and body[0]["role"] != "tool"


def test_trim_uses_configured_budget_when_window_is_unknown():
    agent = make_agent(build_history(), ECHO, budget=0)
    before = len(agent.history)
    asyncio.run(agent._trim_history())
    assert len(agent.history) == before

    agent = make_agent(build_history(), ECHO, budget=100_000)
    asyncio.run(agent._trim_history())
    assert len(agent.history) < before


class FakeOrchestrator:
    def __init__(self, response="Answer.", streamed=False, delay=0.0):
        self.response = response
        self._last_streamed = streamed
        self._last_streamed_text = response if streamed else ""
        self.history = []
        self._user_active = False
        self._active_session_id = "s1"
        self.injected = []
        self.interrupted = False
        self._delay = delay

    @property
    def answer_streamed(self) -> bool:
        if not self._last_streamed or not self.response:
            return False
        return (" ".join(self._last_streamed_text.split())
                == " ".join(self.response.split()))

    def new_session(self, title=""):
        return "s1"

    def inject_user_message(self, text):
        self.injected.append(text)

    def request_interrupt(self):
        self.interrupted = True

    async def run(self, task):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self.response


class FakeCoordinator:
    def drain(self):
        return []


def _cli(orchestrator):
    cli = ZervGenCLI.__new__(ZervGenCLI)
    cli.config = GlobalSettings()
    cli.orchestrator = orchestrator
    orchestrator.coordinator = FakeCoordinator()
    cli._user_active = False
    cli._active_lock = asyncio.Lock()
    cli._pending_input = asyncio.Queue()
    cli._agent_task = None
    cli._agent_running = False
    cli._app = None
    cli._perm_future = None
    cli._commands = []

    async def fake_command(cmd):
        cli._commands.append(cmd)
        return True

    cli.handle_system_command = fake_command
    return cli


def _capture(monkeypatch):
    printed = []
    monkeypatch.setattr(cli_module.CC, "print", lambda *args, **kwargs: printed.append(args) if "you" not in str(args[0]) else None)
    return printed


def test_answer_printed_once_when_not_streamed(monkeypatch):
    printed = _capture(monkeypatch)
    cli = _cli(FakeOrchestrator("Final answer."))
    asyncio.run(cli._do_run("hi"))
    assert len(printed) == 1


def test_streamed_answer_is_not_printed_again(monkeypatch):
    printed = _capture(monkeypatch)
    cli = _cli(FakeOrchestrator("Final answer.", streamed=True))
    asyncio.run(cli._do_run("hi"))
    assert printed == []


def test_user_active_flags_are_cleared(monkeypatch):
    _capture(monkeypatch)
    cli = _cli(FakeOrchestrator("ok"))
    asyncio.run(cli._do_run("hi"))
    assert cli._user_active is False
    assert cli.orchestrator._user_active is False


def test_escape_neutralizes_rich_markup():
    cli = _cli(FakeOrchestrator())
    assert cli._escape("[bold red]x[/bold red]") == "\\[bold red]x\\[/bold red]"


def test_midrun_slash_command_is_deferred(monkeypatch):
    _capture(monkeypatch)
    orchestrator = FakeOrchestrator("done", delay=0.05)
    cli = _cli(orchestrator)
    cli._pending_input.put_nowait("/help")
    cli._pending_input.put_nowait("note for the model")
    asyncio.run(cli._process_task("task"))
    assert orchestrator.injected == ["note for the model"]
    assert cli._commands == ["/help"]


def test_stop_during_run_interrupts(monkeypatch):
    _capture(monkeypatch)
    orchestrator = FakeOrchestrator("done", delay=0.2)
    cli = _cli(orchestrator)
    cli._pending_input.put_nowait("/stop")
    asyncio.run(cli._process_task("task"))
    assert orchestrator.interrupted is True


def test_response_value_is_shown_when_stream_carried_only_prose(monkeypatch):
    printed = _capture(monkeypatch)
    orchestrator = FakeOrchestrator("Final answer.", streamed=True)
    orchestrator._last_streamed_text = "Let me check that."
    cli = _cli(orchestrator)
    asyncio.run(cli._do_run("hi"))
    assert len(printed) == 1






def test_auto_await_leaves_calls_inside_a_concurrency_argument_alone(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    names = {"read", "shell"}
    code = agent._auto_await_tools('rs = await asyncio.gather(*[read(f) for f in files])', names)
    assert "await await" not in code
    assert "[await read(f) for f in files]" not in code
    assert "gather(*[read(f) for f in files])" in code


def test_auto_await_still_awaits_a_bare_tool_call(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    code = agent._auto_await_tools('x = read("a")\nprint(x)', {"read"})
    assert "x = await read('a')" in code


def test_auto_await_does_not_double_await(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    code = agent._auto_await_tools('x = await read("a")\ny = await read("b")', {"read"})
    assert "await await" not in code


def test_auto_await_skips_gather_arguments(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    code = agent._auto_await_tools('rs = await asyncio.gather(read("a"), read("b"))', {"read"})
    assert code.count("await") == 1


def test_stop_cancels_child_tasks_and_leaves_none_running():
    from src.core.orchestrator import Orchestrator

    async def drive():
        orch = object.__new__(Orchestrator)
        orch._child_tasks = set()
        orch.interrupt_event = asyncio.Event()
        steps = {"n": 0, "cancelled": 0}

        async def child():
            try:
                while True:
                    steps["n"] += 1
                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                steps["cancelled"] += 1
                raise

        parent = asyncio.create_task(orch._await_children([child(), child()]))
        await asyncio.sleep(0.1)
        parent.cancel()
        try:
            await parent
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.3)
        frozen = steps["n"]
        await asyncio.sleep(0.3)
        return frozen, steps, len(orch._child_tasks)

    frozen, steps, live = asyncio.run(drive())
    assert steps["n"] == frozen
    assert steps["cancelled"] == 2
    assert live == 0


def test_child_agent_obeys_a_stop_that_arrived_before_it_started(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)

    class Parent:
        interrupt_event = asyncio.Event()

    agent._is_delegated = True
    agent.orchestrator = Parent()
    assert agent._child_stopped() is False
    Parent.interrupt_event.set()
    assert agent._child_stopped() is True


def test_auto_await_leaves_calls_inside_a_concurrency_argument_alone(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    code = 'files = ["a"]\nrs = await asyncio.gather(*[read(f) for f in files])'
    transformed = agent._auto_await_tools(code, {"read"})
    assert "[await read(f) for f in files]" not in transformed
    assert "gather(*[read(f) for f in files])" in transformed


def test_auto_await_still_wraps_a_bare_call(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    assert "x = await read('a')" in agent._auto_await_tools('x = read("a")', {"read"})


def test_auto_await_does_not_double_wrap_an_awaited_call(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    code = agent._auto_await_tools('x = await read("a")', {"read"})
    assert code.count("await") == 1


def test_auto_await_never_wraps_gather_arguments(monkeypatch, tmp_path):
    agent = _sandbox_agent(monkeypatch, tmp_path)
    code = agent._auto_await_tools('rs = await asyncio.gather(read("a"), read("b"))', {"read"})
    assert code.count("await") == 1


def test_stop_cancels_child_tasks_and_leaves_none_running():
    from src.core.orchestrator import Orchestrator

    async def drive():
        orch = object.__new__(Orchestrator)
        orch._child_tasks = set()
        orch.agents = {}
        orch.interrupt_event = asyncio.Event()
        state = {"cancelled": 0}

        async def child():
            try:
                while True:
                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                state["cancelled"] += 1
                raise

        task = asyncio.create_task(orch._await_children([child(), child()]))
        await asyncio.sleep(0.1)
        assert orch.live_children() == 1
        orch.request_interrupt()
        result = await asyncio.wait_for(task, timeout=5)
        return result, state, orch.live_children()

    result, state, live = asyncio.run(drive())
    assert result is None
    assert state["cancelled"] == 2
    assert live == 0


def test_prompt_never_carries_an_unclosed_fence(monkeypatch, tmp_path):
    from src.config import load_config
    from src.skills_loader import load_role

    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    cfg = load_config()
    cfg.memory_enabled = False
    cfg.prompt_auto_trigger_skills = False
    agent = StemAgent(name="system", provider=None, skill_name="system", settings=cfg, mode="BUILD")
    prompt = agent._build_system_prompt(load_role("system"))
    fences = sum(1 for line in prompt.splitlines() if line.lstrip().startswith("```"))
    assert fences % 2 == 0


def test_truncated_rules_do_not_swallow_what_follows(monkeypatch, tmp_path):
    from src.config import load_config
    from src.skills_loader import load_role

    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(root)
    cfg = load_config()
    cfg.memory_enabled = False
    cfg.prompt_auto_trigger_skills = False
    agent = StemAgent(name="system", provider=None, skill_name="system", settings=cfg, mode="BUILD")
    rules = agent._prompt_project_rules()
    fences = sum(1 for line in rules.splitlines() if line.lstrip().startswith("```"))
    assert fences % 2 == 0, "a truncated rule section must close its fence"


def test_delegated_agent_does_not_resend_its_role_prompt_as_history(monkeypatch, tmp_path):
    from src.core.orchestrator import Orchestrator
    from src.skills_loader import load_role

    monkeypatch.chdir(tmp_path)
    cfg = GlobalSettings()
    cfg.memory_enabled = False
    cfg.prompt_auto_trigger_skills = False

    class Provider:
        name = "rec"
        model = "rec-1"
        native_tools = []
        supports_native_tools = False
        _last_cost = 0.0

        def request_timeout(self, default):
            return default

        def clone(self):
            return self

        async def generate_text(self, messages, system, on_token=None):
            captured["messages"] = messages
            captured["system"] = system
            return "done"

    captured = {}
    orch = Orchestrator(provider=Provider(), settings=cfg, mode="BUILD")
    orch._silent = True
    orch._set_status = lambda *a, **k: None
    orch._log = lambda *a, **k: None
    orch.history = [{"role": "user", "content": "earlier"}]
    asyncio.run(orch._handle_delegation({"agent_name": "code", "task": "do it"}))

    role_prompt = (load_role("code") or load_role("system")).prompt
    history = captured["messages"]
    assert not [m for m in history if m.get("role") == "system"]
    assert captured["system"].count(role_prompt.strip()[:60]) == 1
