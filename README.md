# ZERVGEN

**Autonomous AI Orchestration**

[![Python](https://img.shields.io/badge/Python-3.10%2B-000000?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-000000?style=flat-square)](LICENSE)
[![Build](https://img.shields.io/badge/Build-Alpha-purple?style=flat-square)](https://github.com/LeLeLeonid/ZervGen)

ZervGen is a terminal-first framework implementing a **Supervisor-Worker** architecture for LLMs. It decouples the reasoning engine (Orchestrator) from execution units (Agents), enabling dynamic task routing, tool execution, and provider switching in a stateless environment.

## // CORE CAPABILITIES

*   **Supervisor Architecture:** Centralized intent analysis and task delegation.
*   **Provider Agnostic:** Native support for **Google Gemini** and **Pollinations.AI**.
*   **Dynamic Tool Registry:** Runtime injection of capabilities (Search, FS, TTS, Media).
*   **Specialized Agents:**
    *   `Coder`: File system manipulation and code synthesis.
    *   `Researcher`: Web scraping and data aggregation.
*   **Resilience:** Built-in retry logic and error handling for API instability.

## // INSTALLATION

```bash
git clone https://github.com/LeLeLeonid/ZervGen.git
cd ZervGen
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## // USAGE

Initialize the kernel. Configuration is generated on first launch.

```bash
python main.py
```

**System Commands:**
*   `1`: Enter Orchestration Loop.
*   `2`: System Configuration (Provider/Model/Key).
*   `3`: Shutdown.

## // ARCHITECTURE

Flat, modular structure designed for rapid extension.

```text
ZervGen/
├── config.json           # Runtime configuration
├── main.py               # Bootloader
├── src/
│   ├── cli.py            # UI / Input Loop
│   ├── config.py         # Data Models
│   ├── tools.py          # Function Registry
│   ├── utils.py          # Helpers
│   ├── core/
│   │   ├── orchestrator.py  # Logic Core
│   │   ├── base_agent.py    # Agent Interface
│   │   └── provider.py      # API Interface
│   ├── agents/           # Worker Units
│   │   ├── coder.py
│   │   └── researcher.py
│   ├── prompts/          # System Instructions
│   └── providers/        # API Wrappers
└── tmp/                  # Artifact Storage
```

## // WORKFLOW EXAMPLE

**Input:**
> "Analyze the current weather in Tokyo."

**Execution Flow:**
1.  **Orchestrator** receives input.
2.  **Router** detects intent: `Research` + `File I/O`.
3.  **Action 1:** Calls `get_weather("Tokyo")`.
5.  **Output:** ... .

## // ROADMAP

- [ ] **Persistent Memory:** Vector-based session storage.
- [ ] **Plugin System:** Hot-swappable agent modules.
- [ ] **Voice Input:** STT integration for full duplex voice.
- [ ] **Containerization:** Docker support.
