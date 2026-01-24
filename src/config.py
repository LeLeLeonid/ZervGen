import json
import shutil
import os
from pathlib import Path
from typing import Literal, Optional, Dict, List, Any
from pydantic import BaseModel, Field

CONFIG_PATH = Path("config.json")

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
        missing_vars = []
        for key, value in self.env.items():
            if value in ["YOUR_KEY_HERE", "YOUR_TOKEN_HERE", "...", "xoxb-...", "T...", "..."]:
                missing_vars.append(key)
        return len(missing_vars) == 0, missing_vars

class PollinationsSettings(BaseModel):
    api_key: Optional[str] = None
    text_model: str = "openai"
    image_model: str = "flux"
    audio_model: str = "openai-audio"
    voice: str = "nova"
    reasoning_effort: str = "minimal"
    image_width: int = 1024
    image_height: int = 1024
    output_path: str = "tmp"

class GeminiSettings(BaseModel):
    api_key: Optional[str] = None
    model: str = "gemini-2.0-flash"
    temperature: float = 0.7

class OpenRouterSettings(BaseModel):
    api_key: Optional[str] = None
    model: str = "google/gemini-2.0-flash-exp:free"
    vision_model: str = "allenai/molmo-2-8b:free"
    site_url: str = "https://github.com/LeLeLeonid/ZervGen"
    app_name: str = "ZervGen"

class OpenAISettings(BaseModel):
    api_key: Optional[str] = None
    model: str = "gpt-5.2"

class AnthropicSettings(BaseModel):
    api_key: Optional[str] = None
    model: str = "claude-sonnet-4.5"

DEFAULT_MCP_SERVERS = {
    "filesystem": MCPServerConfig(
        command="npx", 
        args=["-y", "@modelcontextprotocol/server-filesystem", "."], 
        enabled=True
    ),
    "n8n": MCPServerConfig(
        command="npx", 
        args=[
            "-y", "supergateway", 
            "--streamableHttp", "http://localhost:5678/api/v1/docs/swagger.json",
            "--header", "X-N8N-API-KEY: YOUR_KEY_HERE"
        ], 
        enabled=True
    ),
    "git": MCPServerConfig(
        command="npx", 
        args=["-y", "@modelcontextprotocol/server-git"], 
        enabled=True
    ),
    "puppeteer": MCPServerConfig(
        command="npx", 
        args=["-y", "@modelcontextprotocol/server-puppeteer"], 
        enabled=True
    ),
    "fetch": MCPServerConfig(
        command="npx", 
        args=["-y", "@modelcontextprotocol/server-fetch"], 
        enabled=True
    ),
    "memory": MCPServerConfig(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
        enabled=True
    )
}

class GlobalSettings(BaseModel):
    provider: Literal["pollinations", "gemini", "openrouter", "openai", "anthropic"] = "pollinations"
    max_steps: int = 500
    history_limit: int = 50
    log_truncation: bool = True
    debug_mode: bool = False
    require_approval: bool = False
    mcp_enabled: bool = True
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=lambda: DEFAULT_MCP_SERVERS)
    allowed_directories: List[str] = Field(
        default_factory=lambda: ["./tmp", "C:/Users/Public"]
    )
    pollinations: PollinationsSettings = Field(default_factory=PollinationsSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)
    mode: str = "BUILD"
    
    def get_mcp_health_report(self) -> Dict[str, Any]:
        report = {}
        if not self.mcp_enabled:
            return {"global": {"status": "disabled", "issues": ["MCP Globally Disabled"]}}
            
        for name, config in self.mcp_servers.items():
            if not config.enabled:
                report[name] = {"status": "disabled", "issues": []}
                continue
            
            issues = []
            if not config.is_executable_available():
                issues.append(f"Command '{config.command}' not found in PATH")
            
            env_valid, missing_vars = config.validate_env_vars()
            if not env_valid:
                issues.append(f"Missing environment variables: {', '.join(missing_vars)}")
            
            report[name] = {
                "status": "healthy" if not issues else "issues",
                "issues": issues,
                "executable_available": config.is_executable_available(),
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
        "max_steps": 1
    },
    "PLAN": {
        "description": "Deep reasoning, architectural design.",
        "prompt": "MODE: [PLAN]. Do not write code or execute actions yet. Analyze the problem, list dependencies, and outline a step-by-step strategy.",
        "max_steps": 500
    },
    "BUILD": {
        "description": "Execution, coding, file manipulation.",
        "prompt": "MODE: [BUILD]. Execute the plan. Write files, run commands, and verify results. Be precise and complete.",
        "max_steps": None
    },
    "DEBUG": {
        "description": "Identify and fix issues in code or logic.",
        "prompt": "MODE: [DEBUG]. Identify and fix issues in code or logic. Use systematic debugging techniques to resolve problems.",
        "max_steps": None
    }
}


def load_config() -> GlobalSettings:
    if not CONFIG_PATH.exists():
        print("[System] Config not found. Generating default config.json...")
        defaults = GlobalSettings()
        defaults.save()
        return defaults
    
    try:
        with open(CONFIG_PATH, "r") as f:
            return GlobalSettings.model_validate(json.load(f))
    except Exception:
        if CONFIG_PATH.exists():
            shutil.copy(CONFIG_PATH, CONFIG_PATH.with_suffix(".bak"))
        defaults = GlobalSettings()
        defaults.save()
        return defaults

def validate_config(config: GlobalSettings) -> tuple[bool, List[str]]:
    issues = []
    
    if config.provider == "gemini" and not config.gemini.api_key:
        issues.append("Gemini API key is missing")
    elif config.provider == "openrouter" and not config.openrouter.api_key:
        issues.append("OpenRouter API key is missing")
    elif config.provider == "openai" and not config.openai.api_key:
        issues.append("OpenAI API key is missing")
    elif config.provider == "anthropic" and not config.anthropic.api_key:
        issues.append("Anthropic API key is missing")
    
    health = config.get_mcp_health_report()
    for name, status in health.items():
        if status.get("status") == "issues":
            issues.extend([f"MCP {name}: {issue}" for issue in status["issues"]])
    
    return len(issues) == 0, issues