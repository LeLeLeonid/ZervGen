import asyncio
import json
import logging
import random
import re
import sqlite3
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
                item = self._save_queue.get()
                if item is None:
                    break
                while True:
                    try:
                        self._save_queue.get_nowait()
                    except Exception:
                        break
                self._kg.save()
            except Exception as e:
                logger.error(f"Save worker error: {e}")

    def _enqueue_save(self) -> None:
        self._save_queue.put(True)

    def shutdown(self) -> None:
        self._save_queue.put(None)

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
            self._enqueue_save()

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
                    existing_ids = {f.get("id") for f in self._kg.get_facts()}
                    if item["id"] not in existing_ids:
                        fact = self._create_fact(item["content"], item.get("category", "general"), new_tier)
                        fact["id"] = item["id"]
                        self._kg.add_fact(fact)
                        self._enqueue_save()
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

    def get_long_term(self, limit: int = 20, tier: str = None) -> Dict[str, Any]:
        facts = self._kg.get_facts(tier)
        return {"type": "long_term", "count": len(facts), "items": facts[-limit:]}

    def evolve(self) -> str:
        removed = self._kg.dedupe()
        if removed > 0:
            self._enqueue_save()
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
        safe = Path(filename).name
        if safe != filename or ".." in filename:
            return [], "build", "system", []

        filepath = self._sessions_dir / safe
        if not filepath.resolve().is_relative_to(self._sessions_dir.resolve()):
            return [], "build", "system", []
        if not filepath.exists():
            return [], "build", "system", []

        history, mode, last_role = [], "build", "system"
        short_term_items = []
        valid_roles = ("system", "user", "assistant")
        conversation_types = {"input", "response", "direct", "task", "agent_start", "delegation_result", "loop", "manual", "auto_result", "auto_error"}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    raw_role = entry.get("role", "system")
                    role = raw_role if raw_role in valid_roles else "assistant"

                    entry_type = entry.get("type", "")
                    if entry_type in conversation_types or not entry_type:
                        history.append({"role": role, "content": entry.get("content", "")})

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


from dataclasses import dataclass, field

@dataclass
class PeerCard:
    agent_id: str
    domain: str
    facts: List[str] = field(default_factory=list)
    strategies: List[str] = field(default_factory=list)
    anti_patterns: List[str] = field(default_factory=list)
    tool_preferences: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.5

    def to_prompt_block(self) -> str:
        lines = [f"### {self.agent_id}/{self.domain}"]
        if self.facts:
            lines.append("Facts: " + "; ".join(self.facts[:5]))
        if self.strategies:
            lines.append("Works: " + "; ".join(self.strategies[:3]))
        if self.anti_patterns:
            lines.append("Avoid: " + "; ".join(self.anti_patterns[:3]))
        return "\n".join(lines)


class PeerCards:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._cards = {}
            cls._instance._file = Path("tmp/memory/peer_cards.json")
            cls._instance._load()
        return cls._instance

    def _load(self):
        if self._file.exists():
            try:
                self._cards = {k: PeerCard(**v) for k, v in json.loads(self._file.read_text("utf-8")).items()}
            except Exception:
                self._cards = {}

    def _save(self):
        self._file.parent.mkdir(parents=True, exist_ok=True)
        data = {k: {"agent_id": v.agent_id, "domain": v.domain, "facts": v.facts,
                     "strategies": v.strategies, "anti_patterns": v.anti_patterns,
                     "tool_preferences": v.tool_preferences, "confidence": v.confidence}
                for k, v in self._cards.items()}
        self._file.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")

    def get_relevant(self, agent_id: str, query: str = "", limit: int = 2) -> List[PeerCard]:
        cards = [c for k, c in self._cards.items() if c.agent_id == agent_id]
        if not cards:
            cards = list(self._cards.values())
        q = query.lower()
        scored = [(c.confidence + (3 if c.domain.lower() in q else 0), c) for c in cards if c.confidence > 0]
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:limit]]

    def upsert(self, card: PeerCard):
        key = f"{card.agent_id}:{card.domain}"
        existing = self._cards.get(key)
        if existing:
            existing.facts = list(set(existing.facts + card.facts))[:15]
            existing.strategies = list(set(existing.strategies + card.strategies))[:8]
            existing.anti_patterns = list(set(existing.anti_patterns + card.anti_patterns))[:8]
            existing.confidence = min(1.0, existing.confidence + 0.05)
        else:
            self._cards[key] = card
        self._save()


class Dreamer:
    def __init__(self, provider, memory, interval: int = 300):
        self.provider = provider
        self.memory = memory
        self.interval = max(60, interval)
        self._running = False
        self._task = None
        self._cards = PeerCards()
        self._session_dir = Path("tmp/memory/sessions")
        self._last_check = 0
        self._error_count = 0
        self._max_errors = 5

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                if not self._running:
                    break
                await self._cycle()
                self._error_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._error_count += 1
                logger.error(f"Dream error ({self._error_count}/{self._max_errors}): {e}")
                if self._error_count >= self._max_errors:
                    logger.warning("Dreamer stopped after too many errors")
                    self._running = False
                    break
                await asyncio.sleep(min(60 * self._error_count, 300))

    async def _cycle(self):
        await self._check_errors()
        await self._extract_facts()

    async def _check_errors(self):
        if not self._session_dir.exists():
            return
        files = sorted(self._session_dir.glob("session_*.jsonl"), key=lambda f: f.stat().st_mtime)
        if not files:
            return
        latest = files[-1]
        if latest.stat().st_mtime <= self._last_check:
            return
        self._last_check = latest.stat().st_mtime
        try:
            lines = latest.read_text("utf-8").strip().split("\n")
            events = [json.loads(l) for l in lines[-20:] if l.strip()]
        except Exception:
            return

        errors = [str(e.get("content", "")) for e in events
                  if any(p in str(e.get("content", "")) for p in ("Error:", "SyntaxError", "ModuleNotFoundError"))]

        for err in errors[:3]:
            diag = await self._diagnose(err)
            if diag:
                self.memory.add_memory(f"Diag: {err[:100]} → {diag[:200]}", "diagnostic", "recent")

    async def _diagnose(self, error):
        try:
            prompt = f"Error:\n{error}\n\nSuggest a fix (2-3 sentences). NO code. NO commands."
            r = await self.provider.generate_text(
                [{"role": "user", "content": prompt}],
                "Return a brief text diagnosis only. No code. No commands."
            )
            content = r.get("content", "") if isinstance(r, dict) else str(r)
            return content.strip()[:500] if content else None
        except Exception:
            return None

    async def _extract_facts(self):
        recent = self.memory.get_recent_memories(limit=30)
        if len(recent) < 5:
            return
        transcript = "\n".join(f"- {str(m.get('content', ''))[:200]}" for m in recent)
        try:
            r = await self.provider.generate_text(
                [{"role": "user", "content": f"Extract facts, strategies, anti-patterns as JSON {{facts:[], strategies:[], anti_patterns:[]}}\n\n{transcript}"}],
                "Return JSON only."
            )
            content = r.get("content", "") if isinstance(r, dict) else str(r)
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                insights = json.loads(m.group())
                for f in insights.get("facts", []):
                    self.memory.add_memory(f, "deduced", "recent")
                for s in insights.get("strategies", []):
                    self.memory.add_memory(f"Strategy: {s}", "strategy", "recent")
                self._cards.upsert(PeerCard(
                    agent_id="system", domain="learned",
                    facts=insights.get("facts", []),
                    strategies=insights.get("strategies", []),
                    anti_patterns=insights.get("anti_patterns", []),
                ))
        except Exception:
            pass


class SessionDB:
    _MAX_RETRIES = 15

    def __init__(self, db_path="tmp/memory/sessions.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=1.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._writes = 0
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, provider TEXT, model TEXT,
                title TEXT DEFAULT '', started_at REAL, ended_at REAL, message_count INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_msg ON messages(session_id, timestamp);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content=messages, content_rowid=id);
            CREATE TRIGGER IF NOT EXISTS fts_ins AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES(new.id, new.content); END;
            CREATE TRIGGER IF NOT EXISTS fts_del AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content); END;
        """)

    def _write(self, fn):
        for attempt in range(self._MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                self._writes += 1
                if self._writes % 50 == 0:
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except Exception:
                        pass
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    if attempt < self._MAX_RETRIES - 1:
                        time.sleep(random.uniform(0.02, 0.15))
                        continue
                raise

    def create_session(self, sid, provider="", model=""):
        self._write(lambda c: c.execute("INSERT OR IGNORE INTO sessions(id,provider,model,started_at) VALUES(?,?,?,?)",
            (sid, provider, model, time.time())))

    def end_session(self, sid):
        self._write(lambda c: c.execute("UPDATE sessions SET ended_at=? WHERE id=?", (time.time(), sid)))

    def set_session_title(self, sid, title):
        self._write(lambda c: c.execute("UPDATE sessions SET title=? WHERE id=?", (title[:200], sid)))

    def save_message(self, sid, role, content, tool_name=""):
        def _do(c):
            c.execute("INSERT INTO messages(session_id,role,content,tool_name,timestamp) VALUES(?,?,?,?,?)",
                (sid, role, str(content)[:100000], tool_name, time.time()))
            c.execute("UPDATE sessions SET message_count=message_count+1 WHERE id=?", (sid,))
        self._write(_do)

    def _sanitize_fts(self, q):
        for ch in ('"', "'", '*', '(', ')', ':', '+', '-'):
            q = q.replace(ch, ' ')
        return q.strip()

    def search(self, query, limit=10):
        q = self._sanitize_fts(query)
        if not q:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT m.session_id, m.role, substr(m.content,1,200) as content, m.timestamp "
                    "FROM messages_fts f JOIN messages m ON f.rowid=m.id WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                    (q, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    def load_session(self, sid):
        with self._lock:
            rows = self._conn.execute("SELECT role, content FROM messages WHERE session_id=? ORDER BY timestamp", (sid,)).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(self, limit=20):
        with self._lock:
            rows = self._conn.execute("SELECT id, provider, model, title, started_at, message_count FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self):
        with self._lock:
            r = self._conn.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(message_count),0) as msgs FROM sessions").fetchone()
        return {"sessions": r["cnt"], "messages": r["msgs"]}


session_db = SessionDB()
