import asyncio
import json
import os
import signal
import sys
import threading
import warnings
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt, IntPrompt, Confirm
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
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.output import create_output
from prompt_toolkit.patch_stdout import patch_stdout


warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=ResourceWarning)

from src.config import load_config, MODES
from src.core.memory import memory_core
from src.core.orchestrator import Orchestrator
from src.core.provider import get_provider, get_model_name, list_providers
from src.skills_loader import get_all_roles
from src.utils import get_global_tokens, reset_global_tokens, count_tokens, add_global_tokens, ModelCatalog

console = Console()
_interrupt_event = threading.Event()
C = "purple"


def _acm(text: str) -> str:
    return f"[bold][{C}]{text}[/{C}][/bold]"


class CC:
    @staticmethod
    def print(*args, **kwargs):
        for arg in args:
            console.print(Align.center(arg))


class ZervGenCLI:
    def __init__(self, config=None):
        global C
        self.config = config or load_config()
        C = self.config.accent_color
        self.orchestrator: Optional[Orchestrator] = None
        self._current_task: Optional[asyncio.Task] = None
        self._user_active: bool = False
        self._active_lock = asyncio.Lock()
        self._show_usage: bool = True
        self._pt_session = PromptSession(history=FileHistory("tmp/.zervgen_history"))
        self._app: Optional[Application] = None
        self._agent_running = False
        self._should_exit = False
        self._pending_input: asyncio.Queue = None
        self._output_lines: List[str] = []
        self._agent_task: Optional[asyncio.Task] = None
        self._modal_cmd: Optional[str] = None
        self._status_text = ""
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
            "stop": lambda a: CC.print("[yellow]Use Ctrl+C to interrupt.[/yellow]"),
            "dream": lambda a: self._toggle_dream(),
            "mcp": lambda a: self._cmd_mcp(),
            "provider": lambda a: self._handle_provider(a),
            "role": lambda a: self._handle_role(a),
            "mode": lambda a: self._handle_mode(a),
            "critic": lambda a: self._toggle_cfg_cmd("critic_enabled", "Critic", a),
            "trim": lambda a: self._toggle_cfg_cmd("history_trim_enabled", "History Trim", a),
            "contract": lambda a: self._handle_contract(a),
            "search": lambda a: self._handle_search(a),
            "rollback": lambda a: self._handle_rollback(a),
            "reload": lambda a: self._cmd_reload(),
            "trace": lambda a: self._cmd_trace(a),
        }

    async def _init_system(self) -> None:
        chosen = self.config.provider
        chain = [chosen] + [p for p in ("openrouter", "groq", "ollama", "lmstudio", "pollinations") if p != chosen]
        last_err = None
        for name in chain:
            try:
                provider = get_provider(name, self.config)
                self.orchestrator = Orchestrator(provider, self.config, memory=memory_core)
                self.orchestrator.status_cb = self._on_status
                if name != chosen:
                    self.config.provider = name
                    CC.print(f"[yellow]'{chosen}' unavailable ({last_err}) — running on '{name}' this session. Config untouched.[/yellow]")
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
                buf.complete_next()  # Cycle through dropdown
            else:
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
            if self._show_usage:
                return [("class:prompt", f"[{get_global_tokens()}] > ")]
            return [("class:prompt", "> ")]

        def get_toolbar():
            if self._status_text:
                return [("class:toolbar.busy", f" {self._status_text} ")]
            if self.orchestrator:
                s = self.orchestrator.get_mode_status()
                return [("class:toolbar", f" {s['mode']}:{s['role']} {self.config.provider.upper()} ")]
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
[bold blue]███████╗███████╗██████╗ ██╗   ██╗ ██████╗ ███████╗███╗   ██╗[/bold blue]
[bold blue]╚══███╔╝██╔════╝██╔══██╗██║   ██║██╔════╝ ██╔════╝████╗  ██║[/bold blue]
[bold {C}]  ███╔╝ █████╗  ██████╔╝██║   ██║██║  ███╗█████╗  ██╔██╗ ██║[/bold {C}]
[bold {C}] ███╔╝  ██╔══╝  ██╔══██╗╚██╗ ██╔╝██║   ██║██╔══╝  ██║╚██╗██║[/bold {C}]
[bold blue]███████╗███████╗██║  ██║ ╚████╔╝ ╚██████╔╝███████╗██║ ╚████║[/bold blue]
[bold blue]╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚══════╝╚═╝  ╚═══╝[/bold blue]
[dim]v1.6.0 - [i]!Stable. Simple. Smart.[/i][/dim]
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
        args = parts[1] if len(parts) > 1 else None
        handler = self._cmd_table.get(raw_cmd)
        if handler:
            res = handler(args)
            if asyncio.iscoroutine(res):
                await res
            return True
        CC.print(f"[red]Unknown command: {cmd}[/red]\n[dim]Type /help for commands.[/dim]")
        return True

    def _clear_session(self, hard: bool = False, all_sessions: bool = False) -> bool:
        sid = self.orchestrator._active_session_id
        if all_sessions:
            memory_core._session_db.delete_all_sessions()
            CC.print("[yellow]All sessions deleted from disk.[/yellow]")
        elif hard and sid:
            memory_core._session_db.delete_session(sid)
            CC.print(f"[yellow]Session {sid[:8]} deleted from disk.[/yellow]")
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

    async def _cmd_mcp(self) -> bool:
        self.config.mcp_enabled = not self.config.mcp_enabled
        self.config.save()
        state = "ENABLED" if self.config.mcp_enabled else "DISABLED"
        CC.print(f"[bold {'green' if self.config.mcp_enabled else 'red'}]MCP: {state}[/bold {'green' if self.config.mcp_enabled else 'red'}]")
        if self.config.mcp_enabled and self.orchestrator:
            if self.orchestrator._mcp:
                await self.orchestrator._mcp.cleanup()
            self.orchestrator._mcp = None
            self.orchestrator._mcp_initialized = False
            await self.orchestrator._init_mcp()
        return True

    def _cmd_reload(self) -> bool:
        from src.skills_loader import skill_index
        skill_index.reload()
        CC.print(f"[green]Reloaded {len(skill_index.skills)} skills[/green]")
        return True

    def _cmd_trace(self, args: Optional[str]) -> bool:
        cfg = load_config()
        if args is None:
            cfg.trace_enabled = not cfg.trace_enabled
        else:
            cfg.trace_enabled = args.lower() in ("on", "1", "true", "enable")
        cfg.save()
        CC.print(f"[bold]Trace: {'ON' if cfg.trace_enabled else 'OFF'}[/bold]")
        return True

    def _toggle_cfg_cmd(self, attr: str, label: str, args: Optional[str]) -> None:
        current = getattr(self.config, attr, False)
        if not args:
            CC.print(f"[bold]{label}:[/bold] {'enabled' if current else 'disabled'}")
            return
        enable = args.lower() in ("on", "enable", "true", "1")
        setattr(self.config, attr, enable)
        self.config.save()
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

    def _toggle_usage(self) -> bool:
        self._show_usage = not self._show_usage
        CC.print(f"[bold][{C}]Token Counter: {'ON' if self._show_usage else 'OFF'}[/{C}][/bold]")
        return True

    def _handle_provider(self, args: Optional[str]) -> bool:
        model = get_model_name(self.config.provider, self.config)
        CC.print(f"[bold]Provider:[/bold] {self.config.provider.upper()}  [dim]Model: {model}[/dim]")
        return True

    def _handle_role(self, args: Optional[str]) -> bool:
        if not args:
            CC.print(f"[bold]Current Role:[/bold] {self.orchestrator.get_mode_status()['role']}")
            return True
        if self.orchestrator.set_role(args):
            CC.print(f"[bold green][+] ROLE UPDATED: {args.upper()}[/bold green]")
        else:
            CC.print(f"[bold red][!] ERROR:[/bold red] Role '{args}' not found")
        return True

    async def _handle_contract(self, args: Optional[str]) -> bool:
        from src.skills_loader import skill_index
        if not args:
            CC.print("[yellow]Usage: /contract <skill_name>[/yellow]")
            CC.print(f"\nAvailable skills: {', '.join(skill_index.skills.keys())}")
            return True
        skill = skill_index.get(args)
        if not skill:
            CC.print(f"[red]Skill '{args}' not found[/red]")
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
            table = Table(title=f"Search: {args}", border_style=C)
            table.add_column("#", style="cyan")
            table.add_column("Snippet")
            table.add_column("Role", style="green")
            table.add_column("Time", style="dim")
            for i, r in enumerate(results, 1):
                c = r.get("content", "")
                table.add_row(
                    str(i),
                    (c[:100] + "...") if len(c) > 100 else c,
                    r.get("role", "?"),
                    datetime.fromtimestamp(r.get("timestamp", 0)).strftime("%m-%d %H:%M")
                    if isinstance(r.get("timestamp"), (int, float))
                    else "",
                )
            CC.print(table)
        except Exception as e:
            CC.print(f"[red]Error: {e}[/red]")
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
            CC.print(f"[red]Error: {e}[/red]")
        return True

    async def _handle_mode(self, args: Optional[str]) -> bool:
        if not args:
            status = self.orchestrator.get_mode_status()
            CC.print(f"\n[bold]Current Mode:[/bold] {status['mode']}")
            CC.print(f"\n{_acm('SELECT MODE')}")
            CC.print("  [1] ASK    - Questions & explanations")
            CC.print("  [2] PLAN   - Architecture & planning")
            CC.print("  [3] BUILD   - Code generation")
            CC.print("  [4] DEBUG  - Troubleshooting")
            CC.print("  [b] Back")

            choice = Prompt.ask(_acm("Mode"))
            mode_map = {'1': 'ASK', '2': 'PLAN', '3': 'BUILD', '4': 'DEBUG'}
            if choice.lower() in ('b', 'back'):
                return True
            if choice in mode_map:
                if self.orchestrator.set_mode(mode_map[choice]):
                    CC.print(f"[bold green][+] MODE SHIFTED: {mode_map[choice]}[/bold green]")
                else:
                    CC.print("[bold red][!] ERROR: Mode change failed[/bold red]")
            else:
                CC.print("[yellow]Invalid choice.[/yellow]")
            return True

        if self.orchestrator.set_mode(args.upper()):
            CC.print(f"[bold green][+] MODE SHIFTED: {args.upper()}[/bold green]")
        else:
            CC.print(f"[bold red][!] ERROR: Mode '{args.upper()}' invalid[/bold red]")
        return True

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
            CC.print(f"  [{i}] {name}{marker}")

        choice = Prompt.ask(_acm("Choice"))
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(role_list):
                name, cfg = role_list[idx]
                self.orchestrator.set_role(name)
                self.orchestrator._log(name.capitalize(), f"AGENT: {name}", "state")
                CC.print(f"[green]Agent switched to: {name}[/green]")
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
/memory  - Show memory statistics
/load    - Load your sessions
/usage   - Toggle the Token Counter
/compact - Compact short-term memory into long-term storage
/reload  - Reload your skills
/mcp     - Model Context Protocol
/q       - Quit application

[bold][{C}]PROVIDER:[/{C}][/bold]
/provider - Show current provider & model

[bold][{C}]MODE:[/{C}][/bold]
/mode    - Show selection menu (BUILD, ASK, DEBUG, PLAN)

[bold][{C}]TOGGLES:[/{C}][/bold]
/critic on|off   - Toggle Self-Interrogation [dim]({'on' if self.config.critic_enabled else 'off'})[/dim]
/trim on|off     - Toggle History Trimming [dim]({'on' if self.config.history_trim_enabled else 'off'})[/dim]
/trace on|off    - Live agent event one-liners [dim]({'on' if self.config.trace_enabled else 'off'})[/dim]

[b]ROLES:[/b]
{roles}

[b]MODES:[/b]
{modes}"""
        console.print(Panel(help_text, title="Help", border_style=f"{C}"))
        return True

    def _show_history(self) -> bool:
        if self.orchestrator and self.orchestrator.history:
            summary = self.orchestrator.get_history_summary()
            CC.print(Panel(summary, title="Recent History"))
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
            CC.print(f"[red]Error listing sessions: {e}[/red]")
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
                except:
                    started = str(start_ts)
            else:
                started = ''
            CC.print(f"[{i}] {title} ({msgcnt} msgs) [{started}]")

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

            history = []
            for msg in session.get("messages", []):
                if msg.get("tool_name"):
                    continue
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role not in ("system", "user", "assistant"):
                    role = "assistant"
                history.append({"role": role, "content": content})

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
                            except:
                                pass
                        break

            reset_global_tokens()
            memory_core.clear_triggered_skills()
            in_tok, _ = count_tokens(history, "")
            add_global_tokens(in_tok)

            CC.print(f"[green]Loaded session: {selected.get('title') or session_id}[/green]")

        except Exception as e:
            CC.print(f"[red]Failed to load session: {e}[/red]")

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
            CC.print(f"[red]Compaction failed: {e}[/red]")
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

            CC.print(table)
            CC.print("\n[dim]ID to edit, 'b' to return[/dim]")

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
            CC.print(f"[red]Error listing sessions: {e}[/red]")
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
                except:
                    started = str(ts)
            else:
                started = ''
            table.add_row(str(i), title, str(msgcnt), started)

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
        p = self.config.provider
        if p in ("pollinations", "local", "ollama", "lmstudio", "koboldcpp"):
            return True
        ps = getattr(self.config, p, None)
        if ps and getattr(ps, "api_key", None):
            return True
        if os.environ.get(f"{p.upper()}_API_KEY"):
            return True
        return False

    def _select_model(self) -> None:
        p = self.config.provider
        ps = getattr(self.config, p, None)
        if p == "gemini":
            self._select_gemini_model()
            return
        current = get_model_name(p, self.config)
        CC.print(f"[dim]Current model: {current}[/dim]")
        models = []
        if p in ("ollama", "lmstudio", "koboldcpp"):
            models = self._list_local_models()
        else:
            try:
                models = ModelCatalog.list_models(p) or []
            except Exception:
                models = []
        if models:
            f = Prompt.ask("Filter (blank=all)", default="").strip()
            if f:
                lf = f.lower()
                models = [m for m in models if lf in m.lower()]
        if not models:
            CC.print("[yellow]no catalog — type name[/yellow]")
            new_model = Prompt.ask("Model", default=current)
        elif len(models) == 1:
            new_model = models[0]
        else:
            for i, m in enumerate(models, 1):
                info = ModelCatalog.get_info(p, m) or {}
                ctx = (info.get("limit") or {}).get("context")
                cost = info.get("cost") or {}
                in_cost = cost.get('input', '?')
                out_cost = cost.get('output', '?')
                tag = f"[dim]{ctx // 1000 if ctx else '?'}k ctx · ${in_cost}/M in · ${out_cost}/M out[/dim]"
                mk = " ->" if m == current else "   "
                CC.print(f"{mk}[{i}] {m}  {tag}")
            CC.print(f"   [{len(models) + 1}] custom...")
            try:
                choice = IntPrompt.ask("Select", choices=[str(i) for i in range(1, len(models) + 2)])
            except (KeyboardInterrupt, EOFError):
                return
            new_model = Prompt.ask("Model", default=current) if choice == len(models) + 1 else models[choice - 1]
        if not new_model or new_model == current:
            return
        if ps and hasattr(ps, "model"):
            ps.model = new_model
            try:
                self.config.save()
            except Exception:
                pass
        else:
            os.environ[f"{p.upper()}_MODEL"] = new_model
            CC.print(f"[dim]applied now; to keep after restart add {p.upper()}_MODEL={new_model} to .env[/dim]")
        if self.orchestrator:
            try:
                self.orchestrator.provider = get_provider(p, self.config)
                self.orchestrator.invalidate_cache()
            except Exception:
                pass

    def _select_provider(self) -> None:
        providers = list_providers()
        console.clear()
        CC.print(f"{_acm('SELECT PROVIDER')}\n")
        for i, meta in enumerate(providers, 1):
            CC.print(f"  [{i}] {meta.display_name}")
        try:
            choice = IntPrompt.ask("Select", choices=[str(i) for i in range(1, len(providers) + 1)])
        except (KeyboardInterrupt, EOFError):
            return
        selected = providers[int(choice) - 1]
        old = self.config.provider
        if selected.name not in ("pollinations", "local", "ollama", "lmstudio", "koboldcpp"):
            self.config.provider = selected.name
            if not self._has_api_key():
                CC.print(f"[yellow]{selected.name} requires an API key.[/yellow]")
                self._input_api_key()
        self.config.provider = selected.name
        if self.orchestrator:
            if self.orchestrator.switch_provider(selected.name):
                self.config.save()
                CC.print(f"[bold green]✓ Provider: {selected.name.upper()}[/bold green]")
            else:
                self.config.provider = old
                CC.print(f"[red]Switch failed — stayed on {old.upper()}[/red]")
        else:
            self.config.save()
            CC.print(f"[green]Provider set: {selected.name.upper()}[/green]")

    def _select_gemini_model(self) -> None:
        try:
            from src.providers.gemini import fetch_available_models
        except ImportError:
            CC.print("[red]Gemini provider module not found.[/red]")
            CC.print("[yellow]Enter model name manually.[/yellow]")
            current = getattr(self.config.gemini, "model", "gemini-3.5-flash")
            new = Prompt.ask("Model", default=current)
            self.config.gemini.model = new
            self.config.save()
            return
        if not self.config.gemini.api_key:
            CC.print("[red]API Key required to fetch models.[/red]")
            self._input_api_key()
            if not self.config.gemini.api_key:
                return
        try:
            with console.status(_acm("Fetching models…")):
                models = fetch_available_models(self.config.gemini.api_key)
            if not models:
                CC.print("[yellow]No models returned.[/yellow]")
                return
            console.clear()
            CC.print(f"{_acm('Available Models')}\n")
            for i, m in enumerate(models, 1):
                CC.print(f"  [{i}] {m}")
            choice = IntPrompt.ask(
                "Select",
                choices=[str(i) for i in range(1, len(models) + 1)],
            )
            self.config.gemini.model = models[choice - 1]
            self.config.save()
            CC.print(f"[green]✓ Model: {models[choice - 1]}[/green]")
        except Exception as e:
            CC.print(f"[red]Error: {self._escape(str(e))}[/red]")

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
        p = self.config.provider
        ps = getattr(self.config, p, None)
        current = ""
        if ps and getattr(ps, "api_key", None):
            current = ps.api_key
        if not current:
            current = os.environ.get(f"{p.upper()}_API_KEY", "")
        masked = (current[:6] + "…" + current[-4:]) if len(current) > 12 else ("(none)" if not current else current)
        CC.print(f"[dim]Current {p} key: {masked}[/dim]")
        key = Prompt.ask(f"{p.title()} API Key", password=True, default="")
        if not key:
            CC.print("[dim]kept existing[/dim]" if current else "[yellow]no key entered[/yellow]")
            return
        os.environ[f"{p.upper()}_API_KEY"] = key
        try:
            from src.config import save_env_key
            save_env_key(p, key)
        except Exception:
            pass
        if ps and hasattr(ps, "api_key"):
            ps.api_key = key
            try:
                self.config.save()
            except Exception:
                pass
        if self.orchestrator:
            try:
                self.orchestrator.provider = get_provider(p, self.config)
                self.orchestrator.invalidate_cache()
            except Exception as e:
                CC.print(f"[yellow]key saved, refresh failed: {self._escape(str(e))}[/yellow]")
        CC.print("[dim]saved to .env — table updates on redraw[/dim]")

    def _check_server_installed(self, cfg) -> bool:
        import shutil
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
        if cfg.args and cfg.args[0] == "-m" and len(cfg.args) > 1:
            module = cfg.args[1]
            CC.print(f"\n[yellow]Installing Python module: {module}...[/yellow]")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", module],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0:
                    CC.print(f"[green]{name} installed![/green]")
                    return True
                else:
                    CC.print(f"[red]Install failed:[/red] {result.stderr[:200]}")
                    return False
            except Exception as e:
                CC.print(f"[red]Install error:[/red] {e}")
                return False
        elif cfg.command == "npx":
            if not (shutil.which("npx") or shutil.which("npx.cmd")):
                CC.print(f"[red]Cannot enable '{name}': npx is not installed or not in PATH.[/red]")
                CC.print(f"[yellow]Install Node.js to use npx-based MCP servers.[/yellow]")
                return False
            CC.print(f"[green]'{name}' ready — package will be fetched via npx on first connection.[/green]")
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
            from src.core.mcp_manager import MCPManager
            live = MCPManager(self.config)
            for i, name in enumerate(servers, 1):
                cfg = self.config.mcp_servers[name]
                status = "[green]ON[/green]" if cfg.enabled else "[dim]OFF[/dim]"
                installed = self._check_server_installed(cfg)
                inst_display = "[green]Yes[/green]" if installed else "[red]No[/red]"
                cmd = "[cyan](internal)[/cyan]" if cfg.command == "internal" else cfg.command
                lsrv = live.servers.get(name) if live else None
                if lsrv and lsrv.connected:
                    runtime = "[green]LIVE[/green]"
                elif lsrv:
                    runtime = "[red]DEAD[/red]"
                else:
                    runtime = "[dim]-[/dim]"
                
                if not cfg.enabled:
                    display_name = f"[dim]{name}[/dim]"
                elif installed:
                    display_name = f"[green]{name}[/green]"
                else:
                    display_name = f"[red]{name}[/red]"
                    
                table.add_row(str(i), display_name, status, inst_display, runtime, cmd)

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
                    if not installed and cfg.command != "internal":
                        if self._install_server(name, cfg):
                            cfg.enabled = True
                            self.config.save()
                            CC.print(f"[green]{name} enabled![/green]")
                        Prompt.ask("[dim]Press Enter[/dim]")
                    else:
                        cfg.enabled = not cfg.enabled
                        self.config.save()
                        CC.print(f"[yellow]Toggled {name}: {'ON' if cfg.enabled else 'OFF'}[/yellow]")
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
                        await self._cleanup()
                        raise SystemExit(0)
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
        else:
            self.orchestrator.status_cb = self._on_status
        if not self.orchestrator._active_session_id:
            self.orchestrator.new_session(title=user_input[:80])
        self._agent_task = asyncio.create_task(self._do_run(user_input))
        self._agent_running = True
        if self._app:
            self._app.invalidate()
        try:
            while not self._agent_task.done():
                while True:
                    try:
                        msg = self._pending_input.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if msg.strip().lower() in ("/stop", "stop"):
                        if self._agent_task and not self._agent_task.done():
                            self.orchestrator.request_interrupt()
                            self._agent_task.cancel()
                    else:
                        self.orchestrator.inject_user_message(msg)
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

        for m in list(self.orchestrator._injected):
            self.orchestrator._injected.remove(m)
            self._pending_input.put_nowait(m["content"])

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
        try:
            response = await self.orchestrator.run(user_input)
            if response and response.strip():
                shown = response if len(response) < 12000 else response[:12000] + f"\n\n… [{len(response) - 12000} chars truncated]"
                CC.print(Panel(Markdown(shown), border_style=C))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            CC.print(f"[bold red]Error:[/bold red] {self._escape(str(e))}")
        finally:
            async with self._active_lock:
                self._user_active = False
                self.orchestrator._user_active = False

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