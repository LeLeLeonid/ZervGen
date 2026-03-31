import json
import shutil
import os
from pathlib import Path
from typing import Optional, Dict, List, Any
from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path("config.json")
ENV_PATH = Path.home() / ".zervgen" / ".env"
ALLOWED_ROOTS_PATH = Path.home() / ".zervgen" / "allowed_roots.json"


def _load_env():
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


_load_env()


class MCPServerConfig(BaseModel):
    command: str
    args: List[str]
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = False

    def is_executable_available(self) -> bool:
        cmd = self.command
        if cmd == "npx" and os.name == "nt":
            return shutil.which("npx") is not None or shutil.which("npx.cmd") is not None
        return shutil.which(cmd) is not None

    def validate_env_vars(self) -> tuple[bool, List[str]]:
        missing = [k for k, v in self.env.items() if not v or v in ["YOUR_KEY_HERE", "YOUR_TOKEN_HERE", "...", "xoxb-...", "T..."]]
        return len(missing) == 0, missing


class ProviderSettings(BaseModel):
    api_key: Optional[str] = None
    _env_key: str = ""

    def model_post_init(self, __context):
        if not self.api_key and self._env_key:
            self.api_key = os.environ.get(self._env_key)


class PollinationsSettings(ProviderSettings):
    _env_key = ""
    text_model: str = "openai"
    image_model: str = "flux"
    audio_model: str = "openai-audio"
    voice: str = "nova"
    reasoning_effort: str = "minimal"
    image_width: int = 1024
    image_height: int = 1024
    output_path: str = "tmp"


class GeminiSettings(ProviderSettings):
    _env_key = "GEMINI_API_KEY"
    model: str = "gemini-2.0-flash"
    temperature: float = 0.7


class OpenRouterSettings(ProviderSettings):
    _env_key = "OPENROUTER_API_KEY"
    model: str = "google/gemini-2.0-flash-exp:free"
    vision_model: str = "allenai/molmo-2-8b:free"
    site_url: str = "https://github.com/LeLeLeonid/ZervGen"
    app_name: str = "ZervGen"


class OpenAISettings(ProviderSettings):
    _env_key = "OPENAI_API_KEY"
    model: str = "gpt-5.2"


class AnthropicSettings(ProviderSettings):
    _env_key = "ANTHROPIC_API_KEY"
    model: str = "claude-sonnet-4.5"


DEFAULT_MCP_SERVERS = {
    "filesystem": MCPServerConfig(command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "."], enabled=False),
    "git": MCPServerConfig(command="python", args=["-m", "mcp_server_git"], enabled=False),
    "fetch": MCPServerConfig(command="python", args=["-m", "mcp_server_fetch"], enabled=False),
    "sequential-thinking": MCPServerConfig(command="npx", args=["-y", "@modelcontextprotocol/server-sequential-thinking"], enabled=False),
    "everything": MCPServerConfig(command="npx", args=["-y", "@modelcontextprotocol/server-everything"], enabled=False),
    "puppeteer": MCPServerConfig(command="npx", args=["-y", "@modelcontextprotocol/server-puppeteer"], enabled=False),
}


class GlobalSettings(BaseModel):
    provider: str = "pollinations"
    max_steps: int = 50
    tool_timeout: int = 60
    provider_timeout: int = 120
    max_history: int = 0
    max_pending_results: int = 0
    max_spawned_agents: int = 0
    history_limit: int = 50
    history_trim_enabled: bool = True
    history_trim_size: int = 20
    log_truncation: bool = True
    debug_mode: bool = False
    require_approval: bool = False
    auto_mode: bool = True
    auto_interval: int = 900
    mcp_enabled: bool = True
    mcp_startup_delay: float = 1.0
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=lambda: {k: MCPServerConfig(**v.model_dump()) for k, v in DEFAULT_MCP_SERVERS.items()})
    allowed_directories: List[str] = Field(default_factory=lambda: ["./tmp", "C:/Users/Public"])
    pollinations: PollinationsSettings = Field(default_factory=PollinationsSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)
    mode: str = "BUILD"
    show_prompts: bool = False
    critic_enabled: bool = False
    memory_enabled: bool = True
    dream_interval: int = 300
    accent_color: str = "purple"

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        return v if v else "pollinations"

    @field_validator("max_steps")
    @classmethod
    def validate_max_steps(cls, v: int) -> int:
        return max(1, v)

    def save(self):
        data = self.model_dump()
        for prov in ("pollinations", "gemini", "openrouter", "openai", "anthropic"):
            if prov in data and isinstance(data[prov], dict):
                data[prov].pop("api_key", None)
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=4)


MODES = {
    "ASK": {"description": "Fast, direct answers. Use tools when needed.", "prompt": "MODE: [ASK]. Answer directly. If you need information, use tools (web_search, etc). Keep it simple - one tool call if needed, then answer.", "max_steps": 10},
    "PLAN": {"description": "Deep reasoning, architectural design.", "prompt": "MODE: [PLAN]. Do not write code or execute actions yet. Analyze the problem, list dependencies, and outline a step-by-step strategy.", "max_steps": 50},
    "BUILD": {"description": "Execution, coding, file manipulation.", "prompt": "MODE: [BUILD]. Execute the plan. Write files, run commands, and verify results. Be precise and complete.", "max_steps": 100},
    "DEBUG": {"description": "Identify and fix issues in code or logic.", "prompt": "MODE: [DEBUG]. Identify and fix issues in code or logic. Use systematic debugging techniques to resolve problems.", "max_steps": 100}
}

COMPATIBLE_PRESETS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "headers_fn": lambda key: {"Authorization": f"Bearer {key or os.environ.get('GROQ_API_KEY', '')}"},
        "default_model": "llama-3.1-70b-versatile",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1/chat/completions",
        "headers_fn": lambda key: {"Authorization": f"Bearer {key or os.environ.get('SILICONFLOW_API_KEY', '')}"},
        "default_model": "deepseek-ai/DeepSeek-V3",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1/chat/completions",
        "headers_fn": lambda key: {},
    },
    "lmstudio": {
        "base_url": "http://localhost:1234/v1/chat/completions",
        "headers_fn": lambda key: {},
    },
}

CRITIC_PROMPT = "You are a code reviewer. Check: correctness, error handling, security. Output: PASS/FAIL + 1-2 sentence reason."


def _load_config_impl() -> GlobalSettings:
    if not CONFIG_PATH.exists():
        defaults = GlobalSettings()
        defaults.save()
        return defaults

    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)

        default_servers = {k: MCPServerConfig(**v.model_dump()) for k, v in DEFAULT_MCP_SERVERS.items()}
        if "mcp_servers" in data:
            merged_servers = {**default_servers}
            for name, cfg in data.get("mcp_servers", {}).items():
                if name == "ZervGen":
                    continue
                merged_servers[name] = MCPServerConfig(**cfg)
            data["mcp_servers"] = merged_servers

        return GlobalSettings.model_validate(data)
    except Exception:
        if CONFIG_PATH.exists():
            shutil.copy(CONFIG_PATH, CONFIG_PATH.with_suffix(".bak"))
        defaults = GlobalSettings()
        defaults.save()
        return defaults


_config_instance: Optional[GlobalSettings] = None


def load_config() -> GlobalSettings:
    global _config_instance
    if _config_instance is None:
        _config_instance = _load_config_impl()
    return _config_instance


def save_env_key(provider_name: str, api_key: str) -> None:
    provider_settings = getattr(_config_instance, provider_name, None) if _config_instance else None
    env_key = getattr(provider_settings, '_env_key', '') if provider_settings else ''
    if not env_key:
        return
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{env_key}="):
            new_lines.append(f'{env_key}="{api_key}"')
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f'{env_key}="{api_key}"')
    ENV_PATH.write_text("\n".join(new_lines) + "\n")
    os.environ[env_key] = api_key


def get_allowed_roots() -> List[str]:
    if ALLOWED_ROOTS_PATH.exists():
        try:
            return json.loads(ALLOWED_ROOTS_PATH.read_text())
        except Exception:
            pass
    return []


def add_allowed_root(path: str) -> str:
    p = Path(path).resolve()
    if not p.exists():
        return f"Error: Path does not exist: {p}"
    ALLOWED_ROOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = get_allowed_roots()
    if str(p) in existing:
        return f"Already allowed: {p}"
    existing.append(str(p))
    ALLOWED_ROOTS_PATH.write_text(json.dumps(existing, indent=2))
    from src.tools import reload_allowed_roots
    reload_allowed_roots()
    return f"Added: {p}"


def remove_allowed_root(index: int) -> str:
    existing = get_allowed_roots()
    if 0 <= index < len(existing):
        removed = existing.pop(index)
        ALLOWED_ROOTS_PATH.write_text(json.dumps(existing, indent=2))
        from src.tools import reload_allowed_roots
        reload_allowed_roots()
        return f"Removed: {removed}"
    return f"Error: Invalid index {index}"
