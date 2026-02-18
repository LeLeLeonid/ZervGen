import asyncio
import json
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

try:
    import aiofiles
except ImportError:
    aiofiles = None

try:
    import chromadb
except ImportError:
    chromadb = None


class MemoryCore:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self._memory_dir = Path("tmp/memory")
        self._sessions_dir = self._memory_dir / "sessions"
        self._kg_file = self._memory_dir / "knowledge_graph.json"
        self._vector_dir = self._memory_dir / "vector_store"
        
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        
        self._short_term: deque = deque(maxlen=100)
        self._kg_data = self._load_kg()
        
        self.chroma_client = None
        self.collection = None
        if chromadb:
            try:
                self.chroma_client = chromadb.PersistentClient(path=str(self._vector_dir))
                self.collection = self.chroma_client.get_or_create_collection("zervgen_facts")
            except Exception as e:
                logger.warning(f"Vector DB init failed: {e}")
        
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_file = self._sessions_dir / f"session_{self._session_id}.jsonl"
        self._async_lock = asyncio.Lock()

    def _load_kg(self) -> Dict[str, Any]:
        if self._kg_file.exists():
            try:
                with open(self._kg_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"facts": []}

    def _save_kg(self):
        with open(self._kg_file, "w", encoding="utf-8") as f:
            json.dump(self._kg_data, f, indent=2, ensure_ascii=False)

    def add_memory(self, content: str, category: str = "general") -> str:
        fact = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": time.time(),
            "content": str(content),
            "category": str(category)
        }
        self._kg_data.setdefault("facts", []).append(fact)
        self._save_kg()
        
        self._short_term.append({
            "id": fact["id"], "content": content, "category": category,
            "timestamp": datetime.now().isoformat()
        })
        
        if self.collection:
            try:
                self.collection.add(
                    documents=[content],
                    metadatas=[{"category": category, "timestamp": fact["timestamp"]}],
                    ids=[fact["id"]]
                )
            except Exception:
                pass
        
        return f"Memory stored: [{category}] {content[:100]}"

    def search_memory(self, query: str, limit: int = 5) -> List[Dict]:
        K = 60
        semantic, keyword = [], []
        
        if self.collection:
            try:
                results = self.collection.query(query_texts=[query], n_results=limit * 2)
                if results["documents"] and results["documents"][0]:
                    semantic = [{"content": d, "rank": i + 1} for i, d in enumerate(results["documents"][0])]
            except Exception:
                pass
        
        query_lower = query.lower()
        for i, fact in enumerate(self._kg_data.get("facts", [])):
            content = fact.get("content", "")
            if query_lower in content.lower():
                keyword.append({"content": content, "rank": i + 1})
        
        rrf_scores: Dict[str, float] = {}
        for r in semantic:
            rrf_scores[r["content"]] = rrf_scores.get(r["content"], 0) + 1 / (K + r["rank"])
        for r in keyword:
            rrf_scores[r["content"]] = rrf_scores.get(r["content"], 0) + 1 / (K + r["rank"])
        
        sorted_results = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        return [{"content": c, "score": s} for c, s in sorted_results[:limit]]

    def get_recent_memories(self, limit: int = 10) -> List[Dict]:
        return list(self._short_term)[-limit:]

    def evolve(self) -> str:
        facts = self._kg_data.get("facts", [])
        if len(facts) < 5:
            return "Not enough data."
        
        seen = {f.get("content", ""): f for f in facts}
        removed = len(facts) - len(seen)
        if removed > 0:
            self._kg_data["facts"] = list(seen.values())
            self._save_kg()
            return f"Removed {removed} duplicates."
        return "Memory optimal."

    async def log_event(self, role: str, content: str, event_type: str = "message"):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "role": role, "content": content, "type": event_type
        }
        async with self._async_lock:
            try:
                if aiofiles:
                    async with aiofiles.open(self._session_file, "a", encoding="utf-8") as f:
                        await f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                else:
                    self._write_entry(entry)
            except Exception as e:
                logger.error(f"Log error: {e}")

    def log_event_sync(self, role: str, content: str, event_type: str = "message"):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "role": role, "content": content, "type": event_type
        }
        self._write_entry(entry)
    
    def log_full(self, role: str, content: str, tool: str = "", args: dict = None, title: str = ""):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent": role,
            "output": content[:2000] if content else "",
            "tool": tool,
            "input": args or {},
            "title": title,
        }
        self._write_entry(entry)
    
    def _write_entry(self, entry: Dict):
        try:
            with open(self._session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Log error: {e}")

    def _parse_entry_to_history(self, entry: Dict, history: List[Dict]) -> str:
        if entry.get("type") == "task" or entry.get("role") == "user":
            history.append({"role": "user", "content": entry.get("content", "")})
        elif entry.get("role") == "assistant":
            history.append({"role": "assistant", "content": entry.get("content", "")})
        elif entry.get("agent") and entry.get("tool"):
            agent_name = entry.get("agent", "Agent")
            tool_name = entry.get("tool", "")
            output = entry.get("output", "")
            title = entry.get("title", "")
            args = entry.get("input", {})
            
            if tool_name in ["start", "delegate_to"]:
                return entry.get("mode", "")
            
            if tool_name == "response":
                history.append({"role": "assistant", "content": output})
            else:
                json_action = json.dumps({"title": title, "tool": tool_name, "args": args})
                history.append({"role": "assistant", "content": json_action})
                history.append({"role": "user", "content": output})
        
        return entry.get("mode", "")

    async def load_session(self, filename: str) -> Tuple[List[Dict], str]:
        filepath = self._sessions_dir / filename
        if not filepath.exists():
            return [], "build"
        
        history, mode = [], "build"
        
        try:
            if aiofiles:
                async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
                    async for line in f:
                        line = line.strip()
                        if line:
                            entry = json.loads(line)
                            entry_mode = self._parse_entry_to_history(entry, history)
                            if entry_mode:
                                mode = entry_mode
            else:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            entry = json.loads(line)
                            entry_mode = self._parse_entry_to_history(entry, history)
                            if entry_mode:
                                mode = entry_mode
            
            self._session_file = filepath
            self._session_id = filename.replace("session_", "").replace(".jsonl", "")
                            
        except Exception as e:
            logger.error(f"Load session error: {e}")
        
        return history, mode

    def get_stats(self) -> Dict[str, Any]:
        return {
            "short_term_count": len(self._short_term),
            "kg_facts": len(self._kg_data.get("facts", [])),
            "vector_enabled": self.collection is not None,
            "session_id": self._session_id,
        }

    def clear_short_term(self):
        self._short_term.clear()


memory_core = MemoryCore()
