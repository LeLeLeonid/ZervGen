import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
import random
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from threading import Lock

import yaml
from src.config import MAX_SHORT_TERM, EVOLUTION_DIR

logger = logging.getLogger(__name__)
chromadb = None


class SessionDB:
    """SQLite-backed session storage with FTS5 search, WAL mode, and retry logic."""
    MAX_RETRIES = 15

    def __init__(self, db_path: Path = None):
        self._db_path = db_path or Path("tmp/memory/sessions.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._init_db()

    def _run_sync(self, func, *args):
        return asyncio.get_running_loop().run_in_executor(self._executor, func, *args)

    def _init_db(self) -> None:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=10000")
        conn.execute("PRAGMA mmap_size=30000000000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                provider TEXT,
                model TEXT,
                title TEXT,
                started_at REAL,
                ended_at REAL,
                message_count INTEGER DEFAULT 0
            )
        """)
        # Ensure last_grade column exists for session quality tracking
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_grade REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                tool_name TEXT,
                timestamp REAL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        try:
            conn.execute("SELECT 1 FROM messages_fts LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    content,
                    role,
                    content='messages',
                    content_rowid='rowid'
                )
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content, role) VALUES (new.rowid, new.content, new.role);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content, role) VALUES('delete', old.rowid, old.content, old.role);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content, role) VALUES('delete', old.rowid, old.content, old.role);
                    INSERT INTO messages_fts(rowid, content, role) VALUES (new.rowid, new.content, new.role);
                END
            """)
        conn.commit()

    def _retry(self, func, *args, **kwargs):
        for attempt in range(self.MAX_RETRIES):
            try:
                with self._lock:
                    conn = sqlite3.connect(str(self._db_path))
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    result = func(conn, *args, **kwargs)
                    conn.commit()
                    conn.close()
                    return result
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                if attempt >= self.MAX_RETRIES - 1 or "lock" not in str(e).lower():
                    raise
                delay = random.uniform(0.01, 0.05) * (2 ** attempt)
                time.sleep(delay)
        raise RuntimeError(f"Failed after {self.MAX_RETRIES} retries")

    def create_session(self, provider: str = "", model: str = "") -> str:
        session_id = str(uuid.uuid4())
        now = time.time()
        def _create(conn):
            conn.execute(
                "INSERT INTO sessions (id, provider, model, started_at) VALUES (?, ?, ?, ?)",
                (session_id, provider, model, now)
            )
            conn.commit()
        self._retry(_create)
        return session_id

    def delete_session(self, session_id: str) -> None:
        def _del(conn):
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        self._retry(_del)

    def delete_all_sessions(self) -> None:
        def _del(conn):
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.commit()
        self._retry(_del)

    def end_session(self, session_id: str) -> None:
        def _update(conn):
            conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time(), session_id))
            conn.commit()
        self._retry(_update)

    def set_session_title(self, session_id: str, title: str) -> None:
        def _update(conn):
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
            conn.commit()
        self._retry(_update)

    def update_session_grade(self, session_id: str, grade: float) -> None:
        def _update(conn):
            conn.execute("UPDATE sessions SET last_grade = ? WHERE id = ?", (grade, session_id))
            conn.commit()
        self._retry(_update)

    def save_message(self, session_id: str, role: str, content: str, tool_name: str = "", msg_id: str = None) -> str:
        msg_id = msg_id or str(uuid.uuid4())
        timestamp = time.time()
        def _insert(conn):
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (id, session_id, role, content, tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (msg_id, session_id, role, content, tool_name, timestamp)
            )
            conn.execute("UPDATE sessions SET message_count = message_count + 1 WHERE id = ?", (session_id,))
            conn.commit()
            return cursor.lastrowid
        self._retry(_insert)
        return msg_id

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        match_expr = " OR ".join(f'"{t}"' for t in terms)
        def _search(conn):
            try:
                rows = conn.execute("""
                    SELECT m.id, m.session_id, m.role, m.content, m.timestamp, s.title
                    FROM messages_fts f
                    JOIN messages m ON m.rowid = f.rowid
                    JOIN sessions s ON m.session_id = s.id
                    WHERE messages_fts MATCH ?
                    ORDER BY rank LIMIT ?
                """, (match_expr, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
            return [{"id": r[0], "session_id": r[1], "role": r[2],
                     "content": (r[3] or "")[:300], "timestamp": r[4], "title": r[5]} for r in rows]
        return self._retry(_search)

    def load_session(self, session_id: str) -> Dict:
        def _load(conn):
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, provider, model, title, started_at, ended_at, message_count, last_grade
                FROM sessions WHERE id = ?
            """, (session_id,))
            row = cursor.fetchone()
            if not row:
                return None
            #TODO: fix last_grade madness
            session = {"id": row[0], "provider": row[1], "model": row[2], "title": row[3], "started_at": row[4], "ended_at": row[5], "message_count": row[6], "last_grade": row[7] if len(row) > 7 else 0.0}
            cursor.execute("SELECT role, content, tool_name, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC", (session_id,))
            session["messages"] = [{"role": r, "content": c, "tool_name": t, "timestamp": ts} for r, c, t, ts in cursor.fetchall()]
            return session
        return self._retry(_load)

    def list_sessions(self, limit: int = 20, min_grade: float = 0.0) -> List[Dict]:
        def _list(conn):
            cursor = conn.cursor()
            if min_grade and min_grade > 0:
                cursor.execute("""
                    SELECT s.id, s.provider, s.model, s.title, s.started_at, s.ended_at, s.message_count, s.last_grade
                    FROM sessions s
                    WHERE s.last_grade >= ?
                    ORDER BY s.started_at DESC
                    LIMIT ?
                """, (min_grade, limit))
                rows = cursor.fetchall()
                return [{"id": r[0], "provider": r[1], "model": r[2], "title": r[3],
                         "started_at": r[4], "ended_at": r[5], "message_count": r[6],
                         "avg_grade": r[7]} for r in rows]
            else:
                cursor.execute("""
                    SELECT s.id, s.provider, s.model, s.title, s.started_at, s.ended_at, s.message_count
                    FROM sessions s
                    ORDER BY s.started_at DESC
                    LIMIT ?
                """, (limit,))
                rows = cursor.fetchall()
                return [{"id": r[0], "provider": r[1], "model": r[2], "title": r[3],
                         "started_at": r[4], "ended_at": r[5], "message_count": r[6]} for r in rows]
        return self._retry(_list)

    def get_stats(self) -> Dict[str, Any]:
        def _stats(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions")
            sess = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM messages")
            msgs = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'")
            user_msgs = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM messages WHERE role = 'assistant'")
            asst_msgs = cursor.fetchone()[0]
            return {"sessions": sess, "messages": msgs, "user_messages": user_msgs, "assistant_messages": asst_msgs}
        return self._retry(_stats)

    def close(self) -> None:
        pass  # WAL mode handles checkpoints automatically


class KnowledgeGraph:
    def __init__(self, kg_file: Path):
        self._kg_file = kg_file
        self._data: Dict[str, Any] = {"facts": []}
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
        try:
            self._kg_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._kg_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save KG: {e}")

    def add_fact(self, fact: Dict) -> None:
        self._data.setdefault("facts", []).append(fact)

    def get_facts(self, tier: Optional[str] = None) -> List[Dict]:
        facts = self._data.get("facts", [])
        return [f for f in facts if f.get("tier") == tier] if tier else list(facts)

    def remove_fact(self, fact_id: str) -> bool:
        facts = self._data.get("facts", [])
        original_len = len(facts)
        self._data["facts"] = [f for f in facts if f.get("id") != fact_id]
        return len(self._data["facts"]) < original_len

    def dedupe(self) -> int:
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
        self._init()

    def _init(self) -> None:
        global chromadb
        if chromadb is None:
            try:
                import chromadb as _chromadb
                chromadb = _chromadb
            except ImportError:
                return
        try:
            self._vector_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self._vector_dir))
            self._collection = self._client.get_or_create_collection("zervgen_facts")
        except Exception as e:
            logger.warning(f"VectorStore init failed: {e}")

    def add(self, documents: List[str], metadatas: List[Dict], ids: List[str]) -> None:
        if not self._collection: return
        try: self._collection.add(documents=documents, metadatas=metadatas, ids=ids)
        except Exception as e: logger.debug(f"Vector add failed: {e}")

    def search(self, query: str, n: int = 10) -> List[Dict]:
        if not self._collection: return []
        try:
            results = self._collection.query(query_texts=[query], n_results=n)
            if results["documents"] and results["documents"][0]:
                return [{"content": d, "rank": i + 1} for i, d in enumerate(results["documents"][0])]
        except Exception as e: logger.debug(f"Vector search failed: {e}")
        return []


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: List[tuple] = []
        self._idf: Dict[str, float] = {}
        self._avg_len: float = 0

    def add(self, doc_id: str, text: str) -> None:
        self._docs.append((doc_id, text.lower().split()))
        N = len(self._docs)
        self._avg_len = sum(len(t) for _, t in self._docs) / max(N, 1)
        df: Dict[str, int] = {}
        for _, tokens in self._docs:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: ((N - f + 0.5) / (f + 0.5) + 1) for t, f in df.items()}

    def index(self, documents: List[tuple]):
        self._docs = [(did, text.lower().split()) for did, text in documents]
        N = len(self._docs)
        self._avg_len = sum(len(t) for _, t in self._docs) / max(N, 1)
        df: Dict[str, int] = {}
        for _, tokens in self._docs:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: ((N - f + 0.5) / (f + 0.5) + 1) for t, f in df.items()}

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        qtokens = query.lower().split()
        scores = []
        for did, dtokens in self._docs:
            score = 0.0
            dl = len(dtokens)
            for qt in qtokens:
                if qt not in self._idf: continue
                tf = dtokens.count(qt)
                score += self._idf[qt] * (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * dl / max(self._avg_len, 1)))
            if score > 0:
                scores.append({"id": did, "score": score, "rank": 0})
        scores.sort(key=lambda x: -x["score"])
        for i, s in enumerate(scores[:limit]):
            s["rank"] = i + 1
        return scores[:limit]


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
        self._kg_file = self._memory_dir / "knowledge_graph.json"
        self._vector_dir = self._memory_dir / "vector_store"
        self._blocks_file = self._memory_dir / "runtime_blocks.json"
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        self._short_term: deque = deque(maxlen=MAX_SHORT_TERM)
        self._kg = KnowledgeGraph(self._kg_file)
        self._vector = VectorStore(self._vector_dir)
        self._bm25 = BM25Index()
        self._session_db = SessionDB()
        self._active_session_id: Optional[str] = None
        self._triggered_this_session: set = set()  # track auto-injected skills per session
        self._rebuild_bm25()

    def _rebuild_bm25(self):
        docs = [(f.get("id", ""), f.get("content", "")) for f in self._kg.get_facts()]
        self._bm25.index(docs)

    def search_memory(self, query: str, limit: int = 10) -> List[Dict]:
        if not query.strip():
            return []
        vector_results = self._vector.search(query, n=limit * 2)
        bm25_results = self._bm25.search(query, limit=limit * 2)
        merged_scores = self._rrf_rank(vector_results + bm25_results, k=60)
        sorted_items = sorted(merged_scores.items(), key=lambda x: -x[1])[:limit]
        results = []
        for content, score in sorted_items:
            fact = next((f for f in self._kg.get_facts() if f.get("content") == content), None)
            results.append({"content": content, "score": score, "fact": fact})
        return results

    def inject_context(self, query: str, limit: int = 5, trusted_sources=None) -> str:
        if not query or not query.strip():
            return ""
        hits = self.search_memory(query, max(1, limit) * 3)
        allowed = set(trusted_sources or ())
        selected = []
        seen = set()
        for hit in hits:
            fact = hit.get("fact") or {}
            source = fact.get("source", "")
            category = fact.get("category", "")
            if allowed and source not in allowed and category not in {"context", "lesson"}:
                continue
            content = str(hit.get("content") or "").strip()
            if content and content not in seen:
                seen.add(content)
                selected.append(content)
            if len(selected) >= limit:
                break
        if len(selected) < limit:
            for fact in self._kg.get_facts():
                if fact.get("category") not in {"context", "lesson"}:
                    continue
                content = str(fact.get("content") or "").strip()
                if content and content not in seen:
                    seen.add(content)
                    selected.append(content)
                if len(selected) >= limit:
                    break
        return "\n".join(selected)

    def search_tgs(self, query: str, limit: int = 3) -> str:
        facts = self._kg.get_facts()
        if len(facts) < 500 and facts:
            text_hits = self.search_memory(query, limit=limit*2)
            if not text_hits:
                return ""
            import re
            term_pat = re.compile(r'\b[A-Z][a-z]{2,}\b|"[^"]+"|\'[^\']+\'|\b[a-z]{4,}\b')
            adj, fact_terms = {}, {}
            for f in facts:
                content = f.get("content", "")
                terms = list(set(term_pat.findall(content)))
                fact_terms[f.get("id", id(f))] = terms
                for t in terms:
                    adj.setdefault(t, set()).update(terms)
                    adj[t].discard(t)
            query_terms = set(term_pat.findall(query))
            seeds = [t for t in query_terms if t in adj][:3]
            if seeds:
                paths, visited = [], set()
                queue = [(s, [s]) for s in seeds]
                while queue and len(paths) < 5:
                    node, path = queue.pop(0)
                    if len(path) > 2:
                        visited.add(node)
                        continue
                    for nb in adj.get(node, []):
                        if nb not in path:
                            new_path = path + [nb]
                            if len(new_path) == 2:
                                paths.append(new_path)
                            else:
                                queue.append((nb, new_path))
                path_entities = {e for p in paths for e in p}
                scored = []
                for h in text_hits:
                    content = h.get("content", "")
                    overlap = sum(1 for e in path_entities if e in content)
                    final_score = h.get("score", 0) * 0.7 + overlap * 0.3
                    scored.append((final_score, h))
                scored.sort(key=lambda x: x[0], reverse=True)
                ranked_text = [h for _, h in scored[:limit]]
                text_entities = set()
                for h in ranked_text:
                    text_entities.update(term_pat.findall(h.get("content", "")))
                orphans = text_entities - path_entities
                rescued = []
                for o in list(orphans)[:2]:
                    if o in adj and adj[o]:
                        rescued.append([o, list(adj[o])[0]])
                path_str = "\n".join(" -> ".join(p) for p in paths + rescued)
                text_str = "\n".join(h.get("content", "") for h in ranked_text)
                raw = f"[GRAPH PATHS]\n{path_str}\n[/GRAPH PATHS]\n[TEXT EVIDENCE]\n{text_str}\n[/TEXT EVIDENCE]"
                return raw
            return "\n".join(h.get("content", "") for h in text_hits[:limit])
        return self.inject_context(query, limit=limit)

    def _create_fact(self, content: str, category: str, tier: str) -> Dict:
        return {"id": str(uuid.uuid4())[:8], "timestamp": time.time(), "content": str(content), "category": str(category), "tier": tier}

    async def add_memory(self, content: str, category: str = "general", tier: str = "now", critic_score: float = 0.0, source: str = "user") -> str:
        GRADED_AND_FAILED = 0 < critic_score < 4.0
        if GRADED_AND_FAILED:
            return "Memory rejected: graded below threshold"
        if len(content) < 10 or len(content) > 2000:
            return "Memory rejected: invalid length"
        if any(kw in content.lower() for kw in ("error:", "traceback", "syntaxerror", "nameerror", "ptc result:", "occurred:")):
            return "Memory rejected: probable error dump"

        fact = self._create_fact(content, category, tier)
        fact["source"] = source
        self._kg.add_fact(fact)
        self._vector.add([content], [fact], [fact["id"]])
        self._bm25.add(fact["id"], content)
        self._short_term.append(fact)
        self._kg.save()
        return f"Stored blueprint (grade:{critic_score})"

    def prune_toxic(self, threshold_hits: int = 5, success_rate: float = 0.5) -> int:
        # Auto-drop blueprints recalled often but failing
        removed = 0
        for f in list(self._kg.get_facts()):
            if f.get("hits", 0) >= threshold_hits and f.get("success_rate", 1.0) < success_rate:
                self._kg.remove_fact(f["id"])
                removed += 1
        if removed: self._kg.save()
        return removed

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
                        self._kg.save()
                        self._rebuild_bm25()
                return f"Promoted {memory_id} to {new_tier}"
        return f"Memory {memory_id} not found"

    def _rrf_rank(self, results: List[Dict], k: int = 60) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        for r in results:
            key = r.get("content") or r.get("id", "")
            scores[key] = scores.get(key, 0) + 1 / (k + r["rank"])
        return scores

    def compress_context(self, query: str, limit: int = 5) -> str:
        raw = self.search_memory(query, limit * 2)
        if not raw: return ""
        signal_lines = []
        for doc in raw:
            text = doc.get("content", "")
            for line in text.splitlines():
                line = line.strip()
                if any(c in line for c in "0123456789[]{}()=<>!:/\\") or len(line.split()) <= 12:
                    signal_lines.append(line)
        max_lines = max(3, len(signal_lines) // 5)
        return "\n".join(signal_lines[:max_lines])

    def search_progressive(self, query: str, depth: str = "summary", limit: int = 5) -> List[Dict]:
        if depth == "summary":
            return self.search_memory(query, limit)
        if depth == "detail":
            results = self.search_memory(query, limit)
            fact_by_id = {f.get("id", ""): f for f in self._kg.get_facts()}
            for r in results:
                fid = next((fid for fid, f in fact_by_id.items() if f.get("content") == r.get("content")), None)
                if fid:
                    r["detail"] = fact_by_id[fid]
            return results
        if depth == "raw":
            results = self.search_memory(query, limit)
            raw = self.search_sessions(query, limit)
            return results + [{"content": r["content"], "source": f"session:{r.get('session_id', '')}", "score": 0.5} for r in raw]
        return self.search_memory(query, limit)

    def search_sessions(self, query: str, limit: int = 10) -> List[Dict]:
        # Use SQLite session DB FTS search
        try:
            return self._session_db.search(query, limit)
        except Exception as e:
            logger.error(f"search_sessions error: {e}")
            return []

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
            self._kg.save()
            self._rebuild_bm25()
            return f"Removed {removed} duplicates."
        return "Memory optimal."

    def log_event_sync(self, role: str, content: str, event_type: str = "message", mode: str = ""):
        from src.utils import redact_fast
        entry = {"id": str(uuid.uuid4())[:8], "timestamp": datetime.now().isoformat(), "role": "user" if event_type in ("task", "input") else role, "content": redact_fast(content), "type": event_type, "mode": mode}
        self._short_term.append(entry)
        if self._active_session_id:
            try:
                self._session_db.save_message(
                    session_id=self._active_session_id,
                    role=entry["role"],
                    content=entry["content"],
                    tool_name=event_type if event_type in ("tool_call", "tool_result", "ptc_call", "ptc_result") else ""
                )
            except Exception:
                pass

    def clear_short_term(self): self._short_term.clear()
    def clear_triggered_skills(self): self._triggered_this_session.clear()

    def set_active_session(self, session_id: str) -> None:
        self._active_session_id = session_id
        self._triggered_this_session.clear()
        session = self._session_db.load_session(session_id)
        if session:
            self.clear_short_term()
            for msg in session.get("messages", []):
                self._short_term.append({
                    "id": msg.get("id", str(uuid.uuid4())[:8]),
                    "timestamp": msg.get("timestamp", time.time()),
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "type": msg.get("role", "user")
                })

    def get_active_session(self) -> Optional[str]:
        return self._active_session_id

    def get_session_history(self, session_id: str) -> List[Dict]:
        session = self._session_db.load_session(session_id)
        return session.get("messages", []) if session else []

    def get_stats(self) -> Dict[str, Any]:
        stats = {
            "now_count": len(self._short_term),
            "long_term_facts": len(self._kg.get_facts()),
            "vector_enabled": self._vector._collection is not None,
            "bm25_docs": len(self._bm25._docs),
            "blocks": len(self.get_runtime_blocks()),
            "sessions": self._session_db.get_stats().get("sessions", 0),
            "session_messages": self._session_db.get_stats().get("messages", 0)
        }
        return stats

    def get_runtime_blocks(self) -> List[Dict]:
        if not self._blocks_file.exists():
            return []
        try:
            return json.loads(self._blocks_file.read_text("utf-8"))
        except Exception:
            return []

    def add_runtime_block(self, detect: str, reason: str, fix: str, source: str = ""):
        blocks = self.get_runtime_blocks()
        for b in blocks:
            if b.get("detect") == detect:
                b["hits"] = b.get("hits", 0) + 1
                break
        else:
            blocks.append({"detect": detect, "reason": reason, "fix": fix, "source": source, "hits": 1, "created": time.time()})
        self._blocks_file.parent.mkdir(parents=True, exist_ok=True)
        self._blocks_file.write_text(json.dumps(blocks, indent=2, ensure_ascii=False), "utf-8")

    def compress_session_context(self, session_id: str, limit: int = 5) -> str:
        session = self._session_db.load_session(session_id)
        if not session: return ""
        messages = session.get("messages", [])
        if not messages: return ""
        recent = messages[-30:]
        transcript = "\n".join(f"{m.get('role','?')}: {str(m.get('content',''))[:300]}" for m in recent)
        lines = [l.strip() for l in transcript.splitlines() if l.strip()]
        signal = [l for l in lines
                  if any(c in l for c in "0123456789[]{}()=<>!:/") or len(l.split()) <= 12]
        if not signal: signal = lines
        max_lines = max(3, int(len(signal) * 0.2))
        return "\n".join(signal[:max_lines])


memory_core = MemoryCore()


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
        if self.facts: lines.append("Facts: " + ", ".join(self.facts[:5]).replace("→", "->").replace("←", "<-"))
        if self.strategies: lines.append("Works: " + ", ".join(self.strategies[:3]).replace("→", "->").replace("←", "<-"))
        if self.anti_patterns: lines.append("Avoid: " + ", ".join(self.anti_patterns[:3]).replace("→", "->").replace("←", "<-"))
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

    def _configured(self) -> List[PeerCard]:
        try:
            from src.config import load_config
            raw = getattr(load_config(), "peer_cards", {}) or {}
        except Exception:
            return []
        cards = []
        for domain, value in raw.items():
            if isinstance(value, str):
                facts = [value]
                strategies = []
                anti_patterns = []
                confidence = 1.0
            elif isinstance(value, dict):
                facts = value.get("facts", [])
                strategies = value.get("strategies", [])
                anti_patterns = value.get("anti_patterns", [])
                confidence = float(value.get("confidence", 1.0))
            else:
                continue
            if isinstance(facts, str): facts = [facts]
            if isinstance(strategies, str): strategies = [strategies]
            if isinstance(anti_patterns, str): anti_patterns = [anti_patterns]
            cards.append(PeerCard("user", str(domain), facts, strategies, anti_patterns, {}, max(0.0, min(1.0, confidence))))
        return cards

    def _load(self):
        if self._file.exists():
            try: self._cards = {k: PeerCard(**v) for k, v in json.loads(self._file.read_text("utf-8")).items()}
            except Exception: self._cards = {}

    def _save(self):
        self._file.parent.mkdir(parents=True, exist_ok=True)
        data = {k: {"agent_id": v.agent_id, "domain": v.domain, "facts": v.facts, "strategies": v.strategies, "anti_patterns": v.anti_patterns, "tool_preferences": v.tool_preferences, "confidence": v.confidence} for k, v in self._cards.items() if not k.startswith("config:")}
        self._file.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")

    def get_relevant(self, agent_id: str, query: str = "", limit: int = 2) -> List[PeerCard]:
        cards = self._configured() + list(self._cards.values())
        q = set(re.findall(r"[a-z0-9_\-]+", query.lower()))
        scored = []
        for card in cards:
            tokens = set(re.findall(r"[a-z0-9_\-]+", (card.domain + " " + " ".join(card.facts + card.strategies + card.anti_patterns)).lower()))
            overlap = len(q & tokens)
            agent_bonus = 2.0 if card.agent_id == agent_id else 0.0
            score = agent_bonus + overlap + max(0.0, min(1.0, card.confidence))
            if score > 0:
                scored.append((score, card))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:limit]]

    def upsert(self, card: PeerCard):
        key = f"{card.agent_id}:{card.domain}"
        existing = self._cards.get(key)
        if existing:
            existing.facts = list(dict.fromkeys(existing.facts + card.facts))[:15]
            existing.strategies = list(dict.fromkeys(existing.strategies + card.strategies))[:8]
            existing.anti_patterns = list(dict.fromkeys(existing.anti_patterns + card.anti_patterns))[:8]
            existing.tool_preferences.update(card.tool_preferences)
            existing.confidence = min(1.0, max(existing.confidence, card.confidence) + 0.05)
        else:
            self._cards[key] = card
        self._save()

    async def evolve(self) -> None:
        for card in self._cards.values():
            if card.agent_id == "user":
                continue
            card.facts = list(dict.fromkeys(card.facts))[:15]
            card.strategies = list(dict.fromkeys(card.strategies))[:8]
            card.anti_patterns = list(dict.fromkeys(card.anti_patterns))[:8]
        self._save()


class Dreamer:
    def __init__(self, provider, memory, interval: int = 300, orchestrator=None):
        self.provider = provider
        self.memory = memory
        self.orchestrator = orchestrator
        self.interval = max(60, interval)
        self._running = False
        self._task = None
        self._cards = PeerCards()
        self._last_check = 0
        self._error_count = 0
        self._max_errors = 5
        self._patterns_file = Path("tmp/memory/error_patterns.json")
        self._error_patterns = self._load_patterns()

    def _load_patterns(self) -> Dict[str, Dict]:
        if self._patterns_file.exists():
            try: return json.loads(self._patterns_file.read_text("utf-8"))
            except Exception: return {}
        return {}

    def _save_patterns(self):
        self._patterns_file.parent.mkdir(parents=True, exist_ok=True)
        self._patterns_file.write_text(json.dumps(self._error_patterns, indent=2, ensure_ascii=False), "utf-8")

    async def start(self):
        if self._running: return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass

    async def _loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                if not self._running: break
                await self._cycle()
                self._error_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._error_count += 1
                logger.error(f"Dream error ({self._error_count}/{self._max_errors}): {e}")
                if self._error_count >= self._max_errors:
                    logger.error("Dreamer: too many errors, backing off 30min")
                    await asyncio.sleep(1800)
                    self._error_count = 0  # reset after cooldown
                await asyncio.sleep(min(60 * self._error_count, 300))

    async def _cycle(self):
        if self.orchestrator and getattr(self.orchestrator, '_user_active', False):
            return
        await self._check_errors()
        await self._check_task_state()
        removed = self.memory.prune_toxic(threshold_hits=5, success_rate=0.5)
        if removed > 0:
            logger.debug(f"[DREAM] Pruned {removed} toxic memories")
        await self._extract_facts()
        await self._write_lessons()

    async def _check_task_state(self):
        if not self.orchestrator or not self.orchestrator.history:
            return
        agent = self.orchestrator
        if agent._repeat_count >= 2:
            logger.debug("[LOOP] Task stuck: same result %sx: %s", agent._repeat_count, str(agent._last_result)[:100])
            agent._repeat_count = 0
        last = agent.history[-1].get("content", "")
        if any(s in last for s in ("[STOP]", "Max steps", "Error:")):
            logger.debug("[STUCK] Task stalled: %s", last)

    async def _check_errors(self):
        self._error_patterns = self._load_patterns()
        try:
            sessions = self.memory._session_db.list_sessions(limit=10)
            for sess in sessions:
                session_id = sess["id"]
                session_data = self.memory._session_db.load_session(session_id)
                if not session_data:
                    continue
                messages = session_data.get("messages", [])
                if not messages:
                    continue
                last_check = self._last_check
                recent_messages = [m for m in messages[-30:] if m.get("timestamp", 0) > last_check]
                if not recent_messages:
                    continue
                self._last_check = max(m.get("timestamp", 0) for m in recent_messages) if recent_messages else self._last_check
                for i, msg in enumerate(recent_messages):
                    content = str(msg.get("content", ""))
                    if not any(p in content for p in ("Error:", "SyntaxError", "ModuleNotFoundError", "FAILED:")):
                        continue
                    context_msgs = [str(recent_messages[j].get("content", ""))[:300] for j in range(max(0, i - 3), i) if recent_messages[j].get("tool_name")]
                    context = "\n".join(context_msgs)
                    pattern_key = content[:120]
                    existing = self._error_patterns.get(pattern_key, {"count": 0, "first_seen": time.time(), "last_seen": 0})
                    existing["count"] += 1
                    existing["last_seen"] = time.time()
                    if existing["count"] <= 2:
                        diag = await self._diagnose(content, context)
                        if diag:
                            existing.update(diag)
                            if diag.get("detect"):
                                self.memory.add_runtime_block(
                                    detect=diag["detect"],
                                    reason=diag.get("reason", ""),
                                    fix=diag.get("fix", ""),
                                    source=pattern_key[:80]
                                )
                    self._error_patterns[pattern_key] = existing
                    self._save_patterns()
        except Exception as e:
            logger.error(f"Dreamer _check_errors error: {e}")

    async def _diagnose(self, error: str, context: str = "") -> Optional[Dict]:
        try:
            ctx_part = f"\n\nCode/context that caused it:\n{context[:500]}" if context else ""
            prompt = f"""ERROR: {error}{ctx_part}

Return JSON:
{{"detect": "short substring from the code that causes this (for auto-detection)", "reason": "one sentence: what went wrong", "fix": "one sentence: what to do instead"}}

Be specific. The "detect" field must be a literal substring that appears in the bad code."""
            r = await self.provider.generate_text([{"role": "user", "content": prompt}], "Return JSON only.")
            content = r.get("content", "") if isinstance(r, dict) else str(r)
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                result = json.loads(m.group())
                if result.get("detect") and result.get("reason"):
                    return result
        except Exception:
            pass
        return None

    async def _extract_facts(self):
        recent = self.memory.get_recent_memories(limit=30)
        if len(recent) < 5: return
        transcript = "\n".join(f"- {str(m.get('content', ''))[:200]}" for m in recent)
        try:
            r = await self.provider.generate_text([{"role": "user", "content": f"Extract facts, strategies, anti-patterns as JSON {{facts:[], strategies:[], anti_patterns:[]}}\n\n{transcript}"}], "Return JSON only.")
            content = r.get("content", "") if isinstance(r, dict) else str(r)
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                insights = json.loads(m.group())
                card = PeerCard(
                    agent_id="system", domain="learned",
                    facts=insights.get("facts", [])[:10],
                    strategies=insights.get("strategies", [])[:5],
                    anti_patterns=insights.get("anti_patterns", [])[:5],
                )
                if card.facts or card.strategies or card.anti_patterns:
                    self._cards.upsert(card)
        except Exception: pass

    async def _write_lessons(self):
        lessons = []
        if lessons:
            self._cards.upsert(PeerCard(
                agent_id="system", domain="runtime",
                anti_patterns=[l["detect"] for l in lessons if l.get("detect")][:8],
                strategies=[l["fix"] for l in lessons if l.get("fix")][:8],
            ))
        for key, data in self._error_patterns.items():
            if data.get("count", 0) >= 2 and data.get("fix"):
                lessons.append({"error": key, "count": data["count"], "reason": data.get("reason", ""), "fix": data.get("fix", ""), "detect": data.get("detect", "")})
        if not lessons:
            return
        self._cards.upsert(PeerCard(
            agent_id="system", domain="runtime",
            anti_patterns=[l["detect"] for l in lessons if l.get("detect")][:8],
            strategies=[l["fix"] for l in lessons if l.get("fix")][:8],
        ))
        EVOLUTION_DIR.mkdir(parents=True, exist_ok=True)
        path = EVOLUTION_DIR / f"lessons_{int(time.time())}.json"
        path.write_text(json.dumps(lessons, indent=2, ensure_ascii=False), encoding="utf-8")
