import json
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from queue import Queue

MAX_SHORT_TERM = 100

logger = logging.getLogger(__name__)
chromadb = None


class KnowledgeGraph:
    def __init__(self, kg_file: Path):
        self._kg_file = kg_file
        self._data: Dict[str, Any] = {"facts": []}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self._kg_file.exists():
            try:
                with open(self._kg_file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load KG: {e}")
                self._data = {"facts": []}

    def save(self) -> None:
        with self._lock:
            try:
                self._kg_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._kg_file, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Failed to save KG: {e}")

    def add_fact(self, fact: Dict) -> None:
        with self._lock:
            self._data.setdefault("facts", []).append(fact)

    def get_facts(self, tier: Optional[str] = None) -> List[Dict]:
        with self._lock:
            facts = self._data.get("facts", [])
            return [f for f in facts if f.get("tier") == tier] if tier else list(facts)

    def dedupe(self) -> int:
        with self._lock:
            seen = set()
            unique = []
            for f in self._data.get("facts", []):
                content = f.get("content")
                if content not in seen:
                    seen.add(content)
                    unique.append(f)
            removed = len(self._data.get("facts", [])) - len(unique)
            self._data["facts"] = unique
            return removed


class VectorStore:
    def __init__(self, vector_dir: Path):
        self._vector_dir = vector_dir
        self._client = None
        self._collection = None
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        global chromadb
        if chromadb is None:
            try:
                import chromadb as _chromadb
                chromadb = _chromadb
            except ImportError:
                logger.warning("ChromaDB not installed")
                return
        try:
            self._vector_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self._vector_dir))
            self._collection = self._client.get_or_create_collection("zervgen_facts")
        except Exception as e:
            logger.warning(f"VectorStore init failed: {e}")

    def add(self, documents: List[str], metadatas: List[Dict], ids: List[str]) -> None:
        if not self._collection:
            return
        with self._lock:
            try:
                self._collection.add(documents=documents, metadatas=metadatas, ids=ids)
            except Exception as e:
                logger.debug(f"Vector add failed: {e}")

    def search(self, query: str, n: int = 10) -> List[Dict]:
        if not self._collection:
            return []
        with self._lock:
            try:
                results = self._collection.query(query_texts=[query], n_results=n)
                if results["documents"] and results["documents"][0]:
                    return [{"content": d, "rank": i + 1} for i, d in enumerate(results["documents"][0])]
            except Exception as e:
                logger.debug(f"Vector search failed: {e}")
        return []


class MemoryCore:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True

        self._memory_dir = Path("tmp/memory")
        self._sessions_dir = self._memory_dir / "sessions"
        self._kg_file = self._memory_dir / "knowledge_graph.json"
        self._vector_dir = self._memory_dir / "vector_store"

        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        self._short_term: deque = deque(maxlen=MAX_SHORT_TERM)
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_file = self._sessions_dir / f"session_{self._session_id}.jsonl"
        self._file_lock = threading.Lock()

        self._kg = KnowledgeGraph(self._kg_file)
        self._vector = VectorStore(self._vector_dir)

        self._save_queue: Queue = Queue()
        self._save_thread = threading.Thread(target=self._save_worker, daemon=True)
        self._save_thread.start()

    def _save_worker(self) -> None:
        while True:
            try:
                self._save_queue.get()
                self._kg.save()
            except Exception as e:
                logger.error(f"Save worker error: {e}")

    def _create_fact(self, content: str, category: str, tier: str) -> Dict:
        return {
            "id": str(uuid.uuid4())[:8],
            "timestamp": time.time(),
            "content": str(content),
            "category": str(category),
            "tier": tier
        }

    def add_memory(self, content: str, category: str = "general", tier: str = "now") -> str:
        fact = self._create_fact(content, category, tier)

        self._short_term.append({
            "id": fact["id"], "content": content, "category": category,
            "timestamp": datetime.now().isoformat(), "tier": tier
        })

        if tier in ("recent", "long_term"):
            self._kg.add_fact(fact)
            self._save_queue.put(True)

        self._vector.add(
            documents=[content],
            metadatas=[{"category": category, "timestamp": fact["timestamp"], "tier": tier}],
            ids=[fact["id"]]
        )

        return f"Memory stored: [{tier}] {content[:100]}"

    def promote_memory(self, memory_id: str, new_tier: str) -> str:
        for item in self._short_term:
            if item.get("id") == memory_id:
                item["tier"] = new_tier
                if new_tier in ("recent", "long_term"):
                    fact = self._create_fact(
                        item["content"],
                        item.get("category", "general"),
                        new_tier
                    )
                    fact["id"] = item["id"]
                    self._kg.add_fact(fact)
                    self._save_queue.put(True)
                return f"Promoted {memory_id} to {new_tier}"
        return f"Memory {memory_id} not found"

    def _rrf_rank(self, results: List[Dict], k: int = 60) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        for r in results:
            scores[r["content"]] = scores.get(r["content"], 0) + 1 / (k + r["rank"])
        return scores

    def search_memory(self, query: str, limit: int = 5) -> List[Dict]:
        semantic = self._vector.search(query, n=limit * 2)
        query_lower = query.lower()
        keyword = [
            {"content": fact.get("content", ""), "rank": i + 1}
            for i, fact in enumerate(self._kg.get_facts())
            if query_lower in fact.get("content", "").lower()
        ]

        combined = self._rrf_rank(semantic)
        for content, score in self._rrf_rank(keyword).items():
            combined[content] = combined.get(content, 0) + score

        sorted_results = sorted(combined.items(), key=lambda x: x[1], reverse=True)
        return [{"content": c, "score": s} for c, s in sorted_results[:limit]]

    def get_recent_memories(self, limit: int = 10) -> List[Dict]:
        return list(self._short_term)[-limit:]

    def get_now(self) -> Dict[str, Any]:
        return {"type": "now", "description": "Current session", "items": list(self._short_term)[-20:]}

    def get_recent_sessions(self, limit: int = 5) -> Dict[str, Any]:
        if not self._sessions_dir.exists():
            return {"type": "recent", "items": []}

        files = sorted(self._sessions_dir.glob("session_*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]
        sessions = []
        for sf in files:
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    sessions.append({"file": sf.name, "entries": len(lines), "recent": [json.loads(l) for l in lines[-10:] if l.strip()]})
            except Exception:
                pass
        return {"type": "recent", "items": sessions}

    def get_long_term(self, limit: int = 20, tier: str = None) -> Dict[str, Any]:
        facts = self._kg.get_facts(tier)
        return {"type": "long_term", "count": len(facts), "items": facts[-limit:]}

    def evolve(self) -> str:
        removed = self._kg.dedupe()
        if removed > 0:
            self._save_queue.put(True)
            return f"Removed {removed} duplicates."
        return "Memory optimal."

    def log_event_sync(self, role: str, content: str, event_type: str = "message", mode: str = ""):
        entry = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": datetime.now().isoformat(),
            "role": "user" if event_type in ("task", "input") else role,
            "content": content,
            "type": event_type,
            "mode": mode
        }

        with self._file_lock:
            try:
                self._session_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._session_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"Log error: {e}")

    def load_session(self, filename: str):
        if ".." in filename or filename.startswith("/") or "\\" in filename:
            return [], "build", "system", []

        filepath = self._sessions_dir / filename
        if not filepath.exists():
            return [], "build", "system", []

        history, mode, last_role = [], "build", "system"
        short_term_items = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    history.append({"role": entry.get("role", "system"), "content": entry.get("content", "")})

                    if entry.get("mode"):
                        mode = entry["mode"]
                    if entry.get("type") == "session_start" and entry.get("role"):
                        last_role = entry["role"]

                    content = entry.get("content", "")
                    if content.startswith("ROLE:"):
                        last_role = content.split(":", 1)[1].strip()
                    elif content.startswith("MODE:"):
                        mode = content.split(":", 1)[1].strip()

                    short_term_items.append({
                        "id": entry.get("id", str(uuid.uuid4())[:8]),
                        "content": content,
                        "category": entry.get("category", "general"),
                        "timestamp": entry.get("timestamp", datetime.now().isoformat()),
                        "tier": entry.get("tier", "now")
                    })

            self._session_file = filepath
            self._session_id = filename.replace("session_", "").replace(".jsonl", "")
        except Exception as e:
            logger.error(f"Load session error: {e}")

        return history, mode, last_role, short_term_items

    def get_stats(self) -> Dict[str, Any]:
        recent_count = len(list(self._sessions_dir.glob("session_*.jsonl"))) if self._sessions_dir.exists() else 0
        return {
            "now_count": len(self._short_term),
            "recent_sessions": recent_count,
            "long_term_facts": len(self._kg.get_facts()),
            "vector_enabled": self._vector._collection is not None,
            "session_id": self._session_id,
        }

    def clear_short_term(self):
        self._short_term.clear()

    def clear_current_session(self):
        self._short_term.clear()
        try:
            with open(self._session_file, "w", encoding="utf-8") as f:
                f.truncate(0)
        except Exception:
            pass

    def new_session(self):
        self._short_term.clear()
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_file = self._sessions_dir / f"session_{self._session_id}.jsonl"
        header = {
            "id": self._session_id,
            "timestamp": datetime.now().isoformat(),
            "role": "system",
            "content": f"__SESSION_START__: {self._session_id}",
            "type": "session_start",
            "mode": "build"
        }
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._session_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(header, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"New session error: {e}")


memory_core = MemoryCore()
