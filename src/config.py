import json
import shutil
import os
from pathlib import Path
from typing import Optional, Dict, List, Any
from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path("config.json")


def _get_valid_providers() -> List[str]:
    """Lazily discover valid provider names from src/providers/."""
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
    """Configuration for internal tool categories."""
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


class TierConfig(BaseModel):
    """Intelligence tier configuration - Spark/Core/Apex."""
    provider: str = ""  # Empty = use active provider
    model: str = ""     # Empty = use active model

# TODO: Implement in v1.5.1 
DEFAULT_TIERS = {
    "spark": TierConfig(),  # Fast/cheap - uses active provider
    "core": TierConfig(),   # Balanced - uses active provider
    "apex": TierConfig(),   # Best quality - uses active provider
}


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
    max_steps: int = 500
    tool_timeout: int = 60
    provider_timeout: int = 120
    max_history: int = 0  # 0 = unlimited
    max_pending_results: int = 0  # 0 = unlimited
    max_spawned_agents: int = 0  # 0 = unlimited
    mcp_startup_delay: float = 2.0  # seconds to wait for MCP process startup

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        valid = _get_valid_providers()
        if v not in valid:
            raise ValueError(f"Invalid provider '{v}'. Valid providers: {valid}")
        return v

    @field_validator("max_steps")
    @classmethod
    def validate_max_steps(cls, v: int) -> int:
        return max(1, v)

    history_limit: int = 50
    history_trim_enabled: bool = True
    history_trim_size: int = 20
    log_truncation: bool = True
    debug_mode: bool = False
    require_approval: bool = False
    auto_mode: bool = True
    auto_interval: int = 900
    mcp_enabled: bool = True
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=lambda: DEFAULT_MCP_SERVERS)
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
    critic_enabled: bool = True
    verbose_logging: bool = False
    parallel_execution: bool = True
    memory_enabled: bool = True
    enable_subtasks: bool = False
    enable_afk: bool = False
    afk_status: str = "idle"  # idle, running, awaiting_input
    intelligence_tiers: Dict[str, TierConfig] = Field(default_factory=lambda: DEFAULT_TIERS)

    def get_mcp_health_report(self) -> Dict[str, Any]:
        report = {}
        for name, cfg in self.mcp_servers.items():
            if not cfg.enabled:
                report[name] = {"status": "disabled", "issues": []}
                continue
            
            if cfg.command == "internal":
                report[name] = {"status": "healthy", "issues": []}
                continue
            
            issues = []
            if not cfg.is_executable_available():
                issues.append(f"Command '{cfg.command}' not found in PATH")
            env_valid, missing = cfg.validate_env_vars()
            if not env_valid:
                issues.append(f"Missing env vars: {', '.join(missing)}")
            
            report[name] = {
                "status": "healthy" if not issues else "issues",
                "issues": issues,
                "executable_available": cfg.is_executable_available(),
                "env_valid": env_valid
            }
        return report

    def save(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.model_dump(), f, indent=4)


MODES = {
    "ASK": {
        "description": "Fast, direct answers. No heavy reasoning.",
        "prompt": "MODE: [ASK]. Output the answer immediately. Do not plan. Do not use complex tool chains. Just answer.",
        "max_steps": 10,
    },
    "PLAN": {
        "description": "Deep reasoning, architectural design.",
        "prompt": "MODE: [PLAN]. Do not write code or execute actions yet. Analyze the problem, list dependencies, and outline a step-by-step strategy.",
        "max_steps": 50,
    },
    "BUILD": {
        "description": "Execution, coding, file manipulation.",
        "prompt": "MODE: [BUILD]. Execute the plan. Write files, run commands, and verify results. Be precise and complete.",
        "max_steps": 100,
    },
    "DEBUG": {
        "description": "Identify and fix issues in code or logic.",
        "prompt": "MODE: [DEBUG]. Identify and fix issues in code or logic. Use systematic debugging techniques to resolve problems.",
        "max_steps": 100,
    }
}

# Subtask and AFK prompts for orchestrator
SUBTASK_PROMPT = """Break down this task into subtasks. Return JSON array:
[{{"description": "clear subtask 1"}}, {{"description": "clear subtask 2"}}]

Task: {task}"""

AFK_STEP_PROMPT = """You are in AFK mode. Check for pending work and execute one step.
Current state: {state}
Available actions: check_n8n, check_calendar, process_queue, idle"""


def load_config() -> GlobalSettings:
    if not CONFIG_PATH.exists():
        defaults = GlobalSettings()
        defaults.save()
        return defaults

    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
        
        # Merge MCP servers with defaults (ensures new servers are added)
        default_servers = DEFAULT_MCP_SERVERS.copy()
        if "mcp_servers" in data:
            # Start with defaults, update with saved (preserves both)
            merged_servers = {**default_servers}
            for name, cfg in data.get("mcp_servers", {}).items():
                if name in merged_servers:
                    # Update existing with saved values
                    merged_servers[name] = MCPServerConfig(**cfg)
                else:
                    # Add new server
                    merged_servers[name] = MCPServerConfig(**cfg)
            data["mcp_servers"] = merged_servers
        
        return GlobalSettings.model_validate(data)
    except Exception:
        if CONFIG_PATH.exists():
            shutil.copy(CONFIG_PATH, CONFIG_PATH.with_suffix(".bak"))
        defaults = GlobalSettings()
        defaults.save()
        return defaults


def validate_config(config: GlobalSettings) -> tuple[bool, List[str]]:
    issues = []
    
    # Check API key for current provider
    provider_settings = getattr(config, config.provider, None)
    if provider_settings and hasattr(provider_settings, "api_key"):
        if not provider_settings.api_key and config.provider not in ("pollinations", "local"):
            issues.append(f"{config.provider.title()} API key is missing")
    
    # Check local provider connectivity
    if config.provider == "local":
        try:
            import httpx
            resp = httpx.get(f"{config.local.base_url}/models", timeout=5)
            if resp.status_code != 200:
                issues.append(f"Local provider not accessible at {config.local.base_url}")
        except Exception:
            issues.append(f"Local provider not accessible at {config.local.base_url}")

    # Check MCP health
    health = config.get_mcp_health_report()
    for name, status in health.items():
        if status.get("status") == "issues":
            issues.extend([f"MCP {name}: {issue}" for issue in status["issues"]])

    return len(issues) == 0, issues
