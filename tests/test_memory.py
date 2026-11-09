import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.core.memory as memory_module


def _memory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(memory_module, "chromadb", None, raising=False)
    memory_module.MemoryCore._instance = None
    memory = memory_module.MemoryCore()
    return memory


def test_recall_bumps_hits_and_outcome_scoring(tmp_path, monkeypatch):
    memory = _memory(tmp_path, monkeypatch)
    asyncio.run(memory.add_memory("alpha beta gamma deployment checklist", category="lesson", source="user"))
    memory.record_outcome("alpha beta gamma", success=True)
    fact = memory._kg.get_facts()[0]
    assert fact["hits"] == 1
    assert fact["outcomes"] == 1
    assert fact["success_rate"] == 1.0


def test_failed_outcome_lowers_success_rate(tmp_path, monkeypatch):
    memory = _memory(tmp_path, monkeypatch)
    asyncio.run(memory.add_memory("alpha beta gamma deployment checklist", category="lesson", source="user"))
    memory.record_outcome("alpha beta gamma", success=False)
    fact = memory._kg.get_facts()[0]
    assert fact["success_rate"] == 0.0


def test_prune_toxic_removes_repeatedly_failing_facts(tmp_path, monkeypatch):
    memory = _memory(tmp_path, monkeypatch)
    asyncio.run(memory.add_memory("alpha beta gamma deployment checklist", category="lesson", source="user"))
    for _ in range(5):
        memory.record_outcome("alpha beta gamma", success=False)
    assert memory.prune_toxic(threshold_hits=5, success_rate=0.5) == 1
    assert memory._kg.get_facts() == []


def test_prune_toxic_keeps_healthy_facts(tmp_path, monkeypatch):
    memory = _memory(tmp_path, monkeypatch)
    asyncio.run(memory.add_memory("alpha beta gamma deployment checklist", category="lesson", source="user"))
    for _ in range(6):
        memory.record_outcome("alpha beta gamma", success=True)
    assert memory.prune_toxic(threshold_hits=5, success_rate=0.5) == 0
    assert len(memory._kg.get_facts()) == 1
