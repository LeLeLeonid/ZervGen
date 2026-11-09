import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.base_agent import StemAgent
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
