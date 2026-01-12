import json
import time
import os
from datetime import datetime
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
        self.current_session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.session_file = SESSIONS_DIR / f"{self.current_session_id}.jsonl"
        self.history_buffer = [] 
        
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

    def log_event(self, role: str, content: Any = "", event_type: str = "message"):
        try:
            from src.config import load_config
            cfg = load_config()
            truncate = cfg.log_truncation
        except:
            truncate = True

        disk_data = content
        
        if truncate and isinstance(content, dict) and "result" in content:
            disk_data = content.copy()
            tool = disk_data.get("tool")
            args = disk_data.get("args", {})
            result_str = str(disk_data.get("result", ""))

            if tool == "read_files":
                paths = args.get("paths", args.get("path", "unknown"))
                disk_data["result"] = f"File(s) read: {paths}"
            elif len(result_str) > 1000:
                disk_data["result"] = result_str[:200] + f" ... [TRUNCATED {len(result_str)} chars] ..."

        entry = {
            "timestamp": datetime.now().isoformat(),
            "role": role,
            "event": event_type,
            "data": disk_data
        }
        
        try:
            with open(self.session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except: pass

        if event_type in ["message", "tool_result", "thought", "final_answer"]:
            if isinstance(content, dict) and "result" in content:
                obs = f"\nTool: {content.get('tool')}\nResult: {content.get('result')}"
                self.history_buffer.append({"role": role, "content": obs})
            else:
                self.history_buffer.append({"role": role, "content": str(content)})

    def load_session_from_file(self, filename: str) -> List[Dict]:
        path = SESSIONS_DIR / filename
        if not path.exists(): return []
        reconstructed_history = []
        valid_lines = 0
        errors = 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        role = entry.get("role")
                        event = entry.get("event")
                        data = entry.get("data")
                        
                        content = ""
                        if isinstance(data, dict) and "result" in data:
                             content = f"\nTool: {data.get('tool')}\nResult: {data.get('result')}"
                        elif isinstance(data, dict) and "content" in data:
                             content = data["content"]
                        else:
                             content = str(data)

                        if event in ["message", "tool_result", "thought", "final_answer", "input"]:
                            reconstructed_history.append({"role": role, "content": content})
                        
                        valid_lines += 1
                    except json.JSONDecodeError:
                        errors += 1
                        continue
            
            self.session_file = path
            self.current_session_id = path.stem
            self.history_buffer = reconstructed_history
            
            self.log_event("system", "Session Resumed From Log", "system_event")
            
            if errors > 0:
                print(f"[Memory] Loaded {valid_lines} lines. Skipped {errors} corrupted lines.")
                
            return reconstructed_history
        except Exception as e:
            print(f"Error loading session: {e}")
            return []

    def get_recent_memories(self, limit=5) -> str:
        facts = self.kg_data.get("facts", [])
        if not facts: return "No long-term memories."
        recents = facts[-limit:]
        return "RECENT MEMORIES:\n" + "\n".join([f"- {r.get('content')}" for r in recents])

    def add_memory(self, content: str, category: str = "general", *args, **kwargs):
        fact = {
            "timestamp": time.time(),
            "content": str(content),
            "category": str(category)
        }
        self.kg_data.setdefault("facts", []).append(fact)
        self._save_kg()
        return f"Memory stored: [{category}] {content}"

    def search_memory(self, query: str, mode: str = "semantic", *args, **kwargs) -> str:
        results = []
        for fact in self.kg_data.get("facts", []):
            if query.lower() in fact.get("content", "").lower():
                results.append(f"- [{fact.get('category', 'general')}] {fact.get('content')}")
        if results: self.stats["successful_queries"] += 1
        return "FOUND MEMORIES:\n" + "\n".join(results[-10:]) if results else "No relevant memories found."

    def evolve(self) -> str:
        self.stats["evolution_events"] += 1
        facts = self.kg_data.get("facts", [])
        if len(facts) < 5: return "Not enough data."
        unique = {f["content"]: f for f in facts}.values()
        removed = len(facts) - len(unique)
        if removed > 0:
            self.kg_data["facts"] = list(unique)
            self._save_kg()
            return f"Cleaned {removed} duplicates."
        return "Memory optimal."

    def get_stats(self) -> str:
        return str(self.stats)

memory_core = MemoryManager()