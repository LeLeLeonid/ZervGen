import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import ContextCompressor


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


def make_history(n=30):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    return msgs


def test_success_returns_list_with_summary():
    provider = FakeProvider()
    out = asyncio.run(ContextCompressor(provider).compress(make_history(), keep_tokens=10))
    assert isinstance(out, list), f"compress returned {type(out).__name__}, expected list"
    assert provider.calls == 1
    joined = " ".join(str(m.get("content", "")) for m in out)
    assert "SUMMARY" in joined


def test_success_keeps_system_message():
    out = asyncio.run(ContextCompressor(FakeProvider()).compress(make_history(), keep_tokens=10))
    assert out[0]["role"] == "system"


def test_failure_returns_original_messages():
    out = asyncio.run(ContextCompressor(FakeProvider(fail=True)).compress(make_history(), keep_tokens=10))
    assert isinstance(out, list)
    assert any(m.get("role") == "system" for m in out)


def test_previous_summary_is_not_truncated():
    provider = FakeProvider(reply="X" * 10000)
    comp = ContextCompressor(provider)
    asyncio.run(comp.compress(make_history(), keep_tokens=10))
    assert len(comp._prev) == 10000
