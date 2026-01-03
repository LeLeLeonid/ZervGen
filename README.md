# ZERVGEN

**Autonomous AI Orchestration**

[![Version](https://img.shields.io/badge/Version-1.1.1-purple?style=flat-square)](https://github.com/LeLeLeonid/ZervGen)
[![Python](https://img.shields.io/badge/Python-3.10%2B-000000?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-000000?style=flat-square)](LICENSE)

ZervGen is a terminal-first framework implementing a **Supervisor-Worker** architecture for Large Language Models. It decouples the reasoning engine (Orchestrator) from execution units (Agents), enabling dynamic task routing, autonomous tool execution, and seamless provider switching in a stateless environment.

---

## // CORE CAPABILITIES

*   **Supervisor Architecture:** Centralized intent analysis and task delegation using a robust ReAct (Reasoning + Acting) loop.
*   **Multi-Provider Support:**
    *   **OpenRouter:** Access to Claude, Llama, DeepSeek, and more.
    *   **Google Gemini:** Native integration with Flash/Pro models.
    *   **Pollinations.AI:** Fallback support for free text and media generation.
*   **Dynamic Tool Registry:** Runtime injection of capabilities including Web Search (DDG), File System operations, TTS (Edge-TTS), and Media generation.
*   **Specialized Agents:**
    *   `@Coder`: File system manipulation, code synthesis, and command execution.
    *   `@Researcher`: Web scraping, weather analysis, and data aggregation.
*   **Resilience:** Built-in cognitive retry logic, smart history trimming, and anti-bot measures for web scraping.

## // INSTALLATION

```bash
# 1. Clone repository
git clone https://github.com/LeLeLeonid/ZervGen.git
cd ZervGen

# 2. Virtual Environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Dependencies
pip install -r requirements.txt
```

## // CONFIGURATION

On the first launch, ZervGen generates a `config.json` file. You can configure it via the CLI menu or manually.

**Supported Providers:**
1.  **OpenRouter:** Requires API Key. Recommended for best performance.
2.  **Gemini:** Requires Google API Key. High speed and large context.
3.  **Pollinations:** No key required. Good for testing and media generation.

## // USAGE

Initialize the kernel:

```bash
python main.py
```

**System Commands:**
*   `/history`: View current session context.
*   `/time`: Check system time.
*   `/clear`: Flush short-term memory.

**CLI Menu:**
*   `[1] Start Chat`: Enter the orchestration loop.
*   `[2] Configuration`: Hot-swap providers and models.
*   `[3] Exit`: Terminate session.

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
│   ├── tools.py          # Unified Function Registry
│   ├── utils.py          # Helpers (Retry logic, JSON parsing)
│   ├── core/
│   │   ├── orchestrator.py  # Logic Core (The Supervisor)
│   │   ├── base_agent.py    # Agent Interface
│   │   └── provider.py      # API Interface
│   ├── agents/           # Worker Units
│   │   ├── coder.py
│   │   └── researcher.py
│   ├── prompts/          # System Instructions (Markdown)
│   └── providers/        # API Wrappers (OpenRouter, Gemini, Pollinations)
└── tmp/                  # Artifact Storage (Images, Audio)
```

## // WORKFLOW EXAMPLE

**Input:**
> "Analyze the current weather in Tokyo and write a report to weather.txt"

**Execution Flow:**
1.  **Orchestrator** receives input and initializes ReAct loop.
2.  **Router** detects intent: `Research` + `File I/O`.
3.  **Action 1:** Calls `get_weather("Tokyo")` via Tool Registry.
4.  **Action 2:** Delegates to `@Coder` agent to write file.
5.  **Output:** Confirms execution to user.

## // ROADMAP

- [ ] **Persistent Memory:** Vector-based session storage for long-term context.
- [ ] **Plugin System:** Hot-swappable agent modules via drag-and-drop.
- [ ] **Voice Input:** STT integration for full duplex voice interaction.
- [ ] **Dockerization:** Containerized deployment.

---

**License:** MIT