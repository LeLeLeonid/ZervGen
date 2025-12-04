from src.core.base_agent import BaseAgent
from src.core.provider import AIProvider

class Researcher(BaseAgent):
    def __init__(self, provider: AIProvider):
        super().__init__("Researcher", provider, "researcher.md")
        self.load_tools(["web_search", "visit_page", "get_weather"])