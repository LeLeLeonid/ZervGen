# ZERVGEN

**Autonomous AI Orchestration**

[![Version](https://img.shields.io/badge/Version-1.4.5-purple?style=for-the-badge)](https://github.com/LeLeLeonid/ZervGen)
[![License](https://img.shields.io/badge/License-MIT-000000?style=for-the-badge)](LICENSE)

ZervGen is a terminal-first **Secure Core** framework designed for complex task orchestration with defense-in-depth security. It decouples reasoning (The Brain) from execution (Tools) and memory (GraphRAG), featuring prompt injection protection, path traversal prevention, and sandboxed code execution.

---

## // CORE ARCHITECTURE

### 🧠 The Brain (Secure Core)
*   **Supervisor Architecture:** Decouples reasoning (Orchestrator) from execution (Agents).
*   **Security-First Design:** Prompt injection sanitization, random delimiters, path traversal protection.
*   **Provider Agnostic:** Swap between **OpenRouter** (Gemini 3, Llama), **Google Gemini**, **Pollinations.AI**, **OpenAI**, and **Anthropic** on the fly.

### 💾 The Memory (GraphRAG)
*   **Knowledge Graph:** Stores facts and relationships (`knowledge_graph.json`).
*   **Session Persistence:** Automatically saves chat history. You can travel back in time with `/load`.
*   **Self-Evolution:** The system analyzes successful interactions and crystallizes them into long-term memory.

### 🛠️ The Arsenal (Tooling)
*   **Native Tools:**
    *   `read_files` / `write_file` (Safe FS access)
    *   `web_search` (DuckDuckGo)
    *   `visit_page` (Anti-Bot Scraper)
    *   `speak` (Edge-TTS Neural Voice)
*   **Delegate:** Can spawn specialized sub-agents (`Coder`, `Researcher`, `Architect`) with unique personas.

---

## // INSTALLATION

```bash
git clone https://github.com/LeLeLeonid/ZervGen.git
cd ZervGen
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

pip install -r requirements.txt
python main.py
```

## // SECURITY FEATURES

ZervGen v1.4.5 Secure Core includes defense-in-depth protections:

*   **`sanitize_for_prompt`**: Prevents prompt injection attacks by escaping delimiter patterns.
*   **`_is_safe_path`**: Blocks path traversal attacks with directory whitelisting.
*   **`execute_command`**: Whitelist-based command execution with dangerous pattern detection.
*   **`run_safe_code`**: Sandboxed Python execution with import whitelisting and process isolation.

## // CONTROL FREAK FEATURES

ZervGen is designed for power users. Check `config.json`:

*   **`debug_mode`**: See the raw JSON thoughts of the AI before it acts.
*   **`require_approval`**: The "Leash". Force the AI to ask permission before every tool execution.
*   **`log_truncation`**: Keep your log files clean by hiding massive file dumps, while the AI still sees everything.
*   **`allowed_directories`**: Whitelist folders (like your Obsidian Vault) for the AI to access.

# // CONFIGURATION

ZervGen generates a `config.json` on first launch.
You can enable/disable specific MCP servers (Puppeteer, Filesystem) and configure API keys.

```json
"mcp_servers": {
    "puppeteer": { "enabled": true },
    "obsidian": { "args": ["...path to vault..."], "enabled": true }
}
```

## // ARCHITECTURE

The system follows a flat, modular structure designed for rapid extension.

```text
ZervGen/
├── config.json           # Runtime configuration (GitIgnored)
├── main.py               # Bootloader
├── tool_cli.py           # Debug Tool
├── src/
│   ├── cli.py            # UI / Input Loop (Rich-based)
│   ├── config.py         # Pydantic Data Models
│   ├── skills_loader.py  # Skills Loader
│   ├── tools.py          # Unified Function Registry
│   ├── utils.py          # Helpers (Retry logic, JSON parsing)
│   ├── core/
│   │   ├── orchestrator.py  # Logic Core (The Supervisor)
│   │   ├── base_agent.py    # Agent Interface
│   │   ├── mcp_manager.py  # MCPManager (false)
│   │   ├── memory.py    # Agent Interface
│   │   └── provider.py      # API Interface
│   ├── agents/           # Worker Units
│   │   ├── coder.py
│   │   └── researcher.py
│   ├── skills/           # System Instructions (Markdown)
│   └── providers/        # API Wrappers (OpenRouter, Gemini, Pollinations, OpenAI, Anthropic)
└── tmp/                  # Artifact Storage (Images, Audio)
```

## // USAGE

**Chat Loop:**
> **User:** "Research the latest news and summarize them in my Obsidian vault."
> **ZervGen:** *[Calls Web Search -> Visits Pages -> Summarizes -> Writes to Obsidian]*

**Commands:**
*   `/mode [name]` - Switch persona (e.g., `/mode architect` to plan, `/mode coder` to build).
*   `/history` - View current context window.
*   `/load` - Load a previous session.
*   `/evolve` - Force memory consolidation.

---

## // ROADMAP (2026)

- [ ] **Docker Sandboxing:** Run generated code in isolated containers.
- [ ] **Voice Interface:** Real-time STT/TTS loop (JARVIS mode).
- [ ] **Visual Cortex:** Integration with Multimodal models for screen analysis.

---

**License:** MIT