import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.base_agent import StemAgent
from src.utils import (
    CONTEXT_RESERVE_MIN,
    CONTEXT_RESERVE_SHARE,
    context_ceiling,
    count_tokens_tiktoken,
    truncate_to_tokens,
)


def test_ceiling_uses_share_above_min():
    window = 1_000_000
    assert context_ceiling(window) == window - int(window * CONTEXT_RESERVE_SHARE)


def test_ceiling_uses_min_floor_on_small_window():
    window = 32_000
    assert context_ceiling(window) == window - CONTEXT_RESERVE_MIN


def test_ceiling_never_exceeds_window():
    for window in (0, 1, 2048, 4096, 8192, 32_000, 128_000, 1_000_000):
        ceiling = context_ceiling(window)
        assert 0 <= ceiling <= window


def test_truncate_respects_token_budget():
    text = "word " * 10_000
    out = truncate_to_tokens(text, 100)
    assert count_tokens_tiktoken(out) <= 110
    assert out.endswith("[truncated]")


def test_truncate_noop_below_budget():
    text = "short text"
    assert truncate_to_tokens(text, 100) == text


def test_truncate_empty():
    assert truncate_to_tokens("", 100) == ""


class FakeProvider:
    async def generate_text(self, messages, system_prompt):
        return {"content": "S"}


class EchoCompressor:
    async def compress(self, msgs, keep_tokens=20000):
        return msgs


def make_agent(history, budget):
    agent = object.__new__(StemAgent)
    agent.history = history
    agent._cached_system_prompt = None
    agent._compressor = EchoCompressor()
    agent.provider = FakeProvider()
    agent.settings = type("S", (), {
        "history_trim_enabled": True,
        "provider": None,
        "max_token_budget": budget,
    })()
    return agent


def build_history(n=200):
    filler = "x" * 4000
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"u{i} {filler}"})
        msgs.append({"role": "assistant", "content": f"a{i} {filler}"})
        msgs.append({"role": "tool", "content": f"t{i} {filler}"})
    return msgs


def test_unknown_window_with_no_budget_skips_trim():
    history = build_history()
    before = len(history)
    agent = make_agent(history, 0)
    asyncio.run(agent._trim_history())
    assert len(agent.history) == before


def test_unknown_window_uses_configured_budget():
    agent = make_agent(build_history(), 100_000)
    before = len(agent.history)
    asyncio.run(agent._trim_history())
    assert len(agent.history) < before
