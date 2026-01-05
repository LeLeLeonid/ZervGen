# ZERVGEN

**Autonomous AI Orchestration**

[![Version](https://img.shields.io/badge/Version-1.2.0-purple?style=for-the-badge)](https://github.com/LeLeLeonid/ZervGen)
[![License](https://img.shields.io/badge/License-MIT-000000?style=for-the-badge)](LICENSE)

ZervGen features a self-healing ReAct loop, a GraphRAG memory core, and a plugin-based skill architecture.

---

## // NEURAL CAPABILITIES

### 🧠 The Brain (Hybrid Core)
*   **Supervisor Architecture:** Decouples reasoning (Orchestrator) from execution (Agents).
*   **Multi-Model Support:** Native integration with **OpenRouter** (Claude 3.5, Llama 3), **Google Gemini**, and **Pollinations.AI**.
*   **Self-Reflection:** The system analyzes its own actions and saves critical insights to long-term memory automatically.

### 💾 The Memory (GraphRAG)
*   **Knowledge Graph:** Stores facts and relationships, not just text vectors.
*   **Semantic Search:** Allows the agent to "recall" past projects and preferences.
*   **Self-Evolution:** The memory system evolves based on successful query patterns.

### 🛠️ The Arsenal (Dynamic Tooling)
*   **MCP Support:** Connect to external servers (GitHub, Slack, Postgres) via Model Context Protocol.
*   **Native Tools:**
    *   `web_search` (DDG) & `visit_page` (Anti-Bot Scraper)
    *   `filesystem` (Safe Read/Write/Grep)
    *   `speak` (Edge-TTS Neural Voice)
    *   `generate_image` (Pollinations Art)
*   **Rate Limiter:** Built-in protection against API abuse and cost overruns.

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

## // CONFIGURATION

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
│   └── providers/        # API Wrappers (OpenRouter, Gemini, Pollinations)
└── tmp/                  # Artifact Storage (Images, Audio)
```


## // USAGE

**Chat Loop:**
> **User:** "Research the latest news and summarize them in my Obsidian vault."
> **ZervGen:** *[Calls Web Search -> Visits Pages -> Summarizes -> Writes to Obsidian]*

**Commands:**
*   `/history` - View session context.
*   `/clear` - Reset short-term memory.
*   `/stats` - View memory graph statistics.

---

## // ROADMAP (2026)

- [ ] **Docker Sandboxing:** Run generated code in isolated containers.
- [ ] **Voice Interface:** Real-time STT/TTS loop (JARVIS mode).
- [ ] **Visual Cortex:** Integration with Multimodal models for screen analysis.

---

**License:** MIT