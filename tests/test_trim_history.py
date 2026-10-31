import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.base_agent import StemAgent

ECHO = object()


class FakeProvider:
    async def generate_text(self, messages, system_prompt):
        return {"content": "SUMMARY"}


class FakeCompressor:
    def __init__(self, result):
        self.result = result

    async def compress(self, msgs, keep_tokens=20000):
        return msgs if self.result is ECHO else self.result


def make_agent(history, compressor_result):
    agent = object.__new__(StemAgent)
    agent.history = history
    agent._cached_system_prompt = None
    agent._compressor = FakeCompressor(compressor_result)
    agent.provider = FakeProvider()
    agent.settings = type("S", (), {
        "history_trim_enabled": True,
        "provider": None,
    })()
    return agent


def build_history(n=120):
    filler = "x" * 4000
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"u{i} {filler}"})
        msgs.append({"role": "assistant", "content": f"a{i} {filler}"})
        msgs.append({"role": "tool", "content": f"t{i} {filler}"})
    return msgs


def test_none_from_compressor_does_not_nuke_history():
    agent = make_agent(build_history(), None)
    asyncio.run(agent._trim_history())
    assert agent.history is not None
    assert isinstance(agent.history, list)


def test_fallback_never_starts_with_tool_result():
    agent = make_agent(build_history(), ECHO)
    asyncio.run(agent._trim_history())
    body = [m for m in agent.history if m.get("role") != "system"]
    assert body, "fallback must keep some history"
    assert body[0]["role"] != "tool", f"orphaned tool result at head: {body[0]['role']}"


def test_system_messages_survive():
    agent = make_agent(build_history(), ECHO)
    asyncio.run(agent._trim_history())
    assert agent.history[0]["role"] == "system"
