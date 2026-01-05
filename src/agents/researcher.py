from src.core.base_agent import BaseAgent
from src.core.provider import AIProvider

class Researcher(BaseAgent):
    def __init__(self, provider: AIProvider):
        super().__init__("Researcher", provider, "researcher")
        self.load_tools([
            "web_search", "visit_page", "get_weather",
            "search_memory", "search_graph", "remember", "recall", "evolve_memory"
        ])