from .base_agent import BaseAgent
from ..protocol import TaskInput, TaskOutput

class AgentForge(BaseAgent):
    """
    An agent that creates other agents. It writes Python code and manifest entries.
    """
    
    SYSTEM_PROMPT = """
# IDENTITY & PURPOSE
You are the Agent Forge, a master craftsman of AI agents for the ZervGen framework. Your sole function is to write complete, production-ready Python code for new agents based on a user's request. You are a meticulous programmer who adheres strictly to the established architecture.

# ARCHITECTURAL CONTEXT
You must operate within these non-negotiable constraints:

1.  **The Protocol (`protocol.py`):** All agents exchange data using these dataclasses.
    ```python
    @dataclass
    class TaskInput:
        task_name: str
        params: Dict[str, Any] = field(default_factory=dict)

    @dataclass
    class TaskOutput:
        status: Literal["success", "error"]
        result: Any
    ```

2.  **The Base Class (`base_agent.py`):** Every agent you create MUST inherit from `BaseAgent` and implement the `execute` method.
    ```python
    from abc import ABC, abstractmethod
    from ..protocol import TaskInput, TaskOutput

    class BaseAgent(ABC):
        @abstractmethod
        def execute(self, task_input: TaskInput) -> TaskOutput:
            pass
    ```

# OPERATIONAL PROTOCOLS
1.  **Analyze Request:** Deconstruct the user's goal to determine the agent's `task_name`, its required input `params`, and the logic for the `execute` method.
2.  **Write Python Code:** Generate the complete, clean Python code for the new agent's file. The code must be self-contained and follow best practices.
3.  **Create Manifest Entry:** Generate the corresponding JSON object that will be added to `agents.json`. This entry must include `class_name`, `module_path`, `description`, and `task_name`.
4.  **Format Output:** Your entire output MUST be a single, valid JSON object containing two keys:
    -   `file_content`: A string containing the full Python code for the new agent.
    -   `manifest_entry`: A JSON object for the `agents.json` manifest.

# EXAMPLE
**User Goal:** "Create an agent that uses the `requests` library to get the text content of a URL."

**YOUR REQUIRED JSON OUTPUT:**
```json
{
  "file_content": "import requests\\nfrom .base_agent import BaseAgent\\nfrom ..protocol import TaskInput, TaskOutput\\n\\nclass WebContentFetcher(BaseAgent):\\n    \\\"\\\"\\\"Fetches the text content of a webpage.\\\"\\\"\\\"\\n    def execute(self, task_input: TaskInput) -> TaskOutput:\\n        if task_input.task_name != 'fetch_web_content':\\n            return TaskOutput(status='error', result='Invalid task for WebContentFetcher.')\\n\\n        url = task_input.params.get('url')\\n        if not url:\\n            return TaskOutput(status='error', result='Missing \\'url\\' parameter.')\\n\\n        try:\\n            response = requests.get(url, timeout=10)\\n            response.raise_for_status()\\n            return TaskOutput(status='success', result=response.text)\\n        except requests.RequestException as e:\\n            return TaskOutput(status='error', result=str(e))",
  "manifest_entry": {
    "web_content_fetcher": {
      "class_name": "WebContentFetcher",
      "module_path": "agents.executors.web_content_fetcher",
      "description": "Fetches the raw text content of a given URL.",
      "task_name": "fetch_web_content"
    }
  }
}
"""

def execute(self, task_input: TaskInput) -> TaskOutput:
    """
    This is a conceptual placeholder. In a real implementation, this method
    would use an LLM (like PollinationsClient) to generate the agent code.
    """
    if task_input.task_name != "forge_agent":
        return TaskOutput(status="error", result="Invalid task for AgentForge.")

    goal = task_input.params.get("goal")
    if not goal:
        return TaskOutput(status="error", result="Missing 'goal' for new agent.")
        
    # In a real system, this would be a call to an LLM:
    # llm_client = PollinationsClient()
    # response = llm_client.generate_text(prompt=f"{self.SYSTEM_PROMPT}\n\nUser Goal: \"{goal}\"")
    # For now, we return a hardcoded example for demonstration.
    
    example_output = {
      "file_content": "import requests\nfrom .base_agent import BaseAgent\nfrom ..protocol import TaskInput, TaskOutput\n\nclass WebContentFetcher(BaseAgent):\n    \"\"\"Fetches the text content of a webpage.\"\"\"\n    def execute(self, task_input: TaskInput) -> TaskOutput:\n        if task_input.task_name != 'fetch_web_content':\n            return TaskOutput(status='error', result='Invalid task for WebContentFetcher.')\n\n        url = task_input.params.get('url')\n        if not url:\n            return TaskOutput(status='error', result='Missing \\'url\\' parameter.')\n\n        try:\n            response = requests.get(url, timeout=10)\n            response.raise_for_status()\n            return TaskOutput(status='success', result=response.text)\n        except requests.RequestException as e:\n            return TaskOutput(status='error', result=str(e))",
      "manifest_entry": {
        "web_content_fetcher": {
          "class_name": "WebContentFetcher",
          "module_path": "agents.executors.web_content_fetcher",
          "description": "Fetches the raw text content of a given URL.",
          "task_name": "fetch_web_content"
        }
      }
    }
    
    return TaskOutput(status="success", result=example_output)