# ZervGen

An Orchestration Framework for Autonomous AI Agents.

[![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

ZervGen is a lightweight, terminal-first framework for building and orchestrating multi-agent AI systems. It is designed around a clean, modular architecture, empowering developers to create sophisticated workflows where AI agents collaborate to solve complex tasks.

The core is the **Supervisor-Executor** model. A central Supervisor agent acts as the strategic brain, deconstructing high-level goals into concrete, machine-executable plans. These plans are then carried out by a series of specialized Executor agents, turning abstract intent into tangible results.

## Features

-   **Self-Evolving:** ZervGen can generate and save its own high-quality patterns, allowing the framework to learn and expand its capabilities over time.
-   **Pattern-Driven:** Instead of simple prompts, the framework uses a library of robust, reusable patterns to ensure high-quality, predictable results.
-   **Centralized Orchestration:** A powerful Supervisor (Agent 0) deconstructs complex goals into clear, multi-step plans, enabling true task automation.
-   **Modular by Design:** A clean separation between logic, tooling, and interface makes the framework incredibly easy to extend.
-   **Extensible Tooling:** Easily integrate any external API. The initial implementation includes a robust client for the Pollinations.AI API, including YouTube transcription.

## Getting Started

### Prerequisites

-   Python 3.9 or higher

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/LeLeLeonid/ZervGen.git
    cd ZervGen
    ```

2.  **Set up a virtual environment (recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure your environment:**
    Create a `.env` file in the project root. If the file is missing, the application will guide you on the first run. Open the `.env` file to add your Pollinations.AI Bearer Token for higher rate limits.

## Usage

Run the ZervGen framework from the project's root directory:

```bash
python main.py
```

The framework operates in two primary modes: creating new patterns and using existing ones. Use the `list` command to see available patterns.

### Example Workflow 1: Creating a New Pattern

You can teach ZervGen a new skill by telling it to create a new pattern.

**Your Goal:**
> `create pattern: to summarize complex texts into three distinct formats`

**The Supervisor's Plan (Generated Auto):**
```json
[
  {
    "tool_name": "generate_text",
    "params": {
      "prompt": "# IDENTITY\nYou are the Pattern Engine... \n\n**[GOAL]:**\n`to summarize complex texts into three distinct formats`"
    }
  },
  {
    "tool_name": "generate_pattern",
    "params": {
      "pattern_name": "multi_format_summarizer",
      "explanation": "Summarizes complex texts into three distinct formats."
    }
  }
]
```
The Executor will then run these steps, creating a new `multi_format_summarizer.md` file in your patterns library and making it instantly available for use.

### Example Workflow 2: Using an Existing Pattern

Once a pattern exists, you can use it to perform tasks.

**Your Goal:**
> `use multi_format_summarizer: summarize the latest article about AI from a news site.`

The Executor will then use the `multi_format_summarizer` pattern to generate a plan and execute it, giving you the structured summary you need.

## Project Arch.

ZervGen's structure is designed for clarity and scalability.

```
ZervGen/
├── .env
├── main.py               # The entrypoint.
├── README.md             # You're here (*-*)
├── requirements.txt
└── src/
    ├── cli.py            # Manages all user interaction.
    ├── config.py         # Handles loading settings and API keys.
    ├── agents/
    │   ├── supervisor.py     # Contains Agent 0, the core planning logic.
    │   └── executors/
    │       └── tool_executor.py # Executes plans from the Supervisor.
    ├── patterns/
    │   ├── data/             # Contains the raw .md pattern files.
    │   └── explanations.json # A registry of what each pattern does.
    └── tools/
        ├── pattern_manager.py   # Loads and suggests patterns.
        └── pollinations_client.py # Client for external APIs.
```

## Roadmap

-   [ ] **Memory Implementation:** Introduce short-term memory for conversational context.
-   [ ] **Executor Expansion:** Develop new Executor agents for file system operations and web searches.
-   [ ] **Tool Abstraction:** Create a base `Tool` class to simplify adding new capabilities.
-   [ ] **Testing Suite:** Implement comprehensive unit and integration tests.
