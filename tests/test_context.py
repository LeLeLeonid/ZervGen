import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.base_agent import StemAgent
from src.utils import (
    CONTEXT_RESERVE_MIN,
    CONTEXT_RESERVE_SHARE,
    ContextCompressor,
    context_ceiling,
    count_tokens_tiktoken,
    truncate_to_tokens,
)

ECHO = object()


class FakeProvider:
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
    agent.provider = FakeProvider()
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
    provider = FakeProvider()
    out = asyncio.run(ContextCompressor(provider).compress(make_history(), keep_tokens=10))
    assert isinstance(out, list)
    assert provider.calls == 1
    assert out[0]["role"] == "system"
    assert "SUMMARY" in " ".join(str(m.get("content", "")) for m in out)


def test_compressor_failure_returns_original_and_keeps_previous_summary():
    out = asyncio.run(ContextCompressor(FakeProvider(fail=True)).compress(make_history(), keep_tokens=10))
    assert isinstance(out, list) and any(m.get("role") == "system" for m in out)
    comp = ContextCompressor(FakeProvider(reply="X" * 10000))
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