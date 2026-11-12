import asyncio
import json
import logging
import os
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt, IntPrompt
from rich.align import Align
from rich.table import Table
from rich.text import Text
import httpx

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, Window, Dimension, ConditionalContainer
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.filters import has_completions
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.completion import Completer, Completion, WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.patch_stdout import patch_stdout

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=ResourceWarning)

from src.config import load_config, MODES, MODE_ORDER, ENV_PATH, config_entries, set_config_value
from src.core.memory import memory_core
from src.core.orchestrator import Orchestrator
from src.core.provider import get_provider, get_model_name, list_live_models, list_providers
from src.skills_loader import get_all_roles
from src.utils import get_global_tokens, reset_global_tokens, count_tokens, add_global_tokens, ModelCatalog, ActivityRenderer, rich_on, set_rich, print_block

logger = logging.getLogger(__name__)
console = Console()
C = "purple"


def _acm(text: str) -> str:
    return f"[bold][{C}]{text}[/{C}][/bold]"


_EVENT_NOISE = re.compile(
    r"^(Timeout, retry|Rate limited|Provider empty|PTC blocked|You returned nothing|"
    r"That tool call did not parse|SPAWN:|Fallback →|Context overflow|DELEGAT)", re.I)


def _conversation(messages: list) -> list:
    """Only the human turns and the answers, in order — no logs, no tool rows."""
    rows = []
    for msg in messages:
        if msg.get("tool_name"):
            continue
        role = msg.get("role", "user")
        content = str(msg.get("content") or "")
        if not content.strip() or role not in ("user", "assistant"):
            continue
        if role == "assistant" and _EVENT_NOISE.match(content):
            continue
        rows.append({"role": role, "content": content})
    return rows


def _fmt_tokens(count: int) -> str:
    """Compact token counts: 812, 58.0k, 4.06M."""
    count = int(count or 0)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


class CC:
    @staticmethod
    def print(*args, **kwargs):
        for arg in args:
            console.print(arg, soft_wrap=True)


class _RankedCompleter(Completer):
    def __init__(self, entries: List[tuple]):
        self.entries = [
            (
                str(entry[1] if len(entry) > 1 else entry[0]),
                str(entry[0]),
                str(entry[2]) if len(entry) > 2 else "",
            )
            for entry in entries
        ]

    @classmethod
    def score(cls, label: str, value: str, query: str) -> Optional[tuple]:
        if not query:
            return (2, 0, len(label))
        hay = f"{label} {value}".lower()
        needle = query.lower()
        position = hay.find(needle)
        if position == 0:
            return (0, 0, len(label))
        if position > 0:
            return (1, position, len(label))
        cursor = 0
        for char in needle:
            cursor = hay.find(char, cursor)
            if cursor < 0:
                return None
            cursor += 1
        return (2, 0, len(label))

    def ranked(self, query: str) -> List[tuple]:
        scored = []
        for value, label, meta in self.entries:
            rank = self.score(label, value, query)
            if rank is not None:
                scored.append((rank, value, label, meta))
        scored.sort(key=lambda item: item[0])
        return scored

    def exact(self, query: str) -> Optional[str]:
        needle = query.strip().lower()
        for value, label, _ in self.entries:
            if value.lower() == needle or label.lower() == needle:
                return value
        return None

    def best(self, query: str) -> Optional[str]:
        ranked = self.ranked(query.strip())
        if not ranked:
            return None
        return ranked[0][1]

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        raw = document.text_before_cursor
        for _, value, label, meta in self.ranked(raw.strip())[:100]:
            yield Completion(value, start_position=-len(raw), display=label, display_meta=meta)


class ZervGenCLI:
    def __init__(self, config=None):
        global C
        self.config = config or load_config()
        C = self.config.accent_color
        self.orchestrator: Optional[Orchestrator] = None
        self._user_active: bool = False
        self._active_lock = asyncio.Lock()
        self._show_usage: bool = True
        self._app: Optional[Application] = None
        self._agent_running = False
        self._should_exit = False
        self._pending_input: asyncio.Queue = None
        self._agent_task: Optional[asyncio.Task] = None
        self._perm_future: Optional[asyncio.Future] = None
        self._mcp_warm_task: Optional[asyncio.Task] = None
        self._modal_cmd: Optional[str] = None
        self._status_text = ""
        self._transcript = ActivityRenderer(console=console)
        self._cmd_table: Dict[str, Any] = {
            "help": lambda a: self._show_help(),
            "history": lambda a: self._show_history(),
            "todo": lambda a: self._show_todos(),
            "time": lambda a: CC.print(f"[cyan]{datetime.now()}[/cyan]"),
            "clear": lambda a: self._clear_session(hard=(a in ("--hard", "-d")), all_sessions=(a in ("--all", "--nuke"))),
            "agent": lambda a: self._agent_menu(),
            "memory": lambda a: CC.print(Panel(str(memory_core.get_stats()), title="Memory Stats", border_style="green")),
            "usage": lambda a: self._toggle_usage(),
            "compact": lambda a: self._compact_memory(),
            "load": lambda a: self._load_session(),
            "sessions": lambda a: self._sessions_menu(),
            "status": lambda a: CC.print(Panel(str(self.orchestrator.get_mode_status()), title="Status", border_style="cyan")),
            "stop": lambda a: self._halt_run(),
            "dream": lambda a: self._toggle_dream(),
            "goal": lambda a: self._cmd_goal(a),
            "loop": lambda a: self._cmd_loop(a),
            "mcp": lambda a: self._cmd_mcp(a),
            "provider": lambda a: self._handle_provider(a),
            "model": lambda a: self._handle_model(a),
            "tier": lambda a: self._handle_tier(a),
            "role": lambda a: self._handle_role(a),
            "mode": lambda a: self._handle_mode(a),
            "critic": lambda a: self._toggle_cfg_cmd("critic_enabled", "Critic", a),
            "trim": lambda a: self._toggle_cfg_cmd("history_trim_enabled", "History Trim", a),
            "get": lambda a: self._handle_get(a),
            "set": lambda a: self._handle_set(a),
            "settings": lambda a: self._handle_get(a),
            "key": lambda a: self._input_api_key(),
            "contract": lambda a: self._handle_contract(a),
            "search": lambda a: self._handle_search(a),
            "rollback": lambda a: self._handle_rollback(a),
            "resume": lambda a: self._handle_resume(a),
            "reload": lambda a: self._cmd_reload(),
            "trace": lambda a: self._cmd_trace(a),
            "logs": lambda a: self._cmd_logs(a),
            "expand": lambda a: self._cmd_expand(a),
            "verbose": lambda a: self._cmd_verbose(a),
            "view": lambda a: self._cmd_view(a),
            "rich": lambda a: self._cmd_rich(a),
        }

    async def _setup_permission_handler(self):
        if not self.orchestrator:
            return
        cli = self
        async def _tui_ask(question: str, details: str = "", default: bool = False) -> bool:
            if not getattr(cli, "_perm_lock", None):
                cli._perm_lock = asyncio.Lock()
            async with cli._perm_lock:
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                cli._perm_future = future
                cli._status_text = f"⚠ {question} — type y/n"
                if cli._app:
                    cli._app.invalidate()
                try:
                    return await asyncio.wait_for(future, timeout=120)
                except asyncio.TimeoutError:
                    return default
                finally:
                    if cli._perm_future is future:
                        cli._perm_future = None
                    cli._status_text = ""
                    if cli._app:
                        cli._app.invalidate()
        self.orchestrator._ask_user = _tui_ask

    def _cmd_verbose(self, args: Optional[str] = None) -> bool:
        """Toggle per-call rows instead of one collapsed activity row."""
        self.transcript.verbose = not self.transcript.verbose
        state = "on" if self.transcript.verbose else "off"
        CC.print(f"[{C}]Verbose activity: {state}[/{C}]")
        return True

    def _cmd_rich(self, args: Optional[str] = None) -> bool:
        """Toggle the answer panel; markdown and links render either way."""
        arg = (args or "").strip().lower()
        if arg in ("on", "1", "true", "enable"):
            set_rich(True)
        elif arg in ("off", "0", "false", "disable"):
            set_rich(False)
        else:
            set_rich(not rich_on())
        CC.print(f"[{C}]Answer panel: {'ON' if rich_on() else 'OFF'} (markdown always on)[/{C}]")
        return True

    def _cmd_expand(self, args: Optional[str] = None) -> bool:
        """Reprint the last activity group with every row in full."""
        try:
            index = int((args or "").strip() or "-1")
        except ValueError:
            index = -1
        CC.print(Panel(Text(self.transcript.expand(index)), title="Activity", border_style=C))
        return True

    def _cmd_view(self, args: Optional[str] = None) -> bool:
        """Preview an image, video still or audio shape in the chat."""
        target = (args or "").strip().strip("'\"")
        if not target:
            CC.print("[dim]Usage: /view <path>[/dim]")
            return True
        if not os.path.isfile(os.path.expanduser(target)):
            CC.print(f"[red]Not a file:[/red] {self._escape(target)}")
            return True
        self.transcript.media.render(os.path.expanduser(target))
        return True

    async def _init_system(self) -> None:
        chosen = self.config.provider
        chain = [chosen] + [p for p in ("openrouter", "groq", "ollama", "lmstudio", "pollinations") if p != chosen]
        last_err = None
        for name in chain:
            try:
                provider = get_provider(name, self.config)
                self.orchestrator = Orchestrator(provider, self.config, mode=self.config.mode, memory=memory_core)
                self.orchestrator.status_cb = self._on_status
                self.orchestrator.trace.renderer = self.transcript
                if name != chosen:
                    self.config.provider = name
                    CC.print(f"[yellow]'{self._escape(chosen)}' unavailable ({self._escape(str(last_err))}) — running on '{self._escape(name)}' this session. Config untouched.[/yellow]")
                break
            except Exception as e:
                last_err = e
        else:
            console.print(f"[bold red]All providers failed:[/bold red] {self._escape(str(last_err))}")
            raise last_err

        if self.config.dream_enabled:
            if not hasattr(self, '_dreamer') or not self._dreamer._running:
                from src.core.memory import Dreamer
                self._dreamer = Dreamer(self.orchestrator.provider, memory_core, self.config.dream_interval, orchestrator=self.orchestrator)
                await self._dreamer.start()
        await self._setup_permission_handler()
        await self._warm_mcp()

    def _reload_mcp(self) -> None:
        """Rebuild the MCP server table after a config change and reconnect."""
        manager = getattr(self.orchestrator, "_mcp", None)
        if manager is None:
            return
        manager.reload(self.config)
        if getattr(self.orchestrator, "_mcp_initialized", False):
            asyncio.create_task(self._warm_mcp())

    async def _warm_mcp(self) -> None:
        """Connect configured MCP servers in the background so the prompt lists them."""
        if not getattr(self.config, "mcp_enabled", True):
            return
        if not getattr(self.config, "mcp_connect_on_start", True):
            return
        if str(self.config.mode).upper() in ("ASK", "CALL"):
            return
        await self.orchestrator._init_mcp()
        manager = self.orchestrator._mcp
        if manager is None or not getattr(manager, "servers", None):
            return
        if self._mcp_warm_task and not self._mcp_warm_task.done():
            return

        async def _connect() -> None:
            try:
                await manager.connect_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("MCP warm connection failed: %s", exc)

        self._mcp_warm_task = asyncio.create_task(_connect())
    
    @property
    def transcript(self) -> ActivityRenderer:
        """The one renderer for agent activity; created on first use."""
        renderer = getattr(self, "_transcript", None)
        if renderer is None:
            renderer = ActivityRenderer(console=console)
            self._transcript = renderer
        return renderer

    def _on_status(self, text: str) -> None:
        self._status_text = text or ""
        if self._app:
            self._app.invalidate()

    def _build_tui(self) -> Application:
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            text = event.app.current_buffer.text.strip()
            if text:
                event.app.current_buffer.history.store_string(text)
                event.app.current_buffer.reset()
                if self._pending_input:
                    self._pending_input.put_nowait(text)

        @kb.add("up")
        def _(event):
            b = event.current_buffer
            b.history_backward() if b.document.on_first_line else b.cursor_up()

        @kb.add("down")
        def _(event):
            b = event.current_buffer
            b.history_forward() if b.document.on_last_line else b.cursor_down()

        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")
        
        @kb.add("tab")
        def _(event):
            buf = event.current_buffer
            if buf.complete_state:
                buf.complete_next()
                return
            if not buf.text:
                self._cycle_mode()
                return
            buf.start_completion(insert_common_part=True)

        @kb.add("c-c")
        def _(event):
            if self._agent_running:
                if self.orchestrator:
                    self.orchestrator.request_interrupt()
                t = getattr(self, "_agent_task", None)
                if t and not t.done():
                    t.cancel()
            else:
                self._should_exit = True
                event.app.exit()

        @kb.add("c-d")
        def _(event):
            if not event.app.current_buffer.text:
                self._should_exit = True
                event.app.exit()

        def get_prompt():
            if not self._show_usage:
                return [("class:prompt", "> ")]
            return [("class:prompt", f"[{get_global_tokens()}] > ")]

        def get_toolbar():
            if self._status_text:
                return [("class:toolbar.busy", f" {self._status_text} ")]
            if self.orchestrator:
                s = self.orchestrator.get_mode_status()
                model = get_model_name(self.config.provider, self.config)[:28]
                line = f" {s['mode']}:{s['role']} · {self.config.provider}/{model} "
                run = getattr(self.orchestrator, "_run", None)
                status = getattr(getattr(run, "status", None), "value", "")
                if run is not None and status == "running":
                    cost = float(run.metadata.get("cost_usd", 0.0) or 0.0)
                    line += f"· step {run.budget.steps} · ${cost:.4f} "
                return [("class:toolbar", line)]
            return [("class:toolbar", " ")]

        commands = [f"/{k}" for k in self._cmd_table]
        cmd_completer = WordCompleter(commands, sentence=True)

        input_area = TextArea(
            height=Dimension(min=1, max=12, preferred=1),
            prompt=get_prompt,
            style="class:input-area",
            multiline=True,
            wrap_lines=True,
            completer=cmd_completer,
            complete_while_typing=True,
        )
        input_area.buffer.history = FileHistory("tmp/.zervgen_history")

        layout = Layout(HSplit([
            Window(content=FormattedTextControl(get_toolbar), height=1, style="class:toolbar"),
            Window(char="─", height=1, style="class:rule"),
            input_area,
            ConditionalContainer(
            content=CompletionsMenu(max_height=8, scroll_offset=1),
            filter=has_completions,
        ),
        ]))

        style = PTStyle.from_dict({
            "input-area": "",
            "prompt": f"bold {C}",
            "toolbar": "bg:#1a1a2e #888888",
            "toolbar.busy": "bg:#1a1a2e #ffaa00",
            "completion-menu": f"bg:#1a1a2e {C}",
            "completion-menu.completion.current": f"bg:{C} #000000",
            "rule": C,
        })

        return Application(
            layout=layout,
            key_bindings=kb,
            style=style,
            full_screen=False,
            mouse_support=False,
            erase_when_done=True,
        )

    def print_banner(self, show_menu: bool = True) -> None:
        console.clear()
        banner = f"""
[white]███████╗███████╗██████╗ ██╗   ██╗ ██████╗ ███████╗███╗   ██╗[/white]
[white]╚══███╔╝██╔════╝██╔══██╗██║   ██║██╔════╝ ██╔════╝████╗  ██║[/white]
[bold {C}]  ███╔╝ █████╗  ██████╔╝██║   ██║██║  ███╗█████╗  ██╔██╗ ██║[/bold {C}]
[bold {C}] ███╔╝  ██╔══╝  ██╔══██╗╚██╗ ██╔╝██║   ██║██╔══╝  ██║╚██╗██║[/bold {C}]
[white]███████╗███████╗██║  ██║ ╚████╔╝ ╚██████╔╝███████╗██║ ╚████║[/white]
[white]╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚══════╝╚═╝  ╚═══╝[/white]
[dim]v1.6.5 - [i]Stable. Simple. Smart.[/i][/dim]
        """
        try:
            mem_count = memory_core.get_stats().get('now_count', 0)
        except Exception:
            mem_count = "?"

        stats_text = f"[dim]🧠 Memory: [cyan]{mem_count}[/cyan] | 🔌 Provider: [cyan]{self.config.provider.upper()}[/cyan] | 🤖 Model: [cyan]{get_model_name(self.config.provider, self.config)[:50]}[/cyan][/dim]"

        menu = Table(box=None, show_header=False, padding=(0, 2))
        if show_menu:
            menu.add_column(justify="right", style=f"bold {C}")
            menu.add_column(justify="left")
            menu.add_row("[1]", "💬  Start Chat\n")
            menu.add_row("[2]", "⚙️  Configuration\n")
            menu.add_row("[3]", "🚪  Exit\n")
        
        layout = Table.grid(padding=1, expand=True)
        layout.add_column(justify="center")
        layout.add_row(Panel(banner, border_style=C, expand=False))
        layout.add_row(stats_text)
        layout.add_row("")
        layout.add_row(menu)
        

        console.print(Align.center(layout, vertical="middle"))

    async def handle_system_command(self, cmd: str) -> bool:
        if not self.orchestrator:
            CC.print("[red]System not initialized.[/red]")
            return True
        parts = cmd.split()
        raw_cmd = parts[0].lower().lstrip("/")
        args = " ".join(parts[1:]) if len(parts) > 1 else None
        handler = self._cmd_table.get(raw_cmd)
        if handler:
            res = handler(args)
            if asyncio.iscoroutine(res):
                await res
            return True
        CC.print(f"[red]Unknown command: {self._escape(cmd)}[/red]\n[dim]Type /help for commands.[/dim]")
        return True

    def _clear_session(self, hard: bool = False, all_sessions: bool = False) -> bool:
        sid = self.orchestrator._active_session_id
        if all_sessions:
            memory_core._session_db.delete_all_sessions()
            CC.print("[yellow]All sessions deleted from disk.[/yellow]")
        elif hard and sid:
            memory_core._session_db.delete_session(sid)
            CC.print(f"[yellow]Session {self._escape(sid[:8])} deleted from disk.[/yellow]")
        self.orchestrator.history = []
        self.orchestrator.skill_name = "system"
        self.orchestrator._last_agent_id = None
        self.orchestrator.agents.clear()
        self.orchestrator._active_session_id = None
        memory_core.set_active_session(None)
        memory_core.clear_triggered_skills()
        reset_global_tokens()
        if not (hard or all_sessions):
            CC.print("[yellow]Cleared local history. Disk kept. /clear --hard = this session, /clear --all = all.[/yellow]")
        return True

    async def _cmd_mcp(self, args: Optional[str] = None) -> bool:
        words = (args or "").strip().split()
        verb = words[0].lower() if words else ""
        if verb == "doctor":
            return await self._mcp_doctor()
        if verb in ("search", "find"):
            return self._mcp_search(" ".join(words[1:]))
        if verb in ("install", "add") and len(words) > 1:
            return await self._mcp_install(words[1], " ".join(words[2:]))
        if verb in ("remove", "rm", "uninstall") and len(words) > 1:
            return await self._mcp_remove(words[1])
        if verb in ("list", "ls"):
            return self._mcp_list()
        self.config.mcp_enabled = not self.config.mcp_enabled
        self.config.save()
        state = "ENABLED" if self.config.mcp_enabled else "DISABLED"
        CC.print(f"[bold {'green' if self.config.mcp_enabled else 'red'}]MCP: {state}[/bold {'green' if self.config.mcp_enabled else 'red'}]")
        if self.config.mcp_enabled and self.orchestrator:
            if self.orchestrator._mcp:
                await self.orchestrator._mcp.cleanup()
            self.orchestrator._mcp = None
            self.orchestrator._mcp_initialized = False
            await self._warm_mcp()
        return True

    def _mcp_search(self, query: str) -> bool:
        from src.core.mcp_manager import search_catalog
        rows = search_catalog(query)
        if not rows:
            CC.print(f"[yellow]No MCP server matches '{self._escape(query)}'.[/yellow]")
            return True
        table = Table(title="MCP CATALOG", border_style=C)
        table.add_column("Server", style="bold")
        table.add_column("Transport", style="cyan")
        table.add_column("Installed", style="green")
        table.add_column("Description", style="dim")
        for row in rows:
            table.add_row(Text(row["name"]), Text(row["transport"]),
                          Text("yes" if row["installed"] else "-"), Text(row["description"]))
        CC.print(table)
        CC.print("[dim]/mcp install <name>  ·  /mcp doctor  ·  /mcp list[/dim]")
        return True

    async def _mcp_install(self, name: str, env_args: str) -> bool:
        from src.core.mcp_manager import install_from_catalog
        secrets: Dict[str, str] = {}
        for pair in env_args.split():
            key, sep, value = pair.partition("=")
            if sep:
                secrets[key.strip()] = value.strip()
        result = install_from_catalog(name, secrets)
        CC.print(f"[{'green' if not result.startswith('Error') else 'red'}]{self._escape(result)}[/]")
        if not result.startswith("Error") and self.orchestrator:
            manager = getattr(self.orchestrator, "_mcp", None)
            if manager is not None:
                manager.reload_from_store()
                self.config.mcp_servers = manager.settings.mcp_servers
                await self._warm_mcp()
        return True

    async def _mcp_remove(self, name: str) -> bool:
        from src.core.mcp_manager import uninstall_from_catalog
        result = uninstall_from_catalog(name)
        CC.print(f"[{'green' if not result.startswith('Error') else 'red'}]{self._escape(result)}[/]")
        if not result.startswith("Error") and self.orchestrator:
            manager = getattr(self.orchestrator, "_mcp", None)
            if manager is not None:
                manager.reload_from_store()
                self.config.mcp_servers = manager.settings.mcp_servers
        return True

    def _mcp_list(self) -> bool:
        from src.core.mcp_manager import MCP_STORE, load_store, store_paths
        store = load_store()
        where = ", ".join(str(path) for path in store_paths()) or str(MCP_STORE)
        if not store:
            CC.print(f"[yellow]No servers in {where}[/yellow]")
            return True
        table = Table(title=f"MCP SERVERS · {where}", border_style=C)
        table.add_column("Server", style="bold")
        table.add_column("Transport", style="cyan")
        table.add_column("Enabled", style="green")
        table.add_column("Target", style="dim")
        for name, cfg in sorted(store.items()):
            target = cfg.get("url") or f"{cfg.get('command', '')} {' '.join(cfg.get('args') or [])}".strip()
            table.add_row(Text(name), Text(str(cfg.get("type") or "auto")),
                          Text("on" if cfg.get("enabled") else "off"), Text(target))
        CC.print(table)
        return True

    async def _mcp_doctor(self) -> bool:
        """Probe every configured server and print a per-server diagnosis."""
        manager = getattr(self.orchestrator, "_mcp", None) if self.orchestrator else None
        if manager is None:
            CC.print("[yellow]MCP is not initialized.[/yellow]")
            return True
        rows = await manager.doctor()
        table = Table(title="MCP DOCTOR", border_style=C)
        table.add_column("Server", style="bold")
        table.add_column("State")
        table.add_column("Detail", style="dim")
        tones = {"ready": "green", "starting": "yellow", "degraded": "yellow", "dead": "red", "off": "dim"}
        for row in rows:
            tone = tones.get(row["state"], "dim")
            table.add_row(Text(row["server"]), Text(row["state"], style=tone), Text(str(row["detail"])))
        CC.print(table)
        return True

    def _cmd_reload(self) -> bool:
        from src.skills_loader import skill_index
        skill_index.reload()
        CC.print(f"[green]Reloaded {len(skill_index.skills)} skills[/green]")
        return True

    def _cmd_trace(self, args: Optional[str]) -> bool:
        """``/trace`` toggles; ``last``, ``cost`` and ``why <step>`` read the JSONL."""
        words = (args or "").strip().lower().split()
        arg = words[0] if words else ""
        if arg == "last":
            CC.print(Panel(Text(self._trace_summary()), title="Last run"))
            return True
        if arg == "why":
            step = int(words[1]) if len(words) > 1 and words[1].isdigit() else 0
            CC.print(Panel(Text(self._trace_step(step)), title=f"Step {step or '?'}"))
            return True
        if arg == "cost":
            cost = 0.0
            run = getattr(self.orchestrator, "_run", None) if self.orchestrator else None
            if run is not None:
                cost = float(run.metadata.get("cost_usd", 0.0) or 0.0)
            CC.print(f"[bold]Cost:[/bold] ${cost:.4f}")
            return True
        cfg = load_config()
        if arg in ("on", "1", "true", "enable"):
            cfg.trace_enabled = True
        elif arg in ("off", "0", "false", "disable"):
            cfg.trace_enabled = False
        else:
            cfg.trace_enabled = not cfg.trace_enabled
        cfg.save()
        CC.print(f"[bold]Trace: {'ON' if cfg.trace_enabled else 'OFF'}[/bold]")
        return True

    def _trace_summary(self) -> str:
        """Ten-line digest of the newest trace file: steps, tools, tokens, cost, errors."""
        from src.utils import TRACE_ROOT
        runs = sorted(TRACE_ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not runs:
            return "No trace records yet."
        path = runs[0]
        rows = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as exc:
            return f"Cannot read {path}: {exc}"
        steps = max((int(r.get("step") or 0) for r in rows), default=0)
        tools = [r for r in rows if r.get("kind") in ("tool_call", "tool_result") and r.get("call_id")]
        errors = [r for r in rows if r.get("kind") == "tool_error"]
        answers = [r for r in rows if r.get("kind") == "answer"]
        llm = [r for r in rows if r.get("kind") == "llm"]
        in_tok = sum(int(r.get("input_tokens") or 0) for r in llm)
        out_tok = sum(int(r.get("output_tokens") or 0) for r in llm)
        cost = sum(float(r.get("cost_usd") or 0.0) for r in llm)
        model = next((r.get("model") for r in reversed(llm) if r.get("model")), "")
        hashes = [r.get("prompt_hash") for r in rows if r.get("kind") == "step" and r.get("prompt_hash")]
        breaks = sum(1 for prev, cur in zip(hashes, hashes[1:]) if prev != cur)
        lines = [f"run {path.stem} · {steps} steps · {len(tools)} tools · {len(errors)} errors",
                 f"  {model or 'model?'} · {in_tok:,} in / {out_tok:,} out · ${cost:.4f}",
                 f"  prefix {hashes[0] if hashes else '?'} · {breaks} cache break(s)"]
        for row in tools[-6:]:
            lines.append(f"  {row.get('kind')} {row.get('text', '')[:100]}")
        if answers:
            lines.append(f"  answer {answers[-1].get('chars', 0)} chars")
        return "\n".join(lines)

    def _trace_step(self, step: int) -> str:
        """What one step actually did: prompt, model call, tools, sensors."""
        from src.utils import TRACE_ROOT
        runs = sorted(TRACE_ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not runs:
            return "No trace records yet."
        rows = []
        for line in runs[0].read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not step or int(row.get("step") or 0) == step:
                rows.append(row)
        if not rows:
            return f"No records for step {step}."
        lines = []
        for row in rows:
            kind = row.get("kind", "?")
            if kind == "llm":
                lines.append(f"  model {row.get('model')} · {row.get('input_tokens')} in / "
                             f"{row.get('output_tokens')} out · {row.get('elapsed_ms')}ms")
            elif kind == "step":
                lines.append(f"  prompt {row.get('prompt_chars')} chars · hash {row.get('prompt_hash')} · "
                             f"{row.get('history_tokens')} ctx tokens")
            elif kind in ("tool_call", "tool_result", "tool_error", "sensor"):
                lines.append(f"  {kind} {str(row.get('text', ''))[:140]}")
        return "\n".join(lines) or "Nothing to show."

    def _halt_run(self) -> bool:
        """Stop the active run now: the interrupt flag plus task cancellation."""
        if not self.orchestrator or not (self._agent_task and not self._agent_task.done()):
            CC.print("[yellow]Nothing is running.[/yellow]")
            return True
        self.orchestrator.request_interrupt()
        self._agent_task.cancel()
        CC.print("[bold yellow]Run stopped.[/bold yellow]")
        return True

    def _cmd_logs(self, args: Optional[str] = None) -> bool:
        """``/logs [errors] [lines] [pattern]`` — tail or grep tmp/agent.log."""
        words = (args or "").strip().split()
        source = "errors" if words and words[0] == "errors" else "agent"
        rest = words[1:] if words and words[0] == "errors" else words
        count = int(rest[0]) if rest and rest[0].isdigit() else 40
        pattern = " ".join(w for w in rest if not w.isdigit())
        path = Path("tmp") / ("errors.log" if source == "errors" else "agent.log")
        if not path.exists():
            CC.print(f"[yellow]No {path} yet.[/yellow]")
            return True
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if pattern:
            rows = [r for r in rows if pattern.lower() in r.lower()]
        for row in rows[-max(1, count):]:
            console.print(self._escape(row), soft_wrap=True)
        CC.print(f"[dim]{path} · {len(rows)} matching · /logs errors {count} to switch file[/dim]")
        return True

    def _toggle_cfg_cmd(self, attr: str, label: str, args: Optional[str]) -> None:
        current = getattr(self.config, attr, False)
        if not args:
            CC.print(f"[bold]{label}:[/bold] {'enabled' if current else 'disabled'}")
            return
        enable = args.lower() in ("on", "enable", "true", "1")
        setattr(self.config, attr, enable)
        self.config.save()
        if self.orchestrator:
            self.orchestrator.invalidate_cache()
        CC.print(f"[bold {'green' if enable else 'yellow'}]{label} {'ENABLED' if enable else 'DISABLED'}[/bold {'green' if enable else 'yellow'}]")

    async def _toggle_dream(self) -> bool:
        from src.core.memory import Dreamer
        if not self.orchestrator:
            await self._init_system()
        if not hasattr(self, '_dreamer'):
            self._dreamer = Dreamer(self.orchestrator.provider, memory_core, self.config.dream_interval, orchestrator=self.orchestrator)
        if self._dreamer._running:
            await self._dreamer.stop()
            CC.print("[yellow]Dreaming OFF.[/yellow]")
            self.config.dream_enabled = False
        else:
            self._dreamer.interval = self.config.dream_interval
            await self._dreamer.start()
            CC.print("[green]Dreaming ON.[/green]")
            self.config.dream_enabled = True
        self.config.save()
        return True

    async def _cmd_goal(self, args: Optional[str]) -> bool:
        """Set or clear the completion contract every following turn must satisfy."""
        if not self.orchestrator:
            return True
        text = (args or "").strip()
        if not text or text in ("off", "clear", "none"):
            self.orchestrator._goal_text = ""
            self.orchestrator._goal_max = 10
            CC.print("[yellow]Goal cleared.[/yellow]")
            return True
        rounds = 10
        head, _, tail = text.partition(" ")
        if head.startswith("--max=") and tail:
            rounds = max(1, int(head[6:] or 10))
            text = tail
        self.orchestrator._goal_text = text
        self.orchestrator._goal_max = rounds
        self.orchestrator._goal_round = 0
        CC.print(f"[green]Goal set[/green] (max {rounds} rounds): {self._escape(text)}")
        return True

    async def _cmd_loop(self, args: Optional[str]) -> bool:
        """/loop [--n=3] <task> — repeat the task until done or the cap is hit."""
        if not self.orchestrator:
            return True
        text = (args or "").strip()
        if not text or text in ("off", "stop"):
            self.orchestrator._goal_text = ""
            CC.print("[yellow]Loop off.[/yellow]")
            return True
        rounds = 3
        head, _, tail = text.partition(" ")
        if head.startswith("--n=") and tail:
            rounds = max(1, min(50, int(head[4:] or 3)))
            text = tail
        self.orchestrator._goal_text = text
        self.orchestrator._goal_max = rounds
        self._loop_complete = False
        for i in range(1, rounds + 1):
            if self.orchestrator.interrupt_event.is_set():
                break
            self.orchestrator._goal_round = i
            CC.print(f"[cyan]── loop {i}/{rounds} ──[/cyan]")
            task = text if i == 1 else f"Continue. Reply LOOP_COMPLETE only when the task is fully done.\n{text}"
            self.orchestrator.interrupt_event.clear()
            self._loop_complete = False
            await self._do_run(task)
            self.orchestrator._goal_round = 0
            if self._loop_complete:
                CC.print(f"[green]Loop complete at round {i}/{rounds}.[/green]")
                break
        self.orchestrator._goal_text = ""
        CC.print("[green]Loop finished.[/green]")
        return True

    def _context_tokens(self) -> int:
        """Tokens actually in the next request, not the session's running total."""
        if not self.orchestrator:
            return 0
        try:
            return count_tokens(self.orchestrator.history, "")[0]
        except Exception:
            return 0

    def _toggle_usage(self) -> bool:
        self._show_usage = not self._show_usage
        CC.print(f"[bold][{C}]Token Counter: {'ON' if self._show_usage else 'OFF'}[/{C}][/bold]")
        return True

    def _handle_provider(self, args: Optional[str]) -> Any:
        name = (args or "").strip().lower()
        if not name:
            return self._select_provider()
        from src.config import resolve_provider
        target = resolve_provider(name)
        known = {meta.name for meta in list_providers()} | {target}
        if target not in known:
            CC.print(f"[red]Unknown provider: {self._escape(name)}[/red]\n[dim]Use /provider with no argument to list.[/dim]")
            return True
        previous = self.config.provider
        if self.orchestrator and not self.orchestrator.switch_provider(target):
            CC.print(f"[red]Switch failed — stayed on {self._escape(previous.upper())}[/red]")
            return True
        if not self.orchestrator:
            self.config.provider = target
            self.config.save()
        if not self._has_api_key():
            CC.print(f"[yellow]{target.upper()} needs an API key.[/yellow]")
            self._input_api_key()
        model = get_model_name(self.config.provider, self.config)
        CC.print(f"[bold]Provider:[/bold] {self.config.provider.upper()}  [dim]Model: {model}[/dim]")
        return True

    def _handle_get(self, args: Optional[str]) -> bool:
        rows = config_entries(self.config)
        if not args:
            self._show_config_table(rows)
            return True
        matches = [(path, value) for path, value in rows if path == args or path.endswith(f".{args}")]
        if not matches:
            CC.print(f"[red]Unknown setting: {self._escape(args)}[/red]\n[dim]Use /get to list all.[/dim]")
            return True
        for path, value in matches:
            CC.print(f"[bold cyan]{path}[/bold cyan] = [green]{self._escape(value)}[/green]")
        return True

    def _show_config_table(self, rows: List[tuple]) -> None:
        table = Table(title="SETTINGS", border_style=C)
        table.add_column("Parameter", style="cyan", no_wrap=True)
        table.add_column("Value", style="green")
        for path, value in rows:
            table.add_row(self._escape(path), self._escape(value))
        CC.print(table)
        CC.print("[dim]/set <key> <value> to change any of them.[/dim]")

    def _handle_set(self, args: Optional[str]) -> bool:
        if not args:
            self._show_config_table(config_entries(self.config))
            return True
        parts = args.split(None, 1)
        key = parts[0]
        if len(parts) == 1:
            return self._handle_get(key)
        raw = parts[1].strip()
        old_provider = self.config.provider
        old_mode = self.config.mode
        try:
            message = set_config_value(self.config, key, raw)
        except Exception as exc:
            CC.print(f"[bold red][!] {self._escape(str(exc))}[/bold red]")
            return True
        applied = self._apply_live_config(key, old_provider, old_mode)
        if applied is False:
            return True
        self.config.save()
        CC.print(f"[bold green]✓ {self._escape(message)}[/bold green]")
        return True

    def _apply_live_config(self, key: str, old_provider: str, old_mode: str) -> bool:
        """Push a changed setting into the running system."""
        if not self.orchestrator:
            return True
        if key == "provider" and self.config.provider != old_provider:
            if not self.orchestrator.switch_provider(self.config.provider):
                self.config.provider = old_provider
                CC.print(f"[red]Switch failed — stayed on {self._escape(old_provider.upper())}[/red]")
                return False
        if key in {"provider", "model", "temperature", "active_model_tier"} or key.startswith("model_tiers") or key.endswith(".model") or key.endswith(".temperature"):
            try:
                self.orchestrator.provider = get_provider(self.config.provider, self.config)
            except Exception as exc:
                CC.print(f"[yellow]Provider refresh failed: {self._escape(str(exc))}[/yellow]")
        if key == "mode" and self.orchestrator.mode != self.config.mode:
            if not self.orchestrator.set_mode(self.config.mode):
                self.config.mode = old_mode
                CC.print(f"[red]Mode change failed — stayed on {self._escape(old_mode)}[/red]")
                return False
        if key == "allowed_directories":
            from src.tools import reload_allowed_roots
            reload_allowed_roots()
        if key == "mcp_servers" or key == "mcp_enabled":
            from src.core.mcp_manager import MCPManager
            self.orchestrator._mcp_initialized = False
            MCPManager._instance = None
            self.orchestrator._mcp = None
        if key in {"permission_mode", "mode", "allowed_directories"}:
            self.orchestrator._runtime = self.orchestrator._build_runtime()
        self.orchestrator.invalidate_cache()
        return True

    def _handle_role(self, args: Optional[str]) -> bool:
        if not args:
            CC.print(f"[bold]Current Role:[/bold] {self.orchestrator.get_mode_status()['role']}")
            return True
        if self.orchestrator.set_role(args):
            CC.print(f"[bold green][+] ROLE UPDATED: {args.upper()}[/bold green]")
        else:
            CC.print(f"[bold red][!] ERROR:[/bold red] Role '{self._escape(args)}' not found")
        return True

    async def _handle_contract(self, args: Optional[str]) -> bool:
        from src.skills_loader import skill_index
        if not args:
            CC.print("[yellow]Usage: /contract <skill_name>[/yellow]")
            CC.print(f"\nAvailable skills: {', '.join(skill_index.skills.keys())}")
            return True
        skill = skill_index.get(args)
        if not skill:
            CC.print(f"[red]Skill '{self._escape(args)}' not found[/red]")
            return True
        table = Table(title=f"Contract: {skill.name}", border_style=C)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Description", skill.description or "-")
        table.add_row("Verification", skill.verification)
        table.add_row("Dependencies", ", ".join(skill.dependencies) if skill.dependencies else "none")
        table.add_row("Tools", ", ".join(skill.tools) if skill.tools else "none")
        table.add_row("PRE", str(skill.pre) if skill.pre else "none")
        table.add_row("POST", str(skill.post) if skill.post else "none")
        if skill.procedure:
            table.add_row("Procedure", "\n".join(f"  {i+1}. {s}" for i, s in enumerate(skill.procedure)))
        CC.print(table)
        return True

    async def _handle_search(self, args: Optional[str]) -> bool:
        if not args:
            CC.print("[yellow]Usage: /search <query>[/yellow]")
            return True
        try:
            from src.core.memory import memory_core
            results = memory_core.search_sessions(args.strip(), limit=10)
            if not results:
                CC.print("[dim]No matches[/dim]")
                return True
            table = Table(title=Text(f"Search: {args}"), border_style=C)
            table.add_column("#", style="cyan")
            table.add_column("Snippet")
            table.add_column("Role", style="green")
            table.add_column("Time", style="dim")
            for i, r in enumerate(results, 1):
                c = str(r.get("content", ""))
                table.add_row(
                    str(i),
                    Text((c[:100] + "...") if len(c) > 100 else c),
                    Text(str(r.get("role", "?"))),
                    Text(
                        datetime.fromtimestamp(r.get("timestamp", 0)).strftime("%m-%d %H:%M")
                        if isinstance(r.get("timestamp"), (int, float))
                        else ""
                    ),
                )
            CC.print(table)
        except Exception as e:
            CC.print(f"[red]Error: {self._escape(e)}[/red]")
        return True

    async def _handle_rollback(self, args: Optional[str]) -> bool:
        if not self.orchestrator or not getattr(self.orchestrator, "_git_mgr", None):
            CC.print("[yellow]GitManager not active (enable checkpoints + use BUILD mode)[/yellow]")
            return True
        steps = int(args.strip()) if args and args.strip().isdigit() else 1
        depth = self.orchestrator._git_mgr.history_depth()
        if depth <= steps:
            CC.print(f"[red]Not enough history. Available: {depth - 1} steps[/red]")
            return True
        try:
            sha = self.orchestrator._git_mgr.rollback(steps)
            if sha:
                CC.print(f"[green]Rolled back {steps} step(s) -> {sha[:8]}[/green]")
            else:
                CC.print("[red]Rollback failed[/red]")
        except Exception as e:
            CC.print(f"[red]Error: {self._escape(e)}[/red]")
        return True

    async def _handle_resume(self, args: Optional[str]) -> bool:
        """Restore the newest checkpoint (or the named one) and continue its goal."""
        if not self.orchestrator:
            return True
        checkpoint_id = (args or "").strip()
        try:
            result = await self.orchestrator.resume_checkpoint(checkpoint_id)
        except Exception as e:
            CC.print(f"[red]Resume failed: {self._escape(e)}[/red]")
            return True
        CC.print(f"[cyan]{self._escape(result)}[/cyan]")
        return True

    async def _handle_mode(self, args: Optional[str]) -> bool:
        if args:
            if self._apply_mode(args.upper()):
                CC.print(f"[bold green][+] MODE SHIFTED: {args.upper()}[/bold green]")
            else:
                CC.print(f"[bold red][!] ERROR: Mode '{self._escape(args.upper())}' invalid[/bold red]")
            return True

        status = self.orchestrator.get_mode_status()
        CC.print(f"\n[bold]Current Mode:[/bold] {status['mode']}")
        CC.print(f"\n{_acm('SELECT MODE')}")
        for index, name in enumerate(MODE_ORDER, 1):
            CC.print(f"  [{index}] {name:<6} - {MODES[name]['description']}")
        CC.print("  [b] Back")
        CC.print("[dim]Tip: TAB on an empty prompt cycles modes.[/dim]")

        choice = Prompt.ask(_acm("Mode"))
        if choice.lower() in ('b', 'back'):
            return True
        mode_map = {str(index): name for index, name in enumerate(MODE_ORDER, 1)}
        if choice in mode_map:
            if self._apply_mode(mode_map[choice]):
                CC.print(f"[bold green][+] MODE SHIFTED: {mode_map[choice]}[/bold green]")
            else:
                CC.print("[bold red][!] ERROR: Mode change failed[/bold red]")
        else:
            CC.print("[yellow]Invalid choice.[/yellow]")
        return True

    def _apply_mode(self, mode: str) -> bool:
        if not self.orchestrator or not self.orchestrator.set_mode(mode):
            return False
        self.config.mode = mode
        try:
            self.config.save()
        except Exception:
            pass
        return True

    def _cycle_mode(self) -> None:
        if not self.orchestrator:
            return
        current = str(self.orchestrator.mode).upper()
        index = MODE_ORDER.index(current) + 1 if current in MODE_ORDER else 0
        self._apply_mode(MODE_ORDER[index % len(MODE_ORDER)])
        if self._app:
            self._app.invalidate()

    async def _agent_menu(self) -> bool:
        roles = get_all_roles()
        role_list = list(roles.items())
        if not role_list:
            CC.print("[red]No agents available.[/red]")
            return True

        console.clear()
        CC.print(f"{_acm('SELECT AGENT')}\n")
        for i, (name, cfg) in enumerate(role_list, 1):
            marker = " [cyan](current)[/cyan]" if name == self.orchestrator.skill_name else ""
            CC.print(f"  [{i}] {self._escape(name)}{marker}")

        choice = Prompt.ask(_acm("Choice"))
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(role_list):
                name, cfg = role_list[idx]
                self.orchestrator.set_role(name)
                self.orchestrator._log(name.capitalize(), f"AGENT: {name}", "state")
                CC.print(f"[green]Agent switched to: {self._escape(name)}[/green]")
        except (ValueError, IndexError):
            pass
        return True

    def _show_help(self) -> bool:
        roles = "\n".join(f"- [{C}]{k}[/{C}]: {v.description}" for k, v in get_all_roles().items())
        modes = "\n".join(f"- [green]{k}[/green]: {v['description']}" for k, v in MODES.items())

        help_text = f"""[b]System Commands:[/b]
/history - Show recent conversation context
/time    - Show current system time
/clear   - Clear current session history
/agent   - Select agent submenu
/contract - Show skill contract details
/search  - Search session memory
/rollback - Rollback last agent step (BUILD mode)
/resume  - Restore the latest checkpoint and continue its goal
/memory  - Show memory statistics
/load    - Load your sessions
/usage   - Toggle the Token Counter
/compact - Compact short-term memory into long-term storage
/reload  - Reload your skills
/mcp     - Model Context Protocol (/mcp doctor to probe)
/get     - List every setting, or show one: /get max_steps
/set     - Change any setting: /set max_steps 80
/key     - Set the API key for the current provider
/expand  - Reprint the last activity group (/expand 3)
/verbose - Toggle per-call rows instead of one collapsed row
/rich    - Toggle Rich rendering of markdown and markup
/goal    - Set a completion contract: /goal <text> (off clears)
/loop    - Repeat a task: /loop [--n=3] <task>
/logs    - Tail or grep agent.log: /logs [errors] [lines] [pattern]
/stop    - Stop the running turn now
/view    - Preview an image, video or audio file: /view tmp/x.png
/q       - Quit application

[bold][{C}]PROVIDER:[/{C}][/bold]
/provider          - Pick a provider from a filtered list
/provider <name>   - Switch provider for this session
/model             - Pick a model from the live catalog
/model <name>      - Set the model for the current provider
/tier              - Pick an active model tier (NOOB/COOL/APEX)
/tier <name>       - Activate a tier directly

[bold][{C}]MODE:[/{C}][/bold]
/mode    - Show selection menu (ASK, BUILD, CALL, DEBUG) · TAB cycles modes

[bold][{C}]TOGGLES:[/{C}][/bold]
/critic on|off   - Toggle Self-Interrogation [dim]({'on' if self.config.critic_enabled else 'off'})[/dim]
/trim on|off     - Toggle History Trimming [dim]({'on' if self.config.history_trim_enabled else 'off'})[/dim]
/trace on|off    - Live agent event one-liners [dim]({'on' if self.config.trace_enabled else 'off'})[/dim]
/trace last      - Digest of the newest run
/trace why <n>   - What step n did: prompt, model call, tools
/trace cost      - Cost spent by the current run

[b]ROLES:[/b]
{roles}

[b]MODES:[/b]
{modes}"""
        console.print(Panel(help_text, title="Help", border_style=f"{C}"))
        return True

    def _show_history(self) -> bool:
        if self.orchestrator and self.orchestrator.history:
            summary = self.orchestrator.get_history_summary()
            CC.print(Panel(Text(summary), title="Recent History"))
        else:
            CC.print("[dim]No history yet.[/dim]")
        return True

    def _show_todos(self) -> bool:
        todo_file = Path("tmp/todos.json")
        if not todo_file.exists():
            CC.print("[dim]No TODOs yet.[/dim]")
            return True
        try:
            todos = json.loads(todo_file.read_text()) or []
            if not todos:
                CC.print("[dim]No TODOs yet.[/dim]")
                return True
            lines = [f"{'[x]' if t.get('done') else '[ ]'} {t.get('task', '?')}" for t in todos]
            CC.print(Panel(Text("\n".join(lines)), title="[bold yellow]TODOs[/bold yellow]", border_style="yellow"))
        except Exception:
            CC.print("[dim]Could not read TODOs.[/dim]")
        return True

    async def _load_session(self) -> bool:
        if not self.orchestrator:
            CC.print("[red]System not initialized.[/red]")
            return True

        try:
            sessions = memory_core._session_db.list_sessions(20)
        except Exception as e:
            CC.print(f"[red]Error listing sessions: {self._escape(e)}[/red]")
            return True

        if not sessions:
            CC.print("[dim]No sessions found.[/dim]")
            return True

        CC.print(f"\n{_acm('AVAILABLE SESSIONS')}:")
        for i, s in enumerate(sessions[:10], 1):
            title = s.get('title') or 'Untitled'
            msgcnt = s.get('message_count', 0)
            start_ts = s.get('started_at')
            if start_ts:
                try:
                    started = datetime.fromtimestamp(start_ts).strftime('%m-%d %H:%M')
                except Exception:
                    started = str(start_ts)
            else:
                started = ''
            CC.print(f"[{i}] {self._escape(title)} ({msgcnt} msgs) [{self._escape(started)}]")

        try:
            choice = IntPrompt.ask("Load Session #", choices=[str(i) for i in range(1, min(10, len(sessions)) + 1)])
            idx = int(choice) - 1
            selected = sessions[idx]
            session_id = selected['id']
        except (ValueError, IndexError, KeyError):
            CC.print("[red]Invalid selection[/red]")
            return True

        try:
            session = memory_core._session_db.load_session(session_id)
            if not session:
                CC.print("[red]Session not found.[/red]")
                return True

            self.orchestrator.history.clear()
            memory_core.clear_short_term()

            history = _conversation(session.get("messages", []))

            self.orchestrator.history = history
            memory_core.set_active_session(session_id)
            self.orchestrator._active_session_id = session_id

            for msg in reversed(history):
                content = msg.get("content", "")
                if isinstance(content, str) and content.startswith("AGENT:"):
                    agent_id = content.split(":", 1)[1].strip()
                    if agent_id and agent_id != "None":
                        self.orchestrator._last_agent_id = agent_id
                        base_name = agent_id.split("_")[0].lower() if "_" in agent_id else agent_id
                        if base_name:
                            self.orchestrator.skill_name = base_name
                            try:
                                self.orchestrator.set_role(base_name)
                            except Exception:
                                pass
                        break

            reset_global_tokens()
            memory_core.clear_triggered_skills()
            in_tok, _ = count_tokens(history, "")
            add_global_tokens(in_tok)

            CC.print(f"[green]Loaded session: {self._escape(selected.get('title') or session_id)}[/green]")

        except Exception as e:
            CC.print(f"[red]Failed to load session: {self._escape(e)}[/red]")

        return True

    async def _compact_memory(self) -> bool:
        if not self.orchestrator:
            CC.print("[red]System not initialized.[/red]")
            return True
        history = self.orchestrator.history
        if len(history) <= 10:
            CC.print("[yellow]Not enough history to compact.[/yellow]")
            return True

        summary = self.orchestrator.get_history_summary(max_messages=8)
        CC.print(Panel(summary, title="History Summary", border_style="cyan"))

        system_msgs = [m for m in history if m.get("role") == "system"]
        recent = history[-6:]
        to_summarize = history[len(system_msgs):-6]

        CC.print(f"[cyan]Compacting {len(to_summarize)} messages...[/cyan]")

        transcript = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content', ''))[:500]}"
            for m in to_summarize
        )

        prompt = (
            "Create a compact session checkpoint. Output this structure:\n"
            "## Active Task\n[user's current request verbatim]\n"
            "## Progress\n[numbered list: what was done, include file paths]\n"
            "## In Progress\n[currently working on]\n"
            "## Blocked\n[blockers, include exact error text]\n"
            "## Next Steps\n[what remains]\n"
            "## Key Context\n[file paths, config values, specific data]\n\n"
            "Preserve exact file paths, error messages, and values.\n\n"
            + transcript
        )

        try:
            provider = self.orchestrator.provider
            result = await provider.generate_text(
                [{"role": "user", "content": prompt}],
                "You are a precise conversation summarizer. Output only the summary."
            )
            if isinstance(result, dict):
                summary = result.get("content", "Summary unavailable")
            else:
                summary = str(result)
            summary_msg = {"role": "user", "content": f"[CONTEXT SUMMARY]\n{summary[:2000]}"}
            self.orchestrator.history = system_msgs + [summary_msg] + recent
            reset_global_tokens()
            in_tok, _ = count_tokens(self.orchestrator.history, "")
            add_global_tokens(in_tok)
            
            await memory_core.add_memory(summary[:500], category="compacted", tier="recent")
            CC.print(f"[bold green]Compacted {len(to_summarize)} messages into 1 summary.[/bold green]")
        except Exception as e:
            CC.print(f"[red]Compaction failed: {self._escape(e)}[/red]")
        return True

    async def settings_menu(self) -> None:
        while True:
            console.clear()
            table = Table(title="CONFIGURATION", border_style=C)
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Parameter", style=C)
            table.add_column("Value", style="green")

            table.add_row("1", "Provider", self.config.provider.upper())
            table.add_row("2", "Model", get_model_name(self.config.provider, self.config))
            table.add_row("3", "API Key", "********" if self._has_api_key() else "[red]NOT SET[/red]")
            table.add_row("4", "Debug Mode", str(self.config.debug_mode))
            table.add_row("5", "Critic Enabled", str(self.config.critic_enabled))
            table.add_row("6", "History Trim", f"{'ON' if self.config.history_trim_enabled else 'OFF'} (max: {self.config.history_trim_size})")
            table.add_row("7", "Memory Enabled", str(self.config.memory_enabled))
            dream_status = "ON" if self.config.dream_enabled else "OFF"
            table.add_row("8", "Dreaming", f"{dream_status} ({self.config.dream_interval // 60}min)")
            table.add_row("9", "MCP Servers", "Manage...")
            table.add_row("10", "Path Whitelist", "Manage...")
            table.add_row("11", "Accent Color", C)
            table.add_row("12", "Permission Mode", self.config.permission_mode)
            table.add_row("13", "Model Tier", str(self.config.active_model_tier or "None").upper())
            table.add_row("14", "Advanced (all settings)", f"{len(config_entries(self.config))} parameters")

            CC.print(Align.center(table))
            CC.print(Align.center("[dim]ID to edit, 'b' to return[/dim]"))

            try:
                choice = Prompt.ask(_acm("Config"))
                if choice.lower() in ['back', 'b', 'q', '/q', '/exit']:
                    break

                handlers = {
                    '1': self._select_provider,
                    '2': self._select_model,
                    '3': self._input_api_key,
                    '4': lambda: self._toggle_config('debug_mode'),
                    '5': lambda: self._toggle_config('critic_enabled'),
                    '6': self._history_trim_menu,
                    '7': lambda: self._toggle_config('memory_enabled'),
                    '8': self._dream_menu,
                    '9': self._mcp_menu,
                    '10': self._whitelist_menu,
                    '11': self._select_accent_color,
                    '12': self._permission_menu,
                    '13': self._select_tier,
                    '14': self._advanced_settings_menu,
                }

                if choice in handlers:
                    handler = handlers[choice]
                    result = handler()
                    if asyncio.iscoroutine(result):
                        await result

            except (KeyboardInterrupt, EOFError):
                break

    def _toggle_config(self, attr: str) -> None:
        current = getattr(self.config, attr, False)
        setattr(self.config, attr, not current)
        self.config.save()
        if self.orchestrator:
            self.orchestrator.invalidate_cache()

    def _select_accent_color(self) -> None:
        global C
        colors = ["purple", "blue", "green", "red", "yellow", "cyan", "magenta", "white"]
        current = C
        CC.print("\n[bold]ACCENT COLOR[/bold]")
        for i, c in enumerate(colors, 1):
            marker = " ← current" if c == current else ""
            CC.print(f"  [{i}] [{c}]■■■[/{c}] {c}{marker}")
        try:
            default_idx = colors.index(current) + 1 if current in colors else 1
            choice = IntPrompt.ask("Color", default=default_idx)
            idx = choice - 1
            if 0 <= idx < len(colors):
                C = colors[idx]
                self.config.accent_color = colors[idx]
                self.config.save()
                CC.print(f"[{colors[idx]}]✓ Accent: {colors[idx]}[/{colors[idx]}]")
                if self._app:
                    self._app = self._build_tui()
        except (ValueError, IndexError):
            pass

    def _permission_menu(self) -> None:
        modes = ["ASK", "AUTO", "DENY"]
        CC.print("\n[bold]PERMISSION MODE[/bold]")
        for i, m in enumerate(modes, 1):
            marker = " ← current" if m == self.config.permission_mode else ""
            CC.print(f"  [{i}] {m}{marker}")
        try:
            choice = IntPrompt.ask("Mode", default=1)
            if 1 <= choice <= 3:
                self.config.permission_mode = modes[choice - 1]
                self.config.save()
                CC.print(f"[green]Permission mode: {modes[choice - 1]}[/green]")
        except (ValueError, IndexError):
            pass

    def _advanced_settings_menu(self) -> None:
        rows = config_entries(self.config)
        while True:
            console.clear()
            CC.print(f"[bold]{_acm('ALL SETTINGS')}[/bold]")
            CC.print("[dim]Enter a number to change it, or 'b' to go back.[/dim]\n")
            table = Table(border_style=C)
            table.add_column("#", style="cyan", no_wrap=True)
            table.add_column("Parameter", style=C)
            table.add_column("Value", style="green")
            for i, (path, value) in enumerate(rows, 1):
                table.add_row(str(i), self._escape(path), self._escape(value))
            CC.print(table)
            choice = Prompt.ask(_acm("Setting"))
            if choice.lower() in ("b", "back", "q"):
                return
            try:
                path, current = rows[int(choice) - 1]
            except (ValueError, IndexError):
                continue
            raw = Prompt.ask(f"{path} [dim]({self._escape(current)})[/dim]", default="")
            if not raw.strip():
                continue
            old_provider, old_mode = self.config.provider, self.config.mode
            try:
                message = set_config_value(self.config, path, raw)
            except Exception as exc:
                CC.print(f"[red]{self._escape(str(exc))}[/red]")
                Prompt.ask("[dim]Enter to continue[/dim]", default="")
                continue
            if self._apply_live_config(path, old_provider, old_mode) is not False:
                self.config.save()
                CC.print(f"[green]✓ {self._escape(message)}[/green]")
            rows = config_entries(self.config)

    def _run_menu(self, title: str, items: List[tuple]) -> None:
        while True:
            console.clear()
            CC.print(f"[bold]{title}[/bold]")
            for i, (label, _) in enumerate(items, 1):
                CC.print(f"  [{i}] {label}")
            CC.print("  [b] Back")

            choice = Prompt.ask(_acm("Choice"))
            if choice.lower() in ('b', 'back'):
                break
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(items):
                    items[idx][1]()
            except (ValueError, IndexError):
                pass

    def _history_trim_menu(self) -> None:
        def toggle():
            self._toggle_config('history_trim_enabled')
            state = 'ON' if self.config.history_trim_enabled else 'OFF'
            CC.print(f"[green]History Trim: {state}[/green]")

        def set_size():
            try:
                size = IntPrompt.ask("Trim size", default=self.config.history_trim_size)
                self.config.history_trim_size = max(5, min(100, size))
                self.config.save()
                CC.print(f"[green]Trim size set to {self.config.history_trim_size}[/green]")
            except ValueError:
                pass

        status = 'enabled' if self.config.history_trim_enabled else 'disabled'
        self._run_menu(
            f"History Trim Settings (Current: {status}, Size: {self.config.history_trim_size})",
            [("Toggle Enable/Disable", toggle), ("Set Trim Size", set_size)]
        )

    def _sessions_menu(self) -> bool:
        if not self.orchestrator:
            CC.print("[red]System not initialized.[/red]")
            return True

        try:
            sessions = memory_core._session_db.list_sessions(20)
        except Exception as e:
            CC.print(f"[red]Error listing sessions: {self._escape(e)}[/red]")
            return True

        if not sessions:
            CC.print("[dim]No sessions found.[/dim]")
            return True

        table = Table(title="Recent Sessions", show_header=True, box=None)
        table.add_column("#", style="cyan", no_wrap=True)
        table.add_column("Title", style="green")
        table.add_column("Msgs", style="yellow")
        table.add_column("Started", style="blue")

        for i, s in enumerate(sessions[:10], 1):
            title = s.get('title') or 'Untitled'
            msgcnt = s.get('message_count', 0)
            ts = s.get('started_at')
            if ts:
                try:
                    started = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
                except Exception:
                    started = str(ts)
            else:
                started = ''
            table.add_row(str(i), Text(title), str(msgcnt), started)

        CC.print(table)
        return True

    async def _dream_menu(self) -> None:
        from src.core.memory import Dreamer
        while True:
            running = hasattr(self, '_dreamer') and self._dreamer._running
            CC.print(f"\n{_acm('DREAMING')}")
            CC.print(f"  Status: [{'green' if running else 'red'}]{'ON' if running else 'OFF'}[/{'green' if running else 'red'}]")
            CC.print(f"  Interval: {self.config.dream_interval // 60} minutes")
            CC.print("[dim]1=Toggle ON/OFF  2=Set interval  b=Back[/dim]")
            try:
                choice = Prompt.ask(_acm("Dream")).strip()
                if choice.lower() in ('b', 'back', 'q'):
                    break
                elif choice == '1':
                    await self._toggle_dream()
                elif choice == '2':
                    minutes = IntPrompt.ask("Interval (minutes)", default=self.config.dream_interval // 60)
                    self.config.dream_interval = max(60, minutes * 60)
                    self.config.save()
                    if hasattr(self, '_dreamer'):
                        self._dreamer.interval = self.config.dream_interval
                    CC.print(f"[green]Interval set to {self.config.dream_interval // 60} minutes[/green]")
            except (KeyboardInterrupt, EOFError):
                break

    def _has_api_key(self) -> bool:
        from src.config import _get_all_presets, resolve_provider
        p = resolve_provider(self.config.provider)
        if p in ("pollinations", "ollama", "lmstudio", "koboldcpp"):
            return True
        preset = _get_all_presets(self.config).get(p) or {}
        env = preset.get("api_key_env") or f"{p.upper().replace('-', '_')}_API_KEY"
        if os.environ.get(env):
            return True
        section = getattr(self.config, "gemini" if p == "google" else p, None)
        return bool(section and getattr(section, "api_key", None))

    async def _search_pick(self, title: str, entries: List[tuple], note: str = "", allow_custom: bool = False) -> Optional[str]:
        if not entries:
            return None
        CC.print(f"\n{_acm(title)}")
        CC.print(f"[dim]{len(entries)} options · type to filter · ↑↓ choose · Enter selects · Esc cancels[/dim]")
        if note:
            CC.print(f"[dim]{note}[/dim]")
        completer = _RankedCompleter(entries)
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            buf = event.current_buffer
            state = buf.complete_state
            if state and state.completions:
                choice = state.current_completion or state.completions[0]
                event.app.exit(result=choice.text)
                return
            typed = buf.text.strip()
            event.app.exit(result=completer.exact(typed) or completer.best(typed) or (typed if allow_custom else None))

        session = PromptSession(
            completer=completer,
            complete_while_typing=True,
            bottom_toolbar=" type to search · ↑↓ move · Enter selects · Ctrl+C cancels ",
        )
        try:
            picked = await session.prompt_async("> ", key_bindings=kb)
        except (KeyboardInterrupt, EOFError):
            return None
        picked = (picked or "").strip()
        return picked or None

    async def _select_model(self) -> None:
        from src.config import resolve_provider
        p = resolve_provider(self.config.provider)
        current = get_model_name(p, self.config)
        CC.print(f"[dim]Current model: {self._escape(str(current))}[/dim]")
        if p in ("ollama", "lmstudio", "koboldcpp"):
            models = self._list_local_models()
        else:
            try:
                models = list_live_models(p, self.config) or ModelCatalog.list_models(p)
            except Exception:
                models = []
        models = list(dict.fromkeys(str(m) for m in models if m))
        if not models:
            CC.print("[yellow]no catalog — type the model name[/yellow]")
            try:
                new_model = await PromptSession().prompt_async(f"Model [{current}]: ")
            except (KeyboardInterrupt, EOFError):
                return
            new_model = (new_model or "").strip() or current
        else:
            entries = []
            for model in models:
                info = ModelCatalog.get_info(p, model) or {}
                ctx = (info.get("limit") or {}).get("context")
                cost = info.get("cost") or {}
                meta = f"{ctx // 1000}k ctx · ${cost.get('input', '?')}/${cost.get('output', '?')} per M"
                entries.append((model, model, meta))
            picked = await self._search_pick(
                "SELECT MODEL", entries,
                note=f"provider: {p} · type any name to use a model outside the catalog",
                allow_custom=True,
            )
            if picked is None or not str(picked).strip():
                return
            new_model = str(picked).strip()
        if not new_model or new_model == current:
            return
        self._apply_model(p, new_model)

    def _apply_model(self, provider: str, model: str) -> None:
        self.config.provider_models[provider] = model
        try:
            self.config.save()
        except Exception:
            pass
        if self.orchestrator:
            try:
                self.orchestrator.provider = get_provider(provider, self.config)
                self.orchestrator.invalidate_cache()
            except Exception:
                pass
        CC.print(f"[bold green]✓ Model: {self._escape(model)}[/bold green]")

    def _handle_model(self, args: Optional[str]) -> Any:
        name = (args or "").strip()
        if not name:
            return self._select_model()
        from src.config import resolve_provider
        self._apply_model(resolve_provider(self.config.provider), name)
        return True

    def _handle_tier(self, args: Optional[str]) -> Any:
        name = (args or "").strip().upper()
        tiers = dict(self.config.model_tiers)
        if not name:
            return self._select_tier()
        if name not in tiers:
            CC.print(f"[red]Unknown tier: {self._escape(name)}[/red]\n[dim]Available: {', '.join(tiers)}. Set tier models with /set model_tiers.<tier>.<provider> <model>[/dim]")
            return True
        self.config.active_model_tier = name
        self.config.save()
        if self.orchestrator:
            try:
                self.orchestrator.provider = get_provider(self.config.provider, self.config)
                self.orchestrator.invalidate_cache()
            except Exception:
                pass
        CC.print(f"[bold green]✓ Tier: {name}[/bold green]")
        return True

    async def _select_tier(self) -> None:
        tiers = dict(self.config.model_tiers)
        current = str(self.config.active_model_tier or "None").upper()
        entries = []
        for name, table in tiers.items():
            models = ", ".join(f"{k}={v}" for k, v in (table or {}).items()) or "no models set"
            notes = "current" if name == current else (models[:48] + "…" if len(models) > 48 else models)
            entries.append((name, name, notes))
        picked = await self._search_pick("SELECT TIER", entries,
                                         note="tiers map each provider to a model; set with /set model_tiers.<tier>.<provider> <model>")
        if not picked:
            return
        self._handle_tier(str(picked))

    async def _select_provider(self) -> None:
        from src.config import resolve_provider
        providers = list_providers()
        current = resolve_provider(self.config.provider)
        entries = []
        for meta in providers:
            notes = []
            if meta.name == current:
                notes.append("current")
            if meta.requires_key and not meta.available:
                notes.append("needs key")
            entries.append((meta.display_name, meta.name, " · ".join(notes)))
        picked = await self._search_pick("SELECT PROVIDER", entries)
        if not picked:
            return
        name = str(picked)
        meta = next((m for m in providers if m.name == name), None)
        if meta is None:
            CC.print(f"[red]Unknown provider: {self._escape(name)}[/red]")
            return
        old = self.config.provider
        if meta.name not in ("pollinations", "local", "ollama", "lmstudio", "koboldcpp"):
            self.config.provider = meta.name
            if not self._has_api_key():
                CC.print(f"[yellow]{meta.name} requires an API key.[/yellow]")
                self._input_api_key()
                if not self._has_api_key():
                    self.config.provider = old
                    CC.print(f"[yellow]Stayed on {self._escape(old.upper())} — no key.[/yellow]")
                    return
        if self.orchestrator:
            if self.orchestrator.switch_provider(meta.name):
                self.config.save()
                CC.print(f"[bold green]✓ Provider: {meta.name.upper()}[/bold green]")
            else:
                self.config.provider = old
                CC.print(f"[red]Switch failed — stayed on {old.upper()}[/red]")
        else:
            self.config.save()
            CC.print(f"[green]Provider set: {meta.name.upper()}[/green]")
        return

    def _list_local_models(self) -> list[str]:
        urls = {
            "ollama": "http://localhost:11434/api/tags",
            "lmstudio": "http://localhost:1234/api/v1/models",
            "koboldcpp": "http://localhost:5001/v1/models",
        }
        url = urls.get(self.config.provider)
        if not url:
            return []
        try:
            r = httpx.get(url, timeout=3)
            r.raise_for_status()
            d = r.json()
            if self.config.provider == "ollama":
                return [m.get("name", "") for m in d.get("models", []) if m.get("name")]
            return [m.get("id", "") for m in d.get("data", []) if m.get("id")]
        except Exception:
            CC.print(
                f"[yellow]Could not reach {self.config.provider} — "
                f"is it running?[/yellow]"
            )
            return []

    def _input_api_key(self) -> None:
        from src.config import env_key_for, resolve_provider, save_env_key
        p = resolve_provider(self.config.provider)
        section = getattr(self.config, "gemini" if p == "google" else p, None)
        current = getattr(section, "api_key", None) if section else None
        env_name = env_key_for(p)
        if not current:
            current = os.environ.get(env_name, "")
        masked = (current[:6] + "…" + current[-4:]) if len(current) > 12 else ("(none)" if not current else current)
        CC.print(f"[dim]Current {p} key: {masked}[/dim]")
        key = Prompt.ask(f"{p.title()} API Key", password=True, default="")
        if not key:
            CC.print("[dim]kept existing[/dim]" if current else "[yellow]no key entered[/yellow]")
            return
        stored = False
        try:
            stored = save_env_key(p, key)
        except Exception as exc:
            CC.print(f"[red]Could not write .env: {self._escape(str(exc))}[/red]")
        os.environ[env_name] = key
        if section is not None and hasattr(section, "api_key"):
            section.api_key = key
            key = ""
        if self.orchestrator:
            try:
                self.orchestrator.provider = get_provider(p, self.config)
                self.orchestrator.invalidate_cache()
            except Exception as exc:
                CC.print(f"[yellow]key set, provider refresh failed: {self._escape(str(exc))}[/yellow]")
        CC.print(f"[dim]{env_name} saved to {ENV_PATH}[/dim]" if stored else "[yellow]key held in this session only[/yellow]")

    def _check_server_installed(self, cfg) -> bool:
        """Local servers need their executable; remote servers only need a URL."""
        import shutil
        if getattr(cfg, "kind", "stdio") != "stdio":
            return bool(cfg.url)
        if cfg.command == "internal":
            from src.tools import TOOL_REGISTRY
            return bool(TOOL_REGISTRY)
        if cfg.args and cfg.args[0] == "-m" and len(cfg.args) > 1:
            try:
                __import__(cfg.args[1])
                return True
            except ImportError:
                return False
        if cfg.command == "npx":
            return bool(shutil.which("npx") or shutil.which("npx.cmd"))
        return bool(shutil.which(cfg.command) or shutil.which(f"{cfg.command}.exe"))

    def _install_server(self, name: str, cfg) -> bool:
        import subprocess
        import shutil
        if cfg.kind != "stdio":
            CC.print(f"[green]'{self._escape(name)}' is remote ({cfg.kind}) — nothing to install.[/green]")
            return True
        if cfg.args and cfg.args[0] == "-m" and len(cfg.args) > 1:
            module = cfg.args[1]
            CC.print(f"\n[yellow]Installing Python module: {module}...[/yellow]")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", module],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0:
                    CC.print(f"[green]{self._escape(name)} installed![/green]")
                    return True
                else:
                    CC.print(f"[red]Install failed:[/red] {result.stderr[:200]}")
                    return False
            except Exception as e:
                CC.print(f"[red]Install error:[/red] {e}")
                return False
        elif cfg.command == "npx":
            if not (shutil.which("npx") or shutil.which("npx.cmd")):
                CC.print(f"[red]Cannot enable '{self._escape(name)}': npx is not installed or not in PATH.[/red]")
                CC.print(f"[yellow]Install Node.js to use npx-based MCP servers.[/yellow]")
                return False
            CC.print(f"[green]'{self._escape(name)}' ready — package will be fetched via npx on first connection.[/green]")
            return True
        else:
            CC.print(f"[red]Unknown install method for '{cfg.command}'.[/red]")
            return False

    async def _mcp_menu(self) -> None:
        while True:
            console.clear()
            table = Table(title="MCP SERVERS", border_style=C)
            table.add_column("ID", style="dim", width=4)
            table.add_column("Server", style="bold")
            table.add_column("Status", style=C)
            table.add_column("Installed", style=C)
            table.add_column("Runtime", style=C)
            table.add_column("Command", style="dim")
            servers = list(self.config.mcp_servers.keys())
            live = getattr(self.orchestrator, "_mcp", None)
            for i, name in enumerate(servers, 1):
                cfg = self.config.mcp_servers[name]
                status = "[green]ON[/green]" if cfg.enabled else "[dim]OFF[/dim]"
                installed = self._check_server_installed(cfg)
                inst_display = "[green]Yes[/green]" if installed else "[red]No[/red]"
                if cfg.kind != "stdio":
                    cmd = Text(f"{cfg.kind}: {cfg.url}", style="cyan")
                elif cfg.command == "internal":
                    cmd = Text("(internal)", style="cyan")
                else:
                    cmd = Text(cfg.command)
                lsrv = live.servers.get(name) if live else None
                if lsrv and lsrv.connected:
                    runtime = "[green]LIVE[/green]"
                elif lsrv and lsrv._bg_task and not lsrv._bg_task.done():
                    runtime = "[yellow]STARTING[/yellow]"
                elif cfg.enabled and installed:
                    runtime = "[dim]IDLE[/dim]"
                else:
                    runtime = "[red]DEAD[/red]"
                
                tone = "dim" if not cfg.enabled else ("green" if installed else "red")
                table.add_row(str(i), Text(name, style=tone), status, inst_display, runtime, cmd)

            CC.print(table)
            CC.print("\n[dim]ID to toggle/install, 'b' to back[/dim]")
            CC.print("[dim]Red = not installed or DEAD. Select to install/retry.[/dim]")

            choice = Prompt.ask(_acm("MCP"))
            if choice.lower() in ('b', 'back'):
                break
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(servers):
                    name = servers[idx]
                    cfg = self.config.mcp_servers[name]
                    installed = self._check_server_installed(cfg)
                    if not installed:
                        if self._install_server(name, cfg):
                            cfg.enabled = True
                            self.config.save()
                            self._reload_mcp()
                            CC.print(f"[green]{self._escape(name)} enabled![/green]")
                        Prompt.ask("[dim]Press Enter[/dim]")
                    else:
                        cfg.enabled = not cfg.enabled
                        self.config.save()
                        self._reload_mcp()
                        CC.print(f"[yellow]Toggled {self._escape(name)}: {'ON' if cfg.enabled else 'OFF'}[/yellow]")
            except ValueError:
                pass

    async def chat_loop(self) -> None:
        if not self.orchestrator:
            await self._init_system()
        self._pending_input = asyncio.Queue()
        self._should_exit = False
        self._modal_cmd = None
        self.print_banner(show_menu=False)
        CC.print(f"\n[{C}]Ready. /help · /stop to halt a run · Ctrl+D exit.[/{C}]\n")
        while not self._should_exit:
            self._app = self._build_tui()
            async def _process_loop():
                while not self._should_exit:
                    try:
                        user_input = await asyncio.wait_for(self._pending_input.get(), timeout=0.2)
                    except asyncio.TimeoutError:
                        continue
                    if not user_input.strip():
                        continue
                    if user_input.lower() in ('exit', 'quit', 'q', '/q'):
                        self._should_exit = True
                        self._app.exit()
                        return
                    if user_input.lower() in ('back', '/back', 'menu'):
                        self._should_exit = True
                        self._app.exit()
                        return
                    if user_input.startswith("/"):
                        self._modal_cmd = user_input
                        self._app.exit()
                        return
                    await self._process_task(user_input)
            process_task = asyncio.create_task(_process_loop())
            try:
                with patch_stdout(raw=True):
                    await self._app.run_async()
            except (KeyboardInterrupt, EOFError):
                self._should_exit = True
            finally:
                process_task.cancel()
                try:
                    await process_task
                except asyncio.CancelledError:
                    pass
            if self._modal_cmd:
                cmd = self._modal_cmd
                self._modal_cmd = None
                await self.handle_system_command(cmd)

    async def _process_task(self, user_input: str) -> None:
        if not self.orchestrator:
            return
        if not self.orchestrator._active_session_id:
            self.orchestrator.new_session(title=user_input[:80])
        self.transcript.begin()
        self._agent_task = asyncio.create_task(self._do_run(user_input))
        self._agent_running = True
        deferred: list[str] = []
        if self._app:
            self._app.invalidate()
        try:
            while not self._agent_task.done():
                while True:
                    try:
                        msg = self._pending_input.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if self._perm_future and not self._perm_future.done() and msg.strip().lower() in ("y", "n", "yes", "no"):
                        self._perm_future.set_result(msg.strip().lower() in ("y", "yes"))
                        continue
                    text = msg.strip()
                    if text.lower() in ("/stop", "stop"):
                        if self._agent_task and not self._agent_task.done():
                            self.orchestrator.request_interrupt()
                            self._agent_task.cancel()
                    elif text.startswith("/"):
                        deferred.append(text)
                    else:
                        self.orchestrator.inject_user_message(text)
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            self._agent_task.cancel()
            raise
        finally:
            self._agent_running = False
            async with self._active_lock:
                self._user_active = False
                self.orchestrator._user_active = False
            if self._app:
                self._app.invalidate()
        try:
            await self._agent_task
        except (asyncio.CancelledError, Exception):
            pass
        for m in self.orchestrator.coordinator.drain():
            self._pending_input.put_nowait(m)
        for cmd in deferred:
            await self.handle_system_command(cmd)

    async def _do_run(self, user_input: str) -> None:
        async with self._active_lock:
            self._user_active = True
            self.orchestrator._user_active = True
        if self.orchestrator.history:
            try:
                tokens_used = count_tokens(self.orchestrator.history, "")[0]
                ctx_limit = (
                    ModelCatalog.get_context(
                        self.config.provider,
                        get_model_name(self.config.provider, self.config),
                    )
                    or 128_000
                )
                if tokens_used > int(ctx_limit * 0.90):
                    CC.print(f"[yellow]Context near limit ({tokens_used:,} / {ctx_limit:,}) – consider /clear[/yellow]")
            except Exception:
                pass
        CC.print(f"[bold]you[/bold] {self._escape(user_input)}", soft_wrap=True)
        try:
            response = await self.orchestrator.run(user_input)
            self.transcript.end()
            if response and "LOOP_COMPLETE" in response and getattr(self, "_loop_complete", None) is not None:
                self._loop_complete = True
                response = response.replace("LOOP_COMPLETE", "").strip()
            if response and response.strip() and not self._answer_shown():
                shown = response if len(response) < 12000 else response[:12000] + f"\n\n… [{len(response) - 12000} chars truncated]"
                print_block(shown, panel=rich_on(), emit=CC.print)
                self.transcript.media.render_mentions(shown)
        except asyncio.CancelledError:
            self.transcript.end()
            raise
        except Exception as e:
            self.transcript.end()
            CC.print(f"[bold red]Error:[/bold red] {self._escape(str(e))}")
        finally:
            async with self._active_lock:
                self._user_active = False
                self.orchestrator._user_active = False

    def _answer_shown(self) -> bool:
        """Single paint decision: the agent says whether the stream showed it."""
        return bool(getattr(self.orchestrator, "answer_streamed", False))

    def _escape(self, text: str) -> str:
        return str(text).replace("[", "\\[")

    async def run(self) -> None:
        while True:
            self.print_banner()
            try:
                choice = Prompt.ask(_acm("Enter"), default="1")

                if choice.lower() in ('q', '/q', 'exit', 'quit', '3'):
                    await self._cleanup()
                    raise SystemExit(0)
                elif choice.startswith("/"):
                    if not self.orchestrator:
                        await self._init_system()
                    await self.handle_system_command(choice)
                elif choice == "1":
                    await self.chat_loop()
                elif choice == "2":
                    await self.settings_menu()
                else:
                    console.print("[yellow]Invalid choice. Use 1, 2, 3 or /q to exit.[/yellow]")

            except (KeyboardInterrupt, EOFError):
                await self._cleanup()
                raise SystemExit(0)

    async def _cleanup(self) -> None:
        if self._mcp_warm_task and not self._mcp_warm_task.done():
            self._mcp_warm_task.cancel()
            await asyncio.gather(self._mcp_warm_task, return_exceptions=True)
        if self.orchestrator:
            try:
                await self.orchestrator.cleanup()
            except RuntimeError:
                pass
        import gc
        gc.collect()

    def _whitelist_menu(self) -> None:
        from src.config import get_allowed_roots, add_allowed_root, remove_allowed_root
        while True:
            console.clear()
            roots = get_allowed_roots()
            table = Table(title="PATH WHITELIST", border_style=C)
            table.add_column("#", style="cyan", width=4)
            table.add_column("Path", style="green")
            table.add_row("0", str(Path.cwd().resolve()) + " [dim](auto)[/dim]")
            for i, r in enumerate(roots):
                table.add_row(str(i + 1), r)
            CC.print(table)
            CC.print("\n[dim]add <path>  |  remove <#>  |  'b' to return[/dim]")
            try:
                choice = Prompt.ask(_acm("Whitelist")).strip()
                if choice.lower() in ('b', 'back', 'q'):
                    break
                if choice.lower().startswith("add "):
                    result = add_allowed_root(choice[4:].strip())
                    CC.print(f"[green]{result}[/green]" if "✓" in result else f"[red]{result}[/red]")
                elif choice.lower().startswith("remove "):
                    try:
                        idx = int(choice[7:].strip()) - 1
                        CC.print(f"[green]{remove_allowed_root(idx)}[/green]")
                    except ValueError:
                        CC.print("[red]Invalid number.[/red]")
            except (KeyboardInterrupt, EOFError):
                break