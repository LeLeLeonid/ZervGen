import json
from pathlib import Path
import logging
from thefuzz import process

class PatternManager:
    """Loads, manages, and suggests patterns."""
    def __init__(self, patterns_dir="src/patterns"):
        self.patterns_path = Path(patterns_dir) / "data"
        self.explanations_path = Path(patterns_dir) / "explanations.json"
        self.patterns = self._load_patterns()
        self.explanations = self._load_explanations()

    def _load_patterns(self) -> dict:
        """Loads all .md files from the patterns data directory."""
        if not self.patterns_path.is_dir():
            logging.warning(f"Patterns data directory not found at '{self.patterns_path}'")
            return {}
        
        loaded = {}
        for file_path in self.patterns_path.glob("*.md"):
            try:
                name = file_path.stem
                with open(file_path, 'r', encoding='utf-8') as f:
                    loaded[name] = f.read()
            except Exception as e:
                logging.error(f"Failed to load pattern {file_path.name}: {e}")
        return loaded

    def _load_explanations(self) -> dict:
        """Loads the JSON file with pattern explanations."""
        if not self.explanations_path.exists():
            logging.warning(f"Explanations file not found at '{self.explanations_path}'")
            return {}
        try:
            with open(self.explanations_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load explanations: {e}")
            return {}

    def list_patterns(self) -> list[str]:
        """Returns a list of available pattern names."""
        return list(self.patterns.keys())

    def get_pattern_prompt(self, name: str) -> str | None:
        """Retrieves a pattern's full prompt by its name."""
        return self.patterns.get(name)

    def suggest_patterns(self, user_goal: str, limit: int = 3) -> list[str]:
        """Suggests the most relevant patterns based on the user's goal."""
        if not self.explanations:
            return []

        choices = self.explanations.keys()
        results = process.extract(user_goal, choices, limit=limit)

        return [result for result, score in results]