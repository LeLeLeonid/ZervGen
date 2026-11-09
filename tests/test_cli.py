import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.cli as cli_module
from src.cli import ZervGenCLI
from src.config import GlobalSettings


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
    monkeypatch.setattr(cli_module.CC, "print", lambda *args, **kwargs: printed.append(args))
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


def test_response_value_is_shown_when_nothing_streamed(monkeypatch):
    printed = _capture(monkeypatch)
    orchestrator = FakeOrchestrator("Final answer.", streamed=False)
    orchestrator._last_streamed = True
    orchestrator._last_streamed_text = ""
    cli = _cli(orchestrator)
    asyncio.run(cli._do_run("hi"))
    assert len(printed) == 1
