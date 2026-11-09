import asyncio
import ast
import hashlib
import html
import inspect
import os
import platform
import re
import secrets
import string
import subprocess as _subprocess
import sys
import threading
import time
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Any, Callable, TypeVar
from rich.console import Console
import httpx
import json
import logging
import logging.handlers
from dataclasses import dataclass, field

from src.config import _REDACT_PATTERNS, CONTEXT_PRIORITY, CONTEXT_MAX_CHARS, _INJECT_RE, _TRACE_STYLE, load_config

console = Console()

TRACE_ROOT = Path("tmp/state/trace")

def trace_line(role: str, event_type: str, content: str) -> None:
    """TUI-safe dim one-liner for live agent visibility."""
    if not load_config().trace_enabled:
        return
    from rich.markup import escape
    icon, style = _TRACE_STYLE.get(event_type, ("·", "dim"))
    snippet = escape(" ".join(str(content).split())[:90])
    console.print(
        f"[dim]{datetime.now():%H:%M:%S}[/dim] [{style}]{icon} {role}[/{style}] [dim]{snippet}[/dim]",
        soft_wrap=True,
    )

def _flat(text: Any) -> str:
    """Whitespace-collapsed text."""
    return " ".join(str(text or "").split())

def _fingerprint(kind: str, role: str, text: str) -> str:
    """Stable per-step duplicate key."""
    return hashlib.sha1(f"{kind}\x00{role}\x00{_flat(text)}".encode("utf-8", "replace")).hexdigest()[:16]

def summarise_args(args: Any, limit: int = 3) -> str:
    """``[path=src/cli.py, offset=40]`` — primitives only, like a tool row."""
    if not isinstance(args, dict) or not args:
        return ""
    keys = ("path", "file_path", "command", "query", "url", "pattern", "agent_name", "name")
    entries = [(k, v) for k, v in args.items() if isinstance(v, (str, int, float, bool))]
    entries.sort(key=lambda kv: (kv[0] not in keys, kv[0]))
    entries = entries[:limit]
    if not entries:
        return ""
    return "[" + ", ".join(f"{k}={_flat(v)[:60]}" for k, v in entries) + "]"

@dataclass
class _PendingCall:
    """A tool call waiting for its result."""

    name: str
    args: Dict[str, Any]
    started: float = field(default_factory=time.monotonic)

class TraceBus:
    """One logical event → one terminal line, one log line, one JSONL record.

    Before this, a single answer was announced four times: the code block, the
    ``response`` tool call, the tool result, and the answer. The terminal showed
    four trimmed copies and ``agent.log`` got eight full copies.

    Tool calls are buffered and rendered together with their result. Answers
    render as a receipt — the UI paints the text.
    """

    capture_chars: int = 2000
    root: Path = TRACE_ROOT

    #: Never rendered on their own (recorded only).
    BUFFERED = {"tool_call"}
    SILENT = {"model_output", "ptc_result", "ptc_call", "ptc", "response", "run_event"}

    def __init__(self, role: str = "", enabled: Optional[bool] = None,
                 capture_chars: Optional[int] = None, root: Optional[Path] = None) -> None:
        self.role = role
        self.enabled = bool(load_config().trace_enabled) if enabled is None else bool(enabled)
        self.paint = True
        self.capture_chars = int(capture_chars or 2000)
        self.root = Path(root or TRACE_ROOT)
        self.run_id = ""
        self._pending: Dict[str, _PendingCall] = {}
        self._seen: set[str] = set()
        self._step = 0
        self._records = 0

    # -- wiring ---------------------------------------------------------
    def bind(self, run_id: str) -> None:
        """Point the bus at a run so records have somewhere to land."""
        self.run_id = run_id or ""

    def step(self, number: int) -> None:
        """Start a step; duplicate detection is per step."""
        self._step = int(number or 0)
        self._seen.clear()

    @property
    def path(self) -> Optional[Path]:
        return self.root / f"{self.run_id}.jsonl" if self.run_id else None

    # -- recording ------------------------------------------------------
    def _record(self, kind: str, role: str, text: str, data: Dict[str, Any]) -> None:
        path = self.path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                record = {"ts": round(time.time(), 3), "run_id": self.run_id,
                          "step": self._step, "kind": kind, "role": role,
                          "text": str(text or "")[: self.capture_chars], **data}
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._records += 1
        except Exception:
            logging.getLogger(__name__).debug("Trace record failed", exc_info=True)

    # -- public API -----------------------------------------------------
    def emit(self, kind: str, role: str = "", text: str = "", *, paint: bool = True, **data: Any) -> None:
        """Record an event and render it exactly once."""
        if not self.enabled:
            return
        role = role or self.role
        body = str(text or "")
        mark = _fingerprint(kind, role, body)
        if mark in self._seen:
            return
        self._seen.add(mark)
        self._record(kind, role, body, dict(data))
        if not paint or not self.paint or kind in self.SILENT or kind in self.BUFFERED:
            return
        trace_line(role, kind, body)
        logging.getLogger(__name__).info("%s: %s", kind.upper(), _flat(body)[:400])

    def record(self, kind: str, role: str = "", text: str = "", **data: Any) -> None:
        """Persist an event without rendering or logging it."""
        if not self.enabled:
            return
        self._record(kind, role or self.role, str(text or ""), dict(data))

    def tool_call(self, call_id: str, name: str, args: Dict[str, Any]) -> None:
        """Buffer a call; it renders with its result."""
        if not self.enabled:
            return
        self._pending[call_id] = _PendingCall(name, dict(args or {}))
        self._record("tool_call", self.role, name,
                     {"call_id": call_id, "name": name, "args": dict(args or {})})

    def tool_result(self, call_id: str, name: str = "", *, ok: bool = True,
                    output: Any = None, error: Any = None, duration: float = 0.0) -> None:
        """Render the buffered call and its outcome as one row."""
        if not self.enabled:
            return
        pending = self._pending.pop(call_id, None)
        name = name or (pending.name if pending else "tool")
        args = pending.args if pending else {}
        elapsed = float(duration or 0.0) or (time.monotonic() - pending.started if pending else 0.0)
        summary = summarise_args(args)
        if ok:
            kind = "tool_result"
            parts = [p for p in (summary, f"{elapsed * 1000:.0f}ms" if elapsed else "",
                                 f"{len(str(output or ''))}ch") if p]
            line = name + (" " + " · ".join(parts) if parts else "")
        else:
            kind = "tool_error"
            reason = _flat(error)[:110]
            line = f"{name} {summary} · {reason}".strip() if summary else f"{name} · {reason}"
        self._record(kind, self.role, line, {"call_id": call_id, "name": name, "ok": ok,
                                             "duration": round(elapsed, 4),
                                             "chars": len(str(output or ""))})
        if self.paint:
            trace_line(self.role, kind, line)
        logging.getLogger(__name__).info("%s: %s", kind.upper(), line[:400])

    def answer(self, text: str, streamed: bool = False) -> None:
        """Record the final answer; paint a receipt only when the text is already shown."""
        if not self.enabled:
            return
        size = len(str(text or ""))
        mark = _fingerprint("answer", self.role, f"{size}")
        if mark in self._seen:
            return
        self._seen.add(mark)
        self._record("answer", self.role, str(text or ""), {"chars": size, "streamed": streamed})
        if self.paint and streamed:
            trace_line(self.role, "answer", f"answered ({size:,} chars)")
        logging.getLogger(__name__).info("ANSWER: %d chars", size)

    def close(self) -> None:
        """Flush calls whose result never arrived (interrupt, crash)."""
        for call_id, pending in list(self._pending.items()):
            self.tool_result(call_id, pending.name, ok=False, error="no result (run ended)")
        self._pending.clear()

    def summary(self) -> Dict[str, Any]:
        return {"records": self._records, "step": self._step}


# ─── TURN PARSING ────────────────────────────────────────────────────────

SHORTHAND_RE = re.compile(r"^\s*::(?:(?:tool|call)\s+)?([A-Za-z_][\w.-]*)\s*(.*?)\s*$")
XML_TAG_RE = re.compile(
    r"<(?P<tag>[A-Za-z_][\w.-]*)(?P<attrs>(?:\s+[A-Za-z_][\w.-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)"
    r"\s*(?:/>|>(?P<body>.*?)</(?P=tag)>)",
    re.DOTALL,
)
XML_ARG_RE = re.compile(
    r"<arg\s+name=[\"'](?P<key>[^\"']+)[\"']\s*>(?P<value>.*?)</arg>",
    re.DOTALL | re.IGNORECASE,
)
XML_NAME_RE = re.compile(r"<name>\s*(?P<name>[^<]+?)\s*</name>", re.IGNORECASE)
XML_ATTR_RE = re.compile(r"([A-Za-z_][\w.-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
XML_CHILD_RE = re.compile(
    r"<(?P<key>[A-Za-z_][\w.-]*)\s*(?:name=[\"'](?P<label>[^\"']+)[\"']\s*)?>(?P<value>[^<]*)</(?P=key)>",
    re.DOTALL,
)
XML_WRAPPERS = {"tool", "tool_call", "call", "invoke", "function_call"}
XML_NAME_KEYS = ("name", "tool", "function", "tool_name")
PY_FENCE_RE = re.compile(r"```python\s*\n(.*?)\n\s*```", re.DOTALL)
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?\s*(\{.*?\})\s*\n?\s*```", re.DOTALL)

#: Tools that end a turn instead of feeding another step.
TERMINAL_TOOLS = {"response"}

#: Text that means "unfenced Python someone meant to run".
CODE_HINTS = ("await ", "return ", "import ", "from ", "def ", "print(",
              "for ", "while ", "with open", "async ")

@dataclass
class ToolCallSpec:
    """One parsed tool invocation."""

    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    source: str = "ptc"

@dataclass
class TurnIntent:
    """What one model message asks the loop to do."""

    kind: str                    # calls | answer | retry
    calls: List[ToolCallSpec] = field(default_factory=list)
    code: str = ""
    answer: str = ""
    prose: str = ""
    raw: str = ""
    source: str = ""             # shorthand | call | xml | ptc | json | auto-ptc | prose
    reason: str = ""

def _strip_fences(text: str) -> str:
    return re.sub(r"```.*?```", "", text or "", flags=re.DOTALL)

def _arg_names(spec: Any) -> List[str]:
    """Parameter names of a callable, in declaration order."""
    if spec is None:
        return []
    try:
        sig = inspect.signature(spec)
    except (TypeError, ValueError):
        return []
    return [p.name for p in sig.parameters.values()
            if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]

def _scalar(text: str) -> Any:
    """Python literal if it is one, otherwise a bare string."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return ast.literal_eval(text)
    except Exception:
        return text

_BARE = {"true": True, "false": False, "null": None, "none": None, "nil": None}

def _node_value(node: ast.AST) -> Any:
    """Value of one argument expression; bare names stay strings."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return _BARE.get(node.id.lower(), node.id)
    try:
        return ast.literal_eval(node)
    except Exception:
        pass
    try:
        return ast.unparse(node).strip()
    except Exception:
        return ""

def _parse_kwargs(blob: str) -> Optional[Dict[str, Any]]:
    """``a=1 b="two"`` argument list."""
    pairs = re.findall(
        r"([A-Za-z_][\w.-]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,]+)", blob
    )
    return {key: _scalar(value) for key, value in pairs} if pairs else None

def _parse_call_expr(inner: str, spec: Any) -> Optional[Dict[str, Any]]:
    """``a, b, c=1`` inside a call, mapped onto the callable's parameters."""
    if not inner.strip():
        return {}
    try:
        node = ast.parse(f"_f({inner})", mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call):
        return None
    names = _arg_names(spec)
    args: Dict[str, Any] = {}
    for index, value in enumerate(node.args):
        if index >= len(names):
            if names or len(node.args) > 1 or node.keywords:
                return None
            args["text"] = _node_value(value)
            continue
        args[names[index]] = _node_value(value)
    for keyword in node.keywords:
        if keyword.arg is None:
            return None
        args[keyword.arg] = _node_value(keyword.value)
    return args

def _parse_call_args(blob: str, spec: Any = None) -> Optional[Dict[str, Any]]:
    """Arguments from JSON, ``(a, b)``, ``k=v`` or one bare value."""
    blob = (blob or "").strip()
    if not blob:
        return {}
    if blob[0] in "{[":
        try:
            value = json.loads(blob)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    if blob.startswith("(") and blob.endswith(")"):
        return _parse_call_expr(blob[1:-1], spec)
    if "=" in blob:
        return _parse_kwargs(blob)
    names = _arg_names(spec)
    if names:
        return {names[0]: _scalar(blob)}
    values = [v.strip() for v in blob.split(",")]
    if len(values) > 1:
        return {"args": [_scalar(v) for v in values]}
    return {"value": _scalar(blob)}

def parse_shorthand(text: str, names: Optional[set[str]] = None,
                    specs: Optional[Dict[str, Any]] = None) -> List[ToolCallSpec]:
    """``::name{json}``, ``::name(a, b)`` and ``::name a=1 b=2`` calls."""
    calls: List[ToolCallSpec] = []
    in_fence = False
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = SHORTHAND_RE.match(line)
        if not match:
            continue
        name, blob = match.group(1), match.group(2)
        if names is not None and name not in names:
            continue
        args = _parse_call_args(blob, (specs or {}).get(name))
        if args is None:
            continue
        calls.append(ToolCallSpec(name, args, "call" if blob.startswith("(") else "shorthand"))
    return calls

def _xml_scalar(text: str) -> Any:
    raw = (text or "").strip()
    return _scalar(raw) if raw else ""

def _xml_attrs(blob: str) -> Dict[str, Any]:
    return {
        key: _xml_scalar(double or single)
        for key, double, single in XML_ATTR_RE.findall(blob or "")
    }

def _xml_children(body: str, spec: Any) -> Dict[str, Any]:
    """``<a>1</a><b>2</b>`` children mapped onto a callable's parameters."""
    children = [
        (child.group("label") or child.group("key"), _xml_scalar(child.group("value")))
        for child in XML_CHILD_RE.finditer(body)
    ]
    return _map_slots(children, spec)

def _map_slots(pairs: list, spec: Any) -> Dict[str, Any]:
    slots = _arg_names(spec)
    args: Dict[str, Any] = {}
    for index, (key, value) in enumerate(pairs):
        if slots and key not in slots and index < len(slots):
            key = slots[index]
        args[key] = value
    return args

def _xml_args(body: str, spec: Any, terminal: bool = False) -> Dict[str, Any]:
    """Arguments from ``<arg name="k">v</arg>``, JSON, children or one scalar."""
    body = (body or "").strip()
    if not body:
        return {}
    args: Dict[str, Any] = {
        arg.group("key").strip(): _xml_scalar(arg.group("value"))
        for arg in XML_ARG_RE.finditer(body)
    }
    if not args and body.startswith("{"):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            args = payload
    if not args:
        args = _xml_children(body, spec)
    if not args:
        slots = _arg_names(spec)
        if slots:
            args[slots[0]] = _xml_scalar(body)
        elif terminal:
            args["text"] = _xml_scalar(body)
    return args

def parse_xml_calls(text: str, names: Optional[set[str]] = None,
                    specs: Optional[Dict[str, Any]] = None) -> List[ToolCallSpec]:
    """``<tool name="x">…</tool>``, ``<x k="v"/>`` and ``<x><k>v</k></x>``."""
    specs = specs or {}
    calls: List[ToolCallSpec] = []
    known = set(names or ())
    for tag in XML_TAG_RE.finditer(text or ""):
        name = tag.group("tag")
        body = tag.group("body") or ""
        attrs = _xml_attrs(tag.group("attrs"))
        if name in XML_WRAPPERS:
            name = next((str(attrs.pop(k)) for k in XML_NAME_KEYS if attrs.get(k)), "")
            if not name:
                found = XML_NAME_RE.search(body)
                name = found.group("name").strip() if found else ""
        elif known and name not in known:
            continue
        if not name or (known and name not in known):
            continue
        args = attrs or _xml_args(body, specs.get(name), name in TERMINAL_TOOLS)
        calls.append(ToolCallSpec(name, args, "xml"))
    if calls:
        return calls
    for tag in XML_TAG_RE.finditer(text or ""):
        name = tag.group("tag")
        if name in XML_WRAPPERS or (known and name not in known):
            continue
        attrs = _xml_attrs(tag.group("attrs"))
        body = tag.group("body") or ""
        if not attrs and not body.strip():
            continue
        calls.append(ToolCallSpec(name, attrs or _xml_args(body, specs.get(name), name in TERMINAL_TOOLS), "xml"))
    return calls

def _json_calls(payload: Any, names: Optional[set[str]]) -> List[ToolCallSpec]:
    if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
        items = payload["tool_calls"]
    elif isinstance(payload, dict) and payload.get("name") and "arguments" in payload:
        items = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    calls: List[ToolCallSpec] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("tool") or ""
        if not isinstance(name, str) or not name:
            continue
        if names is not None and name not in names:
            continue
        args = item.get("arguments", item.get("args", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if not isinstance(args, dict):
            continue
        calls.append(ToolCallSpec(name, args, "json"))
    return calls

def looks_like_python(text: str) -> bool:
    """Unfenced text that is Python meant to run."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 40_000:
        return False
    head = "\n".join(stripped.splitlines()[:40])
    if not any(hint in head for hint in CODE_HINTS):
        return False
    try:
        ast.parse(stripped)
    except SyntaxError:
        return False
    return True

def looks_like_markup(text: str) -> bool:
    """Text that is raw XML/HTML rather than prose the user can read."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return False
    markup = sum(1 for l in lines if l.startswith("<") and ">" in l)
    return markup > len(lines) * 0.6

def strip_ptc(text: str, names: Optional[set[str]] = None) -> str:
    """Prose with fenced blocks and every tool-call surface removed."""
    lines = []
    in_fence = False
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or SHORTHAND_RE.match(line):
            continue
        lines.append(line)
    calls = set(names or ()) | XML_WRAPPERS
    return XML_TAG_RE.sub(
        lambda m: "" if m.group("tag") in calls else m.group(0),
        "\n".join(lines),
    ).strip()

def _with_calls(source: str, calls: List[ToolCallSpec], raw: str,
                names: Optional[set[str]]) -> TurnIntent:
    terminal = [c for c in calls if c.name in TERMINAL_TOOLS]
    if terminal:
        return TurnIntent("answer", calls=[c for c in calls if c.name not in TERMINAL_TOOLS],
                          answer=str(terminal[-1].args.get("text", "") or ""),
                          prose=strip_ptc(raw, names), raw=raw, source=source)
    prose = "" if source == "json" else strip_ptc(raw, names)
    return TurnIntent("calls", calls=calls, prose=prose, raw=raw, source=source)

def parse_turn(text: str, names: Optional[set[str]] = None,
               specs: Optional[Dict[str, Any]] = None) -> TurnIntent:
    """Classify one model message into a single intent."""
    raw = text or ""
    body = raw.strip()
    if not body:
        return TurnIntent("retry", raw=raw, reason="empty response")

    calls = parse_shorthand(raw, names, specs)
    if calls:
        return _with_calls("shorthand" if calls[0].source == "shorthand" else "call",
                           calls, raw, names)

    xml_calls = parse_xml_calls(raw, names, specs)
    if xml_calls:
        return _with_calls("xml", xml_calls, raw, names)

    fenced = [block for block in PY_FENCE_RE.findall(raw) if block.strip()]
    if fenced:
        return TurnIntent("calls", code="\n\n".join(fenced), prose=strip_ptc(raw, names),
                          raw=raw, source="ptc")

    for match in JSON_FENCE_RE.finditer(raw):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        parsed = _json_calls(payload, names)
        if parsed:
            return _with_calls("json", parsed, raw, names)

    if body[0] in "{[":
        try:
            parsed = _json_calls(json.loads(body), names)
        except json.JSONDecodeError:
            parsed = []
        if parsed:
            return _with_calls("json", parsed, raw, names)

    if looks_like_python(body):
        return TurnIntent("calls", code=body, raw=raw, source="auto-ptc")

    if looks_like_markup(body):
        return TurnIntent("retry", raw=raw, prose=strip_ptc(raw, names), source="markup",
                          reason="raw XML/HTML instead of prose or a tool call")

    return TurnIntent("answer", answer=body, prose=body, raw=raw, source="prose")

class TokenTracker:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._total = 0
            cls._instance._usage = {}
            cls._instance._lock = threading.Lock()
        return cls._instance

    def get_total(self) -> int:
        return self._total

    def add_tokens(self, tokens: int) -> int:
        with self._lock:
            self._total += tokens
            return self._total

    def get_provider(self, provider: str) -> Dict[str, int]:
        return self._usage.get(provider, {"input": 0, "output": 0})

    def add_provider(self, provider: str, input_tokens: int, output_tokens: int) -> Dict[str, int]:
        with self._lock:
            if provider not in self._usage:
                self._usage[provider] = {"input": 0, "output": 0}
            self._usage[provider]["input"] += input_tokens
            self._usage[provider]["output"] += output_tokens
            return self._usage[provider]

    def get_all(self) -> Dict[str, Dict[str, int]]:
        return dict(self._usage)

    def reset_total(self) -> None:
        with self._lock:
            self._total = 0

    def reset_provider(self, provider: Optional[str] = None) -> None:
        with self._lock:
            if provider:
                self._usage.pop(provider, None)
            else:
                self._usage.clear()


_tracker = TokenTracker()

get_global_tokens = _tracker.get_total
add_global_tokens = _tracker.add_tokens
get_provider_tokens = _tracker.get_provider
add_provider_tokens = _tracker.add_provider
get_all_provider_tokens = _tracker.get_all
reset_global_tokens = _tracker.reset_total
reset_provider_tokens = _tracker.reset_provider


def count_tokens(text_or_history, response: str = "") -> tuple:
    if isinstance(text_or_history, list):
        input_tokens = estimate_messages_tokens(text_or_history)
    else:
        input_text = str(text_or_history)
        input_tokens = count_tokens_tiktoken(input_text)

    output_tokens = count_tokens_tiktoken(response) if response else 0
    return input_tokens, output_tokens

T = TypeVar("T")
logger = logging.getLogger(__name__)

_HTTP_NEVER_RETRY = {400, 401, 403, 404, 422}
_HTTP_ALWAYS_RETRY = {408, 429, 500, 502, 503, 504}
_TEXT_ALWAYS_RETRY = {"timeout", "timed out", "overloaded", "rate limit", "connection refused", "connection reset"}
_TEXT_NEVER_RETRY = {"unauthorized", "forbidden", "not found", "invalid api key", "circuit open"}

def _classify_error(err: Exception) -> str:
    msg = str(err).lower()
    err_type = type(err).__name__.lower()
    if "circuitbreaker" in err_type or "circuit open" in msg:
        return "never"
    status_code = getattr(err, "status_code", None) or getattr(err, "response_status", None)
    if isinstance(status_code, int):
        if status_code in _HTTP_NEVER_RETRY:
            return "never"
        if status_code in _HTTP_ALWAYS_RETRY:
            return "always"
    if any(t in msg for t in _TEXT_NEVER_RETRY):
        return "never"
    if any(t in msg for t in _TEXT_ALWAYS_RETRY):
        return "always"
    return "once"

def async_retry(retries: int = 3, delays=2.0, max_delays: float = 30.0):
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            for attempt in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    kind = _classify_error(e)
                    if kind == "never" or (kind == "once" and attempt >= 1) or attempt >= retries:
                        raise
                    if isinstance(delays, (list, tuple)):
                        base = float(delays[min(attempt, len(delays) - 1)])
                    else:
                        base = min(float(delays) * (2 ** attempt), max_delays)
                    wait_time = base * (0.5 + secrets.randbelow(1000) / 1000.0)
                    logging.getLogger(__name__).warning(
                        "RETRY %d/%d %s in %.1fs [%s]: %s",
                        attempt + 1, retries, func.__name__, wait_time, kind, str(e)[:120])
                    await asyncio.sleep(wait_time)
            raise RuntimeError(f"async_retry exhausted for {func.__name__}")
        return wrapper
    return decorator

def get_system_context() -> str:
    now = datetime.now()
    tz = now.astimezone().tzname() or "UTC"
    shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "Unknown"))
    branch = "none"
    try:
        branch = _subprocess.check_output(
            ["git", "branch", "--show-current"],
            stderr=_subprocess.DEVNULL, text=True, timeout=2,
        ).strip() or "none"
    except Exception:
        pass
    return (f"[Time: {now:%Y-%m-%d %H:%M} {tz}] [OS: {platform.system()} {platform.release()}] [Shell: {shell}] [Project: {Path.cwd()}] [Git: {branch}]")

def redact_fast(text: str) -> str:
    if not text or len(text) < 20:
        return text
    for pat, repl in _REDACT_PATTERNS:
        text = re.sub(pat, repl, text)
    return text


def redact_traceback(tb_text: str) -> str:
    """Redact credentials from a Python traceback string."""
    if not tb_text:
        return tb_text
    for pat, repl in _REDACT_PATTERNS:
        tb_text = re.sub(pat, repl, tb_text)
    return tb_text


def discover_context_files(cwd: str = ".") -> str:
    base = Path(cwd).resolve()
    search = [base]
    cur = base.parent
    while cur != cur.parent and cur.exists():
        if (cur / ".git").exists():
            search.insert(0, cur)
            break
        search.append(cur)
        cur = cur.parent
    for sp in search:
        for name in CONTEXT_PRIORITY:
            t = sp / name
            if not t.exists():
                continue
            try:
                raw = t.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                logging.getLogger(__name__).warning(f"discover_context_files: cannot read {t}: {e}")
                continue
            if any(p.search(raw) for p in _INJECT_RE):
                return f"[BLOCKED] {t} contains injection patterns. Skipped for safety."
            if raw.lstrip().startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    raw = parts[2]
            if len(raw) > CONTEXT_MAX_CHARS:
                h, tl = int(CONTEXT_MAX_CHARS * 0.7), int(CONTEXT_MAX_CHARS * 0.2)
                raw = raw[:h] + "\n\n...[truncated]...\n\n" + raw[-tl:]
            return f"--- PROJECT RULES ({name}) ---\n{raw}\n"
    logging.getLogger(__name__).info("discover_context_files: no project context files found")
    return ""


CONTEXT_RESERVE_SHARE = 0.15
CONTEXT_RESERVE_MIN = 16_000

_ENC = None


def _encoder():
    global _ENC
    if _ENC is None:
        import tiktoken
        _ENC = tiktoken.get_encoding("cl100k_base")
    return _ENC


def count_tokens_tiktoken(text: str) -> int:
    try:
        return len(_encoder().encode(text))
    except ImportError:
        return int(len(text.split()) * 1.3)


def truncate_to_tokens(text: str, max_tokens: int, marker: str = "\n[truncated]") -> str:
    if not text or max_tokens <= 0:
        return ""
    try:
        enc = _encoder()
    except ImportError:
        return text[: max_tokens * 4]
    ids = enc.encode(text)
    if len(ids) <= max_tokens:
        return text
    return enc.decode(ids[:max_tokens]) + marker


def context_ceiling(window: int) -> int:
    """Usable prompt budget: the window minus a reserve that never swallows it."""
    if not window or window <= 0:
        return 0
    reserve = max(int(window * CONTEXT_RESERVE_SHARE), min(CONTEXT_RESERVE_MIN, int(window * 0.5)))
    return min(window, max(256, window - reserve))


def estimate_messages_tokens(messages: List[Dict]) -> int:
    total = 4
    for m in messages:
        total += 4
        for v in m.values():
            total += count_tokens_tiktoken(str(v))
    return total


def generate_random_delimiter(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def sanitize_for_prompt(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    replacements = [
        (r'===', '\\==='), (r'---', '\\---'), (r'```', '\\`\\`\\`'),
        (r'<\|', '\\<\\|'), (r'\|>', '\\|\\>'),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


class ContextCompressor:
    def __init__(self, provider):
        self.provider = provider
        self._prev = None

    async def compress(self, msgs, keep_tokens: int = 20000):
        head = [m for m in msgs if m.get("role") == "system"]
        body = msgs[len(head):]
        tail, acc = [], 0
        for m in reversed(body):
            acc += int(len(str(m.get("content", ""))) / 4) + 4
            if acc > keep_tokens and tail:
                break
            tail.insert(0, m)
        middle = body[:len(body) - len(tail)]
        if len(middle) < 4:
            return msgs
        transcript = "\n".join(f"[{m.get('role','?')}]: {m.get('content','')[:500]}" for m in middle)
        prompt = (f"Update previous summary with new messages.\n\nPrevious:\n{self._prev}\n\nNew:\n{transcript}" if self._prev
                  else f"Summarize this conversation segment in 2-4 sentences. Preserve facts, decisions, and tool results. Do not add new information.\n\n{transcript}")
        sys_prompt = "You are a precise summarizer. Output ONLY the summary text. No preamble, no commentary. Preserve key facts and decisions exactly. Never invent information."
        try:
            r = await self.provider.generate_text([{"role":"user","content":prompt}], sys_prompt)
            s = (r.get("content","") if isinstance(r,dict) else str(r)).strip()
            self._prev = s
        except Exception as e:
            logging.getLogger(__name__).error(f"[{e} and {len(middle)} messages trimmed]")
            return head + tail
        if not self._prev:
            return head + tail
        return head + [{"role": "user", "content": f"<context-summary>\n{self._prev}\n</context-summary>"}] + tail


class ModelCatalog:
    CACHE = Path("tmp") / "models_dev.json"
    URL = "https://models.dev/api.json"
    TTL = 3600
    _data = {}
    _time = 0

    @classmethod
    def _fetch(cls, force=False):
        if not force and cls._data and (time.time() - cls._time) < cls.TTL:
            return cls._data
        try:
            r = httpx.get(cls.URL, timeout=5)
            r.raise_for_status()
            d = r.json()
            if d:
                cls._data = d
                cls._time = time.time()
                cls.CACHE.parent.mkdir(parents=True, exist_ok=True)
                cls.CACHE.write_text(json.dumps(d, separators=(",", ":")))
                return d
        except Exception as e:
            logging.getLogger(__name__).warning(f"ModelCatalog._fetch network error: {e}")
        if not cls._data and cls.CACHE.exists():
            try:
                cls._data = json.loads(cls.CACHE.read_text())
                cls._time = time.time() - cls.TTL + 300
            except: pass
        return cls._data

    @classmethod
    def _pid(cls, provider: str) -> str:
        return {"gemini": "google"}.get(provider, provider)

    @classmethod
    def providers(cls) -> Dict[str, Dict]:
        d = cls._fetch()
        return {pid: p for pid, p in d.items() if isinstance(p, dict) and isinstance(p.get("models"), dict)}

    @classmethod
    def provider(cls, provider: str) -> Optional[Dict]:
        return cls.providers().get(cls._pid(provider))

    @classmethod
    def get_api(cls, provider: str) -> Optional[str]:
        api = (cls.provider(provider) or {}).get("api")
        return api.rstrip("/") if isinstance(api, str) and api else None

    @classmethod
    def get_env(cls, provider: str) -> List[str]:
        return [e for e in ((cls.provider(provider) or {}).get("env") or []) if isinstance(e, str)]

    @classmethod
    def list_models(cls, provider: str, kind: str = "chat", tool_call: bool = False) -> List[str]:
        models = (cls.provider(provider) or {}).get("models", {})
        out = []
        for mid, m in models.items():
            if not isinstance(m, dict):
                continue
            if kind and (m.get("type") or "chat") != kind:
                continue
            if tool_call and not m.get("tool_call"):
                continue
            out.append(mid)
        return sorted(out)

    @classmethod
    def default_model(cls, provider: str) -> str:
        models = (cls.provider(provider) or {}).get("models", {})
        best, score = "", None
        for mid, m in models.items():
            if not isinstance(m, dict) or (m.get("type") or "chat") != "chat":
                continue
            limit = m.get("limit") or {}
            context = limit.get("context") if isinstance(limit.get("context"), (int, float)) else 0
            s = (not m.get("tool_call"), -context)
            if score is None or s < score:
                best, score = mid, s
        return best

    @classmethod
    def get_info(cls, provider: str, model_id: str) -> dict | None:
        d = cls._fetch()
        pid = {"gemini": "google"}.get(provider, provider)
        models = d.get(pid, {}).get("models", {})
        if not isinstance(models, dict): return None
        e = models.get(model_id)
        if e: return e
        mi = model_id.lower()
        for mid, md in models.items():
            if mid.lower() == mi:
                return md
        return None

    @classmethod
    def get_context(cls, provider: str, model_id: str) -> int | None:
        i = cls.get_info(provider, model_id)
        if i:
            c = i.get("limit", {}).get("context")
            if isinstance(c, (int, float)) and c > 0:
                return int(c)
        return None

    @classmethod
    def get_rates(cls, provider: str, model_id: str) -> Optional[Dict[str, float]]:
        cost = (cls.get_info(provider, model_id) or {}).get("cost") or {}
        rates = {k: float(v) for k, v in cost.items() if isinstance(v, (int, float))}
        return rates or None

    @classmethod
    def get_cost(cls, provider: str, model_id: str, input_tokens: int, output_tokens: int) -> Optional[float]:
        rates = cls.get_rates(provider, model_id)
        if rates is None:
            return None
        return (input_tokens * rates.get("input", 0.0) + output_tokens * rates.get("output", 0.0)) / 1_000_000

    @classmethod
    def supports(cls, provider: str, model_id: str, field: str) -> Optional[bool]:
        value = (cls.get_info(provider, model_id) or {}).get(field)
        return value if isinstance(value, bool) else None


def safe_run(cmd: list, cwd=None, timeout=30, **kwargs):
    merged = dict(capture_output=True, text=True, timeout=timeout)
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        si = _subprocess.STARTUPINFO()
        si.dwFlags |= _subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        merged.update(creationflags=CREATE_NO_WINDOW, startupinfo=si)
    merged.update(kwargs)
    return _subprocess.run(cmd, cwd=cwd, **merged)

_logging_configured = False

def setup_logging(log_dir: Path = Path("tmp"), level: int = logging.INFO) -> None:
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            return
    root.setLevel(logging.DEBUG)
    afh = logging.handlers.RotatingFileHandler(
        log_dir / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    afh.setLevel(level)
    afh.setFormatter(fmt)
    root.addHandler(afh)
    efh = logging.handlers.RotatingFileHandler(
        log_dir / "errors.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    efh.setLevel(logging.WARNING)
    efh.setFormatter(fmt)
    root.addHandler(efh)
    # Transport chatter drowns the trace; keep only warnings from HTTP clients.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class GitManager:
    """Shadow git for BUILD-mode rollback. Uses GIT_WORK_TREE for zero-copy snapshots."""
    def __init__(self, cwd: Path, max_snapshots: int = 30, max_total_mb: int = 500):
        self.cwd = cwd.resolve()
        self.max_snapshots = max_snapshots
        self.max_total_mb = max_total_mb
        salt = hashlib.sha256(str(self.cwd).encode()).hexdigest()[:16]
        self.repo_dir = Path.home() / ".zervgen" / "checkpoints" / salt
        self._ensure_repo()
        self._ignore = {'.git','tmp','node_modules','__pycache__','.venv','venv','.env'}

    def _ensure_repo(self):
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if not (self.repo_dir / ".git").exists():
            safe_run(["git", "init"], cwd=self.repo_dir, check=True)
            safe_run(["git", "config", "user.email", "zervgen@local"], cwd=self.repo_dir, check=True)
            safe_run(["git", "config", "user.name", "ZervGen"], cwd=self.repo_dir, check=True)
            exclude = self.repo_dir / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            patterns = ["/tmp/", "/node_modules/", "/__pycache__/", "/.venv/", "/venv/", "/.env"]
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            missing = [p for p in patterns if p not in current.splitlines()]
            if missing:
                exclude.write_text(current.rstrip() + "\n" + "\n".join(missing) + "\n", encoding="utf-8")

    def _git_env(self):
        return {**os.environ, "GIT_DIR": str(self.repo_dir / ".git"), "GIT_WORK_TREE": str(self.cwd)}

    def _repo_mb(self) -> float:
        total = sum(f.stat().st_size for f in self.repo_dir.rglob("*") if f.is_file())
        return total / (1024 * 1024)

    def snapshot(self, session_id: str, state: Optional[dict] = None) -> str:
        env = self._git_env()
        safe_run(["git", "add", "-A"], cwd=self.repo_dir, env=env)
        for path in ("tmp", "node_modules", "__pycache__", ".venv", "venv", ".env"):
            safe_run(["git", "reset", "--", path], cwd=self.repo_dir, env=env)
        r = safe_run(["git", "diff", "--cached", "--quiet"], cwd=self.repo_dir, env=env)
        if r.returncode == 0:
            return None
        r = safe_run(["git", "commit", "-m", f"turn {session_id}"], cwd=self.repo_dir, env=env, text=True)
        sha = None
        if r.returncode == 0:
            rev = safe_run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, env=env, text=True)
            sha = rev.stdout.strip() if rev.returncode == 0 else None
        if state is not None:
            state_dir = self.repo_dir / "run_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            key = sha or "HEAD"
            state_dir.joinpath(f"{key}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        self._prune()
        return sha

    def checkpoint(self, run_id: str, state: Optional[dict] = None) -> Optional[str]:
        revision = self.snapshot(run_id, state=None)
        checkpoint_id = f"{run_id}_{int(time.time() * 1000)}"
        state_dir = self.repo_dir / "run_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"id": checkpoint_id, "revision": revision or self.latest_revision(), "state": state or {}}
        state_dir.joinpath(f"{checkpoint_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return checkpoint_id

    def load_state(self, checkpoint_id: str) -> Optional[dict]:
        path = self.repo_dir / "run_state" / f"{checkpoint_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def latest_revision(self) -> Optional[str]:
        env = self._git_env()
        r = safe_run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, env=env, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def restore(self, revision: str) -> bool:
        if not revision:
            return False
        env = self._git_env()
        r = safe_run(["git", "checkout", revision, "--", "."], cwd=self.repo_dir, env=env, text=True)
        return r.returncode == 0
    
    def checkpoints(self, limit: int = 10) -> List[dict]:
        state_dir = self.repo_dir / "run_state"
        files = sorted(state_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if state_dir.exists() else []
        out = []
        for path in files[:max(1, limit)]:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def restore_checkpoint(self, checkpoint_id: str = "") -> Optional[dict]:
        state = self.load_state(checkpoint_id) if checkpoint_id else (self.checkpoints(1) or [None])[0]
        if not state:
            return None
        revision = state.get("revision")
        if revision and not self.restore(revision):
            return None
        return state
    
    def _revisions(self, env) -> List[str]:
        result = safe_run(["git", "rev-list", "HEAD"], cwd=self.repo_dir, env=env, text=True)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _move_ref(self, revision: str, env) -> bool:
        """Point HEAD at a revision. Never touches the work tree or the index."""
        result = safe_run(["git", "update-ref", "HEAD", revision], cwd=self.repo_dir, env=env, text=True)
        return result.returncode == 0

    def _expire(self, env) -> None:
        safe_run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=self.repo_dir, env=env)
        safe_run(["git", "gc", "--prune=now"], cwd=self.repo_dir, env=env)

    def _prune_states(self) -> None:
        state_dir = self.repo_dir / "run_state"
        if not state_dir.exists():
            return
        files = sorted(state_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[max(1, self.max_snapshots):]:
            try:
                path.unlink()
            except OSError:
                pass

    def _prune(self):
        """Drop old snapshots by moving HEAD. The user's work tree is never reset."""
        env = self._git_env()
        revisions = self._revisions(env)
        if not revisions:
            return
        changed = False
        if self.max_snapshots > 0 and len(revisions) > self.max_snapshots:
            revisions = revisions[: self.max_snapshots]
            changed = self._move_ref(revisions[-1], env)
        while self.max_total_mb > 0 and len(revisions) > 1 and self._repo_mb() > self.max_total_mb:
            revisions = revisions[: max(1, len(revisions) // 2)]
            changed = self._move_ref(revisions[-1], env) or changed
            self._expire(env)
        if changed:
            self._expire(env)
            self._prune_states()

    def rollback(self, steps: int = 1) -> str:
        env = self._git_env()
        r = safe_run(["git","log","--oneline",f"-{steps+1}"], cwd=self.repo_dir, env=env, text=True)
        shas = [l.split()[0] for l in r.stdout.strip().split('\n') if l.strip()]
        if len(shas) <= steps: return None
        target = shas[steps]
        safe_run(["git","checkout",target,"--","."], cwd=self.repo_dir, env=env)
        return target

    def history_depth(self) -> int:
        env = self._git_env()
        r = safe_run(["git","log","--oneline"], cwd=self.repo_dir, env=env, text=True)
        return len([l for l in r.stdout.split('\n') if l.strip()])