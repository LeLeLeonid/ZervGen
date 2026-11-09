import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.provider import AIProvider
from src.utils import ModelCatalog


CATALOG = {
    "openrouter": {
        "models": {
            "some/model": {
                "limit": {"context": 200_000},
                "cost": {"input": 1.5, "output": 6.0},
            },
            "unknown-model": {"limit": {"context": 32_000}},
        }
    },
    "google": {
        "models": {
            "gemini-test": {"limit": {"context": 1_000_000}, "cost": {"input": 0.1, "output": 0.4}},
        }
    },
}


def _catalog(monkeypatch):
    monkeypatch.setattr(ModelCatalog, "_fetch", classmethod(lambda cls, force=False: CATALOG))


def test_context_comes_from_catalog(monkeypatch):
    _catalog(monkeypatch)
    assert ModelCatalog.get_context("openrouter", "some/model") == 200_000


def test_gemini_maps_to_google_catalog(monkeypatch):
    _catalog(monkeypatch)
    assert ModelCatalog.get_context("gemini", "gemini-test") == 1_000_000


def test_rates_and_cost_are_external(monkeypatch):
    _catalog(monkeypatch)
    assert ModelCatalog.get_rates("openrouter", "some/model") == {"input": 1.5, "output": 6.0}
    cost = ModelCatalog.get_cost("openrouter", "some/model", 1_000_000, 1_000_000)
    assert cost == 7.5


def test_unknown_model_is_free(monkeypatch):
    _catalog(monkeypatch)
    assert ModelCatalog.get_rates("openrouter", "unknown-model") is None
    provider = AIProvider("https://example.test/v1", api_key="k", model="unknown-model")
    provider.name = "openrouter"
    assert provider._estimate_cost(10, 10) == 0.0
    provider._record_usage({"prompt_tokens": 10, "completion_tokens": 10})
    assert provider._last_cost == 0.0


def test_local_endpoint_is_free(monkeypatch):
    _catalog(monkeypatch)
    provider = AIProvider("http://localhost:11434/v1", api_key="k", model="some/model")
    provider.name = "openrouter"
    assert provider._estimate_cost(1000, 1000) == 0.0


def test_no_price_literals_in_source():
    pattern = re.compile(r"(\$\s*\d+\.\d+|\d+\.\d+\s*/\s*1e6|per\s*1m|usd\s*per)", re.IGNORECASE)
    offenders = []
    for path in Path("src").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "rates" not in line:
                offenders.append(f"{path}:{number}: {line.strip()}")
    assert offenders == []
