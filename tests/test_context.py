import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.core.base_agent import StemAgent
from src.core.memory import SessionDB
from src.core.provider import normalize_messages


class _Provider:
    name = "stub"
    model = "stub-1"
    native_tools = None
    native_calls = []
    _last_cost = 0.0
    _last_attempts = 1

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def supports_native_tools(self):
        return False

    def request_timeout(self, default):
        return 5

    async def generate_text(self, history, system_prompt, on_token=None):
        self.seen.append(normalize_messages(history, system_prompt))
        return self.replies.pop(0) if self.replies else "ok"


def _agent(monkeypatch, tmp_path, replies, mode="CALL"):
    monkeypatch.chdir(tmp_path)
    settings = load_config()
    settings.history_trim_enabled = False
    provider = _Provider(replies)
    agent = StemAgent(name="system", provider=provider, skill_name="system",
                      settings=settings, mode=mode, silent=True)
    return agent, provider


def test_user_input_reaches_the_model_verbatim(monkeypatch, tmp_path):
    agent, provider = _agent(monkeypatch, tmp_path, ["done"])
    text = "Fix <b>this</b> and parse <root><a>1</a></root>"
    asyncio.run(agent.run(text))
    assert provider.seen[0][-1] == {"role": "user", "content": text}


def test_tool_result_enters_history_once_as_a_tool_turn(monkeypatch, tmp_path):
    (tmp_path / "note.txt").write_text("hello\n")
    agent, provider = _agent(monkeypatch, tmp_path, ['::read("note.txt")', "done"])
    asyncio.run(agent.run("read the note"))
    roles = [m["role"] for m in agent.history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert agent.history[2]["name"] == "read"


def test_answer_is_not_appended_twice(monkeypatch, tmp_path):
    agent, _ = _agent(monkeypatch, tmp_path, ['::response("the answer")'])
    asyncio.run(agent.run("answer please"))
    answers = [m for m in agent.history if m.get("content") == "the answer"]
    assert len(answers) == 1


def test_shorthand_comment_becomes_narration(monkeypatch, tmp_path):
    (tmp_path / "note.txt").write_text("hello\n")
    agent, _ = _agent(monkeypatch, tmp_path, ['# opening the note\n::read("note.txt")  # peek\n', "done"])
    seen = []
    agent._silent = False
    agent.trace.paint = True
    agent.trace.renderer = type("R", (), {
        "event": lambda self, role, kind, text, data: seen.append((kind, text))})()
    asyncio.run(agent.run("read the note"))
    notes = [text for kind, text in seen if kind == "narration"]
    assert notes == ["opening the note", "peek"]


def test_mcp_tools_stay_out_of_the_builtin_list(monkeypatch, tmp_path):
    agent, _ = _agent(monkeypatch, tmp_path, ["done"])
    agent._mcp = type("M", (), {"_calls": {"mcp__alpha__search": ("alpha", "search")}})()
    assert "mcp__alpha__search" not in agent._builtin_names()
    assert "read" in agent._builtin_names()


def test_prompt_never_mentions_mcp_tools(monkeypatch, tmp_path):
    agent, _ = _agent(monkeypatch, tmp_path, ["done"])
    server = type("S", (), {"connected": True, "state": "ready", "tools": {"search": {}},
                            "public_names": {"search": "mcp__alpha__search"}})()
    agent._mcp = type("M", (), {"servers": {"alpha": server},
                                "_calls": {"mcp__alpha__search": ("alpha", "search")}})()
    section = agent._prompt_tools()
    assert "mcp__alpha__search" not in section
    assert "mcps" not in section and "mcp_search" not in section
    assert "read" in section


def test_compaction_rewrites_the_session_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = SessionDB(tmp_path / "sessions.db")
    session_id = db.create_session(provider="stub")
    db.save_message(session_id, "user", "old question")
    db.replace_messages(session_id, [{"role": "system", "content": "<context-summary>\nx\n</context-summary>"},
                                     {"role": "user", "content": "tail"}])
    loaded = db.load_session(session_id)
    assert [m["content"] for m in loaded["messages"]] == [
        "<context-summary>\nx\n</context-summary>", "tail"]
    db.close()


def test_ask_mode_stops_after_one_step(monkeypatch, tmp_path):
    agent, _ = _agent(monkeypatch, tmp_path, ["done"], mode="ASK")
    assert agent._get_max_steps() == 1


def test_ptc_comments_narrate_but_strings_do_not(monkeypatch, tmp_path):
    from src.utils import comment_lines
    code = (
        "# open the config\n"
        "text = 'a # not a comment'\n"
        "x = 1  # trailing note\n"
        "y = 2  # noqa: E501\n"
    )
    assert comment_lines(code) == ["open the config", "trailing note"]


def test_hook_glob_list_matches_every_write_tool():
    from src.core.base_agent import StemAgent
    key = "pre_tool_call:write,edit,append"
    for tool in ("write", "write_file", "edit", "edit_file", "append"):
        assert StemAgent._hook_match(key, "pre_tool_call", tool)
    assert not StemAgent._hook_match(key, "pre_tool_call", "shell")
    assert not StemAgent._hook_match(key, "post_tool_call", "write")


def test_hook_timeout_is_reported_not_swallowed(monkeypatch, tmp_path):
    agent, _ = _agent(monkeypatch, tmp_path, ["done"])
    monkeypatch.setattr(agent.settings, "tool_hooks", {"pre_tool_call": ["sleep 5"]})
    monkeypatch.setattr(agent.settings, "hook_timeout", 1)
    import pytest
    with pytest.raises(TimeoutError):
        asyncio.run(agent._run_hook("pre_tool_call", "shell", {"command": "x"}))


def test_q_in_chat_exits_the_process_not_the_menu(monkeypatch, tmp_path):
    from src.cli import ZervGenCLI
    import pytest

    cli = ZervGenCLI.__new__(ZervGenCLI)
    cli._quit_requested = False
    cli.orchestrator = None
    cli._mcp_warm_task = None

    async def fake_chat_loop():
        cli._quit_requested = True

    async def fake_cleanup():
        pass

    cli.chat_loop = fake_chat_loop
    cli._cleanup = fake_cleanup
    cli.print_banner = lambda **kw: None
    monkeypatch.setattr("src.cli.Prompt.ask", lambda *a, **k: "1")
    with pytest.raises(SystemExit):
        asyncio.run(cli.run())


def test_back_from_chat_returns_to_the_menu(monkeypatch, tmp_path):
    from src.cli import ZervGenCLI
    import pytest

    cli = ZervGenCLI.__new__(ZervGenCLI)
    cli._quit_requested = False
    cli.orchestrator = None
    cli._mcp_warm_task = None

    calls = []

    async def fake_chat_loop():
        calls.append("chat")
        cli._quit_requested = False

    async def fake_cleanup():
        pass

    cli.chat_loop = fake_chat_loop
    cli._cleanup = fake_cleanup
    cli.print_banner = lambda **kw: None

    answers = iter(["1", "3"])
    monkeypatch.setattr("src.cli.Prompt.ask", lambda *a, **k: next(answers))
    with pytest.raises(SystemExit):
        asyncio.run(cli.run())
    assert calls == ["chat"]


def test_history_summary_hides_a_call_only_turn(monkeypatch, tmp_path):
    agent, _ = _agent(monkeypatch, tmp_path, ["done"])
    agent.history = [
        {"role": "user", "content": "make a file"},
        {"role": "assistant", "content": '::write {"path": "tmp/x.txt", "content": "N"}'},
        {"role": "tool", "content": "tmp/x.txt (+3 -0)", "name": "write"},
        {"role": "assistant", "content": "Done. Created tmp/x.txt."},
        {"role": "assistant", "content": 'Let me look\n::ls("tmp")'},
    ]
    summary = agent.get_history_summary()
    assert "::write" not in summary
    assert "Done. Created tmp/x.txt." in summary
    assert "Let me look" in summary


def test_export_keeps_prose_but_drops_call_syntax(monkeypatch, tmp_path):
    from src.cli import _conversation
    rows = _conversation([
        {"role": "user", "content": "make a file"},
        {"role": "assistant", "content": '::write {"path": "tmp/x.txt", "content": "N"}'},
        {"role": "tool", "content": "tmp/x.txt (+3 -0)", "name": "write"},
        {"role": "assistant", "content": "Done. Created tmp/x.txt."},
        {"role": "assistant", "content": 'Let me look\n::ls("tmp")'},
    ])
    assert [r["content"] for r in rows] == ["make a file", "Done. Created tmp/x.txt.", "Let me look"]


def test_agent_log_receives_silent_events_even_with_trace_off(monkeypatch, tmp_path, caplog):
    import logging as _logging
    from src.utils import TraceBus
    monkeypatch.chdir(tmp_path)
    bus = TraceBus(role="system", enabled=False, root=tmp_path / "trace")
    bus.bind("run1")
    with caplog.at_level(_logging.INFO):
        bus.emit("ptc_call", "system", '::write {"path": "x"}')
        bus.tool_call("c1", "write", {"path": "x.txt"})
        bus.tool_result("c1", "write", ok=True, output="ok")
    text = caplog.text
    assert "PTC_CALL" in text and "TOOL_CALL" in text and "TOOL_RESULT" in text
    assert not (tmp_path / "trace" / "run1.jsonl").exists()


def test_markdown_heading_survives_strip_ptc():
    from src.utils import strip_ptc
    raw = "# Findings\n\nBody text\n::ls(\"tmp\")"
    assert strip_ptc(raw, {"ls"}).startswith("# Findings")


def test_progress_note_beside_a_call_is_not_prose():
    from src.utils import strip_ptc
    raw = "::read(\"a\")\n# opening the note\n::read(\"b\")"
    assert strip_ptc(raw, {"read"}) == ""


def test_compaction_keeps_the_pinned_role(tmp_path, monkeypatch):
    from src.core.memory import SessionDB
    monkeypatch.chdir(tmp_path)
    db = SessionDB(tmp_path / "s.db")
    session_id = db.create_session(provider="stub")
    db.replace_messages(session_id, [
        {"role": "system", "content": "<context-summary>\nx\n</context-summary>"},
        {"role": "user", "content": "tail"},
        {"role": "system", "content": "code", "tool_name": "agent_role"},
    ])
    loaded = db.load_session(session_id)
    pinned = [m for m in loaded["messages"] if m.get("tool_name") == "agent_role"]
    assert pinned and pinned[0]["content"] == "code"
    db.close()


def test_ask_mode_allows_one_turn_to_read_a_tool_result(monkeypatch, tmp_path):
    (tmp_path / "note.txt").write_text("hello\n")
    agent, provider = _agent(monkeypatch, tmp_path, ['::read("note.txt")', "it says hello"], mode="ASK")
    out = asyncio.run(agent.run("what is in the note"))
    assert out == "it says hello"
    assert len(provider.seen) == 2


def test_diff_renders_under_the_tool_row_it_belongs_to():
    from src.utils import ActivityRenderer
    class _Console:
        def __init__(self): self.lines = []
        def print(self, line, **kw): self.lines.append(str(line))
    view = ActivityRenderer(console=_Console(), clock=False, media=False)
    view.event("system", "tool_result", "write [path=x.py]", {"name": "write", "ok": True})
    view.event("system", "diff", "x.py (+1 -0)\n+a", {})
    view.flush()
    text = "\n".join(view.console.lines)
    assert "write" in text and "path=x.py" in text
    assert "+a" in text
    assert text.index("write") < text.index("+a")


def test_a_diff_with_no_tool_row_still_renders():
    from src.utils import ActivityRenderer
    class _Console:
        def __init__(self): self.lines = []
        def print(self, line, **kw): self.lines.append(str(line))
    view = ActivityRenderer(console=_Console(), clock=False, media=False)
    view.event("system", "diff", "x.py (+1 -0)\n+a", {})
    view.flush()
    assert any("+a" in line for line in view.console.lines)


def test_stream_filter_does_not_treat_python_code_as_the_answer():
    from src.core.base_agent import _StreamFilter
    stream = _StreamFilter()
    for chunk in ('```python\n', 'x = await read("a")\n', '```\n'):
        stream(chunk)
    stream.flush()
    assert stream.wrote is False
    assert stream.emitted == ""


def test_a_write_reports_the_change_it_made(tmp_path, monkeypatch):
    agent, _ = _agent(monkeypatch, tmp_path, ["done"])
    assert agent._format_tool_output("read", "x" * 30_000) == "x" * 30_000
    big = agent._format_tool_output("shell", "y" * 30_000)
    assert "TRUNCATED" in big and len(big) < 25_000
