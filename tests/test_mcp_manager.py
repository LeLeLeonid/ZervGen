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


def test_public_names_qualify_only_on_collision():
    manager = MCPManager(GlobalSettings())
    assert manager._public_name("alpha", "search", False) == "mcp_search"
    assert manager._public_name("alpha", "search", True) == "mcp_alpha_search"


def test_public_name_clamps_to_64_chars():
    manager = MCPManager(GlobalSettings())
    public = manager._public_name("server", "t" * 120, True)
    assert len(public) <= 64


def test_public_name_sanitizes_unsafe_characters():
    manager = MCPManager(GlobalSettings())
    assert manager._public_name("my server", "do/it", True) == "mcp_my_server_do_it"


def test_reindex_disambiguates_duplicate_tool_names():
    manager = MCPManager(GlobalSettings())
    first = _server("alpha", command="a")
    second = _server("beta", command="b")
    for server in (first, second):
        server._connected = True
        server.tools = {"search": object()}
    manager.servers = {"alpha": first, "beta": second}
    manager._reindex()
    assert manager._calls["mcp_alpha_search"] == ("alpha", "search")
    assert manager._calls["mcp_beta_search"] == ("beta", "search")


def test_reindex_skips_disconnected_servers():
    manager = MCPManager(GlobalSettings())
    server = _server(command="a")
    server._connected = False
    server.tools = {"search": object()}
    manager.servers = {"alpha": server}
    manager._reindex()
    assert manager.tools_map == {}