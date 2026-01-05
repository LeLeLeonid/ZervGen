import json
import time
import os
from pathlib import Path
from typing import List, Dict, Any

MEMORY_DIR = Path("tmp") / "memory"
SESSIONS_DIR = MEMORY_DIR / "sessions"
KG_FILE = MEMORY_DIR / "knowledge_graph.json"

MEMORY_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

class MemoryManager:
    def __init__(self):
        self.kg_data = self._load_kg()
        self.current_session_id = f"session_{int(time.time())}"
        self.session_file = SESSIONS_DIR / f"{self.current_session_id}.json"
        
        self.stats = {
            "total_memories": len(self.kg_data.get("facts", [])),
            "successful_queries": 0,
            "evolution_events": 0
        }

    def _load_kg(self) -> Dict[str, Any]:
        if KG_FILE.exists():
            try:
                with open(KG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return {"facts": []}
        return {"facts": []}

    def _save_kg(self):
        with open(KG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.kg_data, f, indent=2, ensure_ascii=False)
        self.stats["total_memories"] = len(self.kg_data["facts"])

    def add_memory(self, content: str, category: str = "general", *args, **kwargs):
        fact = {
            "timestamp": time.time(),
            "content": str(content),
            "category": str(category),
            "metadata": kwargs
        }
        
        if "facts" not in self.kg_data:
            self.kg_data["facts"] = []
            
        self.kg_data["facts"].append(fact)
        self._save_kg()
        return f"Memory stored: [{category}] {content}"

    def search_memory(self, query: str, mode: str = "semantic", *args, **kwargs) -> str:
        results = []
        facts = self.kg_data.get("facts", [])
        
        for fact in facts:
            if query.lower() in fact.get("content", "").lower():
                results.append(f"- [{fact.get('category', 'general')}] {fact.get('content')}")
        
        if results:
            self.stats["successful_queries"] += 1
        
        if not results: return "No relevant memories found."
        return "FOUND MEMORIES:\n" + "\n".join(results[-10:])

    def get_recent_memories(self, limit=5) -> str:
        facts = self.kg_data.get("facts", [])
        if not facts: return "No long-term memories yet."
        recents = facts[-limit:]
        return "RECENT MEMORIES:\n" + "\n".join([f"- {r.get('content')}" for r in recents])

    def save_session(self, history: List[Dict]):
        with open(self.session_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

    def evolve(self) -> str:
        self.stats["evolution_events"] += 1
        facts = self.kg_data.get("facts", [])
        count = len(facts)
        
        if count < 5:
            return "Not enough data to evolve."

        unique = {f["content"]: f for f in facts}.values()
        removed = count - len(unique)
        
        if removed > 0:
            self.kg_data["facts"] = list(unique)
            self._save_kg()
            return f"Evolution complete. Cleaned {removed} duplicates."
        return "Memory is optimal."

    def get_stats(self) -> str:
        return str(self.stats)
    
    @property
    def evolver(self):
        return self

memory_core = MemoryManager()