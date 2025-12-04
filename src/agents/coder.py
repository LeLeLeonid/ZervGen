from src.core.base_agent import BaseAgent
from src.core.provider import AIProvider

class Coder(BaseAgent):
    def __init__(self, provider: AIProvider):
        super().__init__("Coder", provider, "coder.md")
        self.load_tools(["read_file", "write_file", "list_dir", "execute_command"])