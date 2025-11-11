# ZervGen: A Multi-Agent AI Framework

[![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

ZervGen is a modular, terminal-first multi-agent AI framework designed for robust task automation and system orchestration. It is built on principles of clean architecture, providing a scalable and extensible platform for developing sophisticated AI agents.

## Features

-   **Modular Architecture:** A clean separation between the core logic, tooling, and user interface, making the framework easy to extend and maintain.
-   **Terminal-First Interface:** A powerful and responsive command-line interface powered by `rich` for an excellent user experience.
-   **Extensible Tooling:** Easily integrate with powerful external APIs. The initial implementation includes a robust client for the **Pollinations.AI API**.
-   **Configuration Management:** Simple and secure configuration using `.env` files for managing API keys and other settings.
-   **Scalable by Design:** The structure is prepared for future expansion into a true multi-agent system with supervisors and specialized executors.

## Getting Started

### Prerequisites

-   Python 3.9 or higher

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/LeLeLeonid/ZervGen.git
    cd ZervGen
    ```

2.  **Install dependencies:**
    It is highly recommended to use a virtual environment.
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    pip install -r requirements.txt
    ```

3.  **Configure Environment:**
    Create a file named `.env` in the root of the project by copying the example:
    ```bash
    cp .env.example .env
    ```
    Open the `.env` file and add your Pollinations.AI Bearer Token. This is optional for basic usage but recommended for higher rate limits.
    ```ini
    # Get a token from auth.pollinations.ai for higher limits
    POLLINATIONS_API_TOKEN="YOUR_TOKEN_HERE"
    ```

## Usage

Run the ZervGen Kernel from the project's root directory:

```bash
python main.py
```

The Kernel operates in an interactive loop. To use a tool, prefix your prompt with the tool name and a colon.

### Examples

-   **Generate text:**
    ```
    > text: Explain the concept of emergent behavior in complex systems
    ```

-   **Generate an image:**
    ```
    > image: a photorealistic image of an astronaut discovering an ancient alien library on Mars
    ```
    The generated image will be saved to the `outputs/` directory.

-   **Exit the application:**
    ```
    > exit
    ```

## Project Structure

The project follows a professional structure that separates concerns, ensuring the codebase is clean, scalable, and maintainable.

```
ZervGen/
├── .env                  # Local environment configuration (API keys)
├── README.md             # You are here
├── requirements.txt      # Project dependencies
├── main.py               # The main entry point for the application
└── src/
    ├── __init__.py
    ├── cli.py        # Handles all command-line interaction
    ├── config.py     # Manages loading settings and secrets
    ├── core/
    │   └── kernel.py   # The core "headless" logic of the application
    └── tools/
        └── pollinations_client.py # Client for interacting with external APIs
```

## Roadmap

-   [ ] Implement a Supervisor agent to deconstruct complex tasks.
-   [ ] Develop specialized Executor agents (e.g., File System Agent, Web Search Agent).
-   [ ] Introduce a message bus (e.g., Redis or RabbitMQ) for inter-agent communication.
-   [ ] Add comprehensive unit and integration tests.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
```