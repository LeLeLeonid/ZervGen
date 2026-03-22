import json
import shutil
import os
from pathlib import Path
from typing import Optional, Dict, List, Any
from functools import lru_cache
from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path("config.json")


def _get_valid_providers() -> List[str]:
    from src.core.provider import list_providers
    return [p.name for p in list_providers()]


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
        missing = [k for k, v in self.env.items() if v in ["YOUR_KEY_HERE", "YOUR_TOKEN_HERE", "...", "xoxb-...", "T..."]]
        return len(missing) == 0, missing


class ToolConfig(BaseModel):
    enabled: bool = True


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


class GroqSettings(ProviderSettings):
    _env_key = "GROQ_API_KEY"
    model: str = "llama-3.1-70b-versatile"
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 0.9


class SiliconFlowSettings(ProviderSettings):
    _env_key = "SILICONFLOW_API_KEY"
    model: str = "deepseek-ai/DeepSeek-V3"
    vision_model: str = "Qwen/Qwen2.5-VL-32B-Instruct"
    audio_model: str = "fishaudio/fish-speech-1.5"
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 0.7


class LocalSettings(ProviderSettings):
    _env_key = ""
    base_url: str = "http://localhost:1234/v1"
    model: str = "local-model"
    temperature: float = 0.7
    max_tokens: int = 4096


DEFAULT_MCP_SERVERS = {
    "ZervGen": MCPServerConfig(command="internal", args=[], enabled=True),
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
    mcp_startup_delay: float = 2.0
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=lambda: DEFAULT_MCP_SERVERS.copy())
    allowed_directories: List[str] = Field(default_factory=lambda: ["./tmp", "C:/Users/Public"])
    pollinations: PollinationsSettings = Field(default_factory=PollinationsSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)
    groq: GroqSettings = Field(default_factory=GroqSettings)
    siliconflow: SiliconFlowSettings = Field(default_factory=SiliconFlowSettings)
    local: LocalSettings = Field(default_factory=LocalSettings)
    mode: str = "BUILD"
    show_prompts: bool = False
    critic_enabled: bool = False
    hard_fail_enabled: bool = False
    verbose_logging: bool = False
    memory_enabled: bool = True

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        return v if v else "pollinations"

    @field_validator("max_steps")
    @classmethod
    def validate_max_steps(cls, v: int) -> int:
        return max(1, v)

    def get_mcp_health_report(self) -> Dict[str, Any]:
        report = {}
        for name, cfg in self.mcp_servers.items():
            if cfg.command == "internal":
                report[name] = {"status": "healthy", "issues": []}
                continue
            issues = []
            if not cfg.is_executable_available():
                issues.append(f"Command '{cfg.command}' not found in PATH")
            env_valid, missing = cfg.validate_env_vars()
            if not env_valid:
                issues.append(f"Missing env vars: {', '.join(missing)}")
            report[name] = {"status": "healthy" if not issues else "issues", "issues": issues, "executable_available": cfg.is_executable_available(), "env_valid": env_valid}
        return report

    def save(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.model_dump(), f, indent=4)


MODES = {
    "ASK": {"description": "Fast, direct answers. Use tools when needed.", "prompt": "MODE: [ASK]. Answer directly. If you need information, use tools (web_search, etc). Keep it simple - one tool call if needed, then answer.", "max_steps": 10},
    "PLAN": {"description": "Deep reasoning, architectural design.", "prompt": "MODE: [PLAN]. Do not write code or execute actions yet. Analyze the problem, list dependencies, and outline a step-by-step strategy.", "max_steps": 50},
    "BUILD": {"description": "Execution, coding, file manipulation.", "prompt": "MODE: [BUILD]. Execute the plan. Write files, run commands, and verify results. Be precise and complete.", "max_steps": 100},
    "DEBUG": {"description": "Identify and fix issues in code or logic.", "prompt": "MODE: [DEBUG]. Identify and fix issues in code or logic. Use systematic debugging techniques to resolve problems.", "max_steps": 100}
}

SUBTASK_PROMPT = """Break down this task into subtasks. Return JSON array:
[{{"description": "clear subtask 1"}}, {{"description": "clear subtask 2"}}]

Task: {task}"""

AUTO_MODE_PROMPT = """You are in AUTO mode. Check for pending work. If there's work, do it. If nothing, check memory for tasks. If nothing, respond with 'No work'."""

CRITIC_PROMPT = """You are a ruthless code reviewer (CRITIC).

RULES:
1. NEVER write code - only review
2. Check: correctness, error handling, security, test coverage
3. Look at ACTUAL file contents, not agent claims
4. Output ONLY: PASS, FAIL, NEEDS_TESTS, or ESCALATE with brief reason

FORMAT:
VERDICT: [pass/fail/needs_tests/escalate]
REASON: [1-2 sentence explanation]
DEFECTS: [list if any]"""


def load_config(force_reload: bool = False) -> GlobalSettings:
    if force_reload:
        _load_config_cached.cache_clear()
    return _load_config_cached()


@lru_cache(maxsize=1)
def _load_config_cached() -> GlobalSettings:
    return _load_config_uncached()


def _load_config_uncached() -> GlobalSettings:
    if not CONFIG_PATH.exists():
        defaults = GlobalSettings()
        defaults.save()
        return defaults

    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)

        default_servers = DEFAULT_MCP_SERVERS.copy()
        if "mcp_servers" in data:
            merged_servers = {**default_servers}
            for name, cfg in data.get("mcp_servers", {}).items():
                merged_servers[name] = MCPServerConfig(**cfg)
            data["mcp_servers"] = merged_servers

        return GlobalSettings.model_validate(data)
    except Exception:
        if CONFIG_PATH.exists():
            shutil.copy(CONFIG_PATH, CONFIG_PATH.with_suffix(".bak"))
        defaults = GlobalSettings()
        defaults.save()
        return defaults


def reload_config() -> GlobalSettings:
    _load_config_cached.cache_clear()
    return load_config(force_reload=True)
