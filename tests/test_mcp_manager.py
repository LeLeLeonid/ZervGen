import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import GlobalSettings, MCPServerConfig
from src.core.mcp_manager import MCPServer, MCPManager, _expand


def _server(name: str = "demo", **kwargs) -> MCPServer:
    return MCPServer(name, MCPServerConfig(**kwargs))


def test_env_reference_expands_from_environment(monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", "abc123")
    assert _expand("Bearer ${DEMO_TOKEN}", "headers") == "Bearer abc123"


def test_unset_env_reference_is_a_hard_error(monkeypatch):
    monkeypatch.delenv("DEMO_MISSING", raising=False)
    with pytest.raises(RuntimeError, match="DEMO_MISSING"):
        _expand("${DEMO_MISSING}", "demo.env")


def test_kind_infers_transport_from_fields():
    assert MCPServerConfig(command="npx", args=["-y", "pkg"]).kind == "stdio"
    assert MCPServerConfig(url="https://example.test/mcp").kind == "http"
    assert MCPServerConfig(url="https://example.test/sse").kind == "sse"
    assert MCPServerConfig(url="https://example.test/mcp", transport="sse").kind == "sse"
    assert MCPServerConfig(command="x", transport="http", url="https://example.test/x").kind == "http"


def test_tool_filters_admit_and_reject():
    server = _server(command="x", tools_include=["git_*"], tools_exclude=["git_push"])
    assert server._wanted("git_status") is True
    assert server._wanted("read_file") is False
    assert server._wanted("git_push") is False


def test_public_names_are_namespaced_by_server():
    manager = MCPManager(GlobalSettings())
    assert manager._public_name("alpha", "search") == "mcp__alpha__search"


def test_public_name_clamps_to_64_chars():
    manager = MCPManager(GlobalSettings())
    public = manager._public_name("server", "t" * 120)
    assert len(public) <= 64


def test_public_name_sanitizes_unsafe_characters():
    manager = MCPManager(GlobalSettings())
    assert manager._public_name("my server", "do/it") == "mcp__my_server__do_it"


def test_reindex_namespaces_every_tool_and_keeps_a_unique_alias():
    manager = MCPManager(GlobalSettings())
    first = _server("alpha", command="a")
    second = _server("beta", command="b")
    for server in (first, second):
        server._connected = True
        server.tools = {"search": object()}
    first.tools["read"] = object()
    manager.servers = {"alpha": first, "beta": second}
    manager._reindex()
    assert manager._calls["mcp__alpha__search"] == ("alpha", "search")
    assert manager._calls["mcp__beta__search"] == ("beta", "search")
    assert manager._calls["mcp__alpha__read"] == ("alpha", "read")
    assert "mcp_search" not in manager._calls
    assert manager._calls["mcp_read"] == ("alpha", "read")
    assert manager.tools_map["mcp__alpha__search"] == "alpha"


def test_reindex_skips_disconnected_servers():
    manager = MCPManager(GlobalSettings())
    server = _server(command="a")
    server._connected = False
    server.tools = {"search": object()}
    manager.servers = {"alpha": server}
    manager._reindex()
    assert manager.tools_map == {}


def test_failed_server_keeps_its_catalog_and_backs_off():
    from src.core.mcp_manager import BACKOFF_START

    server = _server(command="a")
    server.tools = {"search": object()}
    server._record_failure()
    assert server.state == "degraded"
    assert server.in_backoff
    assert 0 < server.retry_in <= BACKOFF_START
    server._record_failure()
    assert server.retry_in > BACKOFF_START
    assert server.tools, "catalog must survive a failed reconnect"


def test_server_status_reports_state_and_retry():
    manager = MCPManager(GlobalSettings())
    server = _server(command="a")
    manager.servers = {"alpha": server}
    server._record_failure()
    status = manager.server_status()["alpha"]
    assert status["state"] == "degraded"
    assert status["connected"] is False
    assert status["retry_in"] > 0

def test_degraded_server_keeps_serving_its_cached_names():
    manager = MCPManager(GlobalSettings())
    server = _server("alpha", command="a")
    server._connected = True
    server.tools = {"search": object()}
    manager.servers = {"alpha": server}
    manager._reindex()
    server._record_failure()
    manager._reindex()
    assert manager._calls["mcp__alpha__search"] == ("alpha", "search")


def test_agent_connects_servers_on_first_turn(tmp_path, monkeypatch):
    import asyncio

    from src.core.base_agent import StemAgent

    monkeypatch.chdir(tmp_path)
    settings = GlobalSettings()
    settings.memory_enabled = False
    settings.mcp_servers = {"alpha": MCPServerConfig(command="a", enabled=True)}
    agent = StemAgent(name="system", settings=settings, mode="BUILD")
    calls = []

    async def fake_connect():
        calls.append(1)
        return {}

    monkeypatch.setattr(MCPManager, "connect_all", lambda self: fake_connect())
    asyncio.run(agent._init_mcp())
    assert calls == [1]
    agent._mcp_initialized = False
    agent._mcp = None
    MCPManager._instance = None


def test_agent_skips_connect_when_disabled(tmp_path, monkeypatch):
    import asyncio

    from src.core.base_agent import StemAgent

    monkeypatch.chdir(tmp_path)
    settings = GlobalSettings()
    settings.memory_enabled = False
    settings.mcp_connect_on_start = False
    settings.mcp_servers = {"alpha": MCPServerConfig(command="a", enabled=True)}
    agent = StemAgent(name="system", settings=settings, mode="BUILD")
    called = []
    monkeypatch.setattr(MCPManager, "connect_all", lambda self: called.append(1))
    asyncio.run(agent._init_mcp())
    assert called == []
    MCPManager._instance = None
