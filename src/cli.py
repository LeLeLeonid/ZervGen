import asyncio
import os
import signal
import sys
import threading
import warnings
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt, IntPrompt
from rich.align import Align
from rich.table import Table

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=ResourceWarning)

from src.config import load_config, MODES
from src.core.memory import memory_core
from src.core.orchestrator import Orchestrator
from src.core.provider import get_provider, get_model_name, list_providers
from src.skills_loader import get_all_roles
from src.utils import format_token_display, get_global_tokens, reset_global_tokens, count_tokens, add_global_tokens

console = Console()
_interrupt_event = threading.Event()


def _signal_handler(signum, frame):
    _interrupt_event.set()
    console.print("\n[yellow]Exit requested...[/yellow]")


class CC:
    @staticmethod
    def print(*args, **kwargs):
        for arg in args:
            console.print(Align.center(arg, vertical="middle"))


class ZervGenCLI:
    def __init__(self, config=None):
        self.config = config or load_config()
        self.orchestrator: Optional[Orchestrator] = None
        self._current_task: Optional[asyncio.Task] = None
        self._user_active: bool = False
        self._active_lock = threading.Lock()
        self._show_usage: bool = True

    async def _init_system(self) -> None:
        try:
            provider = get_provider(self.config.provider, self.config)
            self.orchestrator = Orchestrator(provider, self.config)
            if self.config.auto_mode and not self.orchestrator._auto_mode:
                asyncio.create_task(self.orchestrator.auto_loop())
        except Exception as e:
            console.print(f"[bold red]Failed to initialize system:[/bold red] {self._escape(str(e))}")
            raise

    def print_banner(self) -> None:
        console.clear()
        banner = """
[bold purple]███████╗███████╗██████╗ ██╗   ██╗ ██████╗ ███████╗███╗   ██╗[/bold purple]
[bold purple]╚══███╔╝██╔════╝██╔══██╗██║   ██║██╔════╝ ██╔════╝████╗  ██║[/bold purple]
[bold blue]  ███╔╝ █████╗  ██████╔╝██║   ██║██║  ███╗█████╗  ██╔██╗ ██║[/bold blue]
[bold blue] ███╔╝  ██╔══╝  ██╔══██╗╚██╗ ██╔╝██║   ██║██╔══╝  ██║╚██╗██║[/bold blue]
[bold purple]███████╗███████╗██║  ██║ ╚████╔╝ ╚██████╔╝███████╗██║ ╚████║[/bold purple]
[bold purple]╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚══════╝╚═╝  ╚═══╝[/bold purple]
[dim]v1.5.0 - Hierarchical Core[/dim]
        """
        try:
            mem_count = memory_core.get_stats().get('now_count', 0)
        except Exception:
            mem_count = "?"

        stats_text = f"[dim]🧠 Memory: [cyan]{mem_count}[/cyan] | 🔌 Provider: [cyan]{self.config.provider.upper()}[/cyan] | 🤖 Model: [cyan]{get_model_name(self.config.provider, self.config)}[/cyan][/dim]"

        menu = Table(box=None, show_header=False, padding=(0, 2))
        menu.add_column(justify="right", style="bold purple")
        menu.add_column(justify="left")
        menu.add_row("[1]", "💬  Start Chat\n")
        menu.add_row("[2]", "⚙️  Configuration")
        menu.add_row("[3]", "🚪  Exit\n")

        layout = Table.grid(padding=1, expand=True)
        layout.add_column(justify="center")
        layout.add_row(Panel(banner, border_style="purple", expand=False))
        layout.add_row(stats_text)
        layout.add_row("")
        layout.add_row(menu)

        console.print(Align.center(layout, vertical="middle"))

    def _toggle_auto(self, enable: bool) -> None:
        if not self.orchestrator:
            return
        current = self.orchestrator._auto_mode
        self.orchestrator.toggle_auto(enable)
        if enable and not current:
            asyncio.create_task(self.orchestrator.auto_loop())
        elif not enable:
            self.orchestrator.interrupt_event.set()

    async def handle_system_command(self, cmd: str) -> bool:
        if not self.orchestrator:
            CC.print("[red]System not initialized.[/red]")
            return True

        parts = cmd.split()
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else None

        commands = {
            "/help": self._show_help,
            "/history": self._show_history,
            "/todo": self._show_todos,
            "/time": lambda: CC.print(f"[cyan]{datetime.now()}[/cyan]"),
            "/clear": self._clear_session,
            "/agent": self._agent_menu,
            "/memory": lambda: CC.print(Panel(str(memory_core.get_stats()), title="Memory Stats", border_style="green")),
            "/usage": self._toggle_usage,
            "/compact": self._compact_memory,
            "/load": self._load_session,
            "/status": lambda: CC.print(Panel(str(self.orchestrator.get_mode_status()), title="Status", border_style="cyan")),
            "/stop": lambda: CC.print(f"[yellow]{self.orchestrator.stop_auto()}[/yellow]"),
        }

        if command in commands:
            return await commands[command]()

        if command == "/auto":
            enable = not self.orchestrator._auto_mode if args is None else args.lower() in ("on", "true", "1")
            self._toggle_auto(enable)
            CC.print(f"[bold {'green' if enable else 'yellow'}]Auto mode {'ON' if enable else 'OFF'}[/bold {'green' if enable else 'yellow'}]")
            return True

        if command == "/provider":
            return self._handle_provider(args)
        if command == "/role":
            return self._handle_role(args)
        if command == "/mode":
            return await self._handle_mode(args)

        toggles = {"/critic": ("critic_enabled", "Critic"), "/trim": ("history_trim_enabled", "History Trim")}
        if command in toggles:
            cfg_attr, label = toggles[command]
            current = getattr(self.config, cfg_attr, False)
            if not args:
                CC.print(f"[bold]{label}:[/bold] {'enabled' if current else 'disabled'}")
                return True
            enable = args.lower() in ("on", "enable", "true", "1")
            setattr(self.config, cfg_attr, enable)
            self.config.save()
            CC.print(f"[bold {'green' if enable else 'yellow'}]{label} {'ENABLED' if enable else 'DISABLED'}[/bold {'green' if enable else 'yellow'}]")
            return True

        CC.print(f"[red]Unknown command: {cmd}[/red]\n[dim]Type /help for commands.[/dim]")
        return True

    def _clear_session(self) -> bool:
        self.orchestrator.history = []
        self.orchestrator.skill_name = "system"
        self.orchestrator._last_agent_id = None
        self.orchestrator.agents.clear()
        memory_core.clear_current_session()
        reset_global_tokens()
        CC.print("[yellow]Session cleared.[/yellow]")
        return True

    def _toggle_usage(self) -> bool:
        self._show_usage = not self._show_usage
        CC.print(f"[bold cyan]Token Counter: {'ON' if self._show_usage else 'OFF'}[/bold cyan]")
        return True

    def _handle_provider(self, args: Optional[str]) -> bool:
        if not args:
            CC.print(f"[bold]Current Provider:[/bold] {self.config.provider.upper()}")
            return True
        if self.orchestrator.switch_provider(args):
            CC.print(f"[bold green][+] Provider switched to: {args.upper()}[/bold green]")
        else:
            CC.print(f"[bold red][!] Failed to switch provider to: {args}[/bold red]")
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

    async def _handle_mode(self, args: Optional[str]) -> bool:
        if not args:
            status = self.orchestrator.get_mode_status()
            CC.print(f"\n[bold]Current Mode:[/bold] {status['mode']} | Auto: {'ON' if self.orchestrator._auto_mode else 'OFF'}")
            CC.print("\n[bold cyan]SELECT MODE:[/bold cyan]")
            CC.print("  [1] ASK    - Questions & explanations")
            CC.print("  [2] PLAN   - Architecture & planning")
            CC.print("  [3] BUILD   - Code generation")
            CC.print("  [4] DEBUG  - Troubleshooting")
            CC.print("  [b] Back")

            choice = Prompt.ask("[bold purple]Mode[/bold purple]")
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
        CC.print("[bold purple]SELECT AGENT[/bold purple]\n")
        for i, (name, cfg) in enumerate(role_list, 1):
            marker = " [cyan](current)[/cyan]" if name == self.orchestrator.skill_name else ""
            CC.print(f"  [{i}] {name}{marker}")

        choice = Prompt.ask("[bold purple]Choice[/bold purple]")
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
        roles = "\n".join(f"- [purple]{k}[/purple]: {v.description}" for k, v in get_all_roles().items())
        modes = "\n".join(f"- [green]{k}[/green]: {v['description']}" for k, v in MODES.items())

        help_text = f"""[b]System Commands:[/b]
/history - Show recent conversation context
/time    - Show current system time
/clear   - Clear current session history
/agent   - Select agent submenu
/memory  - Show memory statistics
/load    - Load your sessions
/auto    - Toggle AUTO mode
/usage   - Toggle the Token Counter
/compact - Compact short-term memory into long-term storage
/q       - Quit application

[bold cyan]PROVIDER:[/bold cyan]
/provider [name] - Show your provider

[bold cyan]MODE:[/bold cyan]
/mode    - Show selection menu (BUILD, ASK, DEBUG, PLAN)

[bold cyan]TOGGLES:[/bold cyan]
/critic on|off   - Toggle Critic (Self-Interrogation) [dim]({'on' if self.config.critic_enabled else 'off'})[/dim]
/trim on|off     - Toggle History Trimming [dim]({'on' if self.config.history_trim_enabled else 'off'})[/dim]

[b]ROLES:[/b]
{roles}

[b]MODES:[/b]
{modes}"""
        console.print(Panel(help_text, title="Help", border_style="blue"))
        return True

    def _show_history(self) -> bool:
        if self.orchestrator and self.orchestrator.history:
            CC.print(Panel(str(self.orchestrator.history), title="Recent History"))
        else:
            CC.print("[dim]No history yet.[/dim]")
        return True

    def _show_todos(self) -> bool:
        if self.orchestrator and hasattr(self.orchestrator, '_active_todos'):
            todos = self.orchestrator._active_todos
            if todos:
                CC.print(Panel("\n".join(f"- {t}" for t in todos), title="[bold yellow]Active TODOs[/bold yellow]", border_style="yellow"))
            else:
                CC.print("[dim]No active TODOs.[/dim]")
        else:
            CC.print("[dim]No orchestrator available.[/dim]")
        return True

    async def _load_session(self) -> bool:
        sessions_dir = Path("tmp/memory/sessions")
        if not sessions_dir.exists():
            CC.print("[red]No sessions found.[/red]")
            return True

        files = sorted([f for f in os.listdir(sessions_dir) if f.endswith(".jsonl")], reverse=True)
        if not files:
            CC.print("[dim]No session logs found.[/dim]")
            return True

        CC.print("\n[bold purple]AVAILABLE SESSIONS:[/bold purple]")
        for i, f in enumerate(files[:10]):
            CC.print(f"[{i+1}] {f}")

        try:
            choice = IntPrompt.ask("Load Session #", choices=[str(i+1) for i in range(len(files[:10]))])
            selected = files[choice-1]

            memory_core.clear_current_session()
            memory_core._short_term.clear()
            loaded_hist, loaded_mode, last_role, short_term_items = memory_core.load_session(selected)

            self.orchestrator.history = loaded_hist
            self.orchestrator.set_mode(loaded_mode)
            if last_role and last_role != "system":
                self.orchestrator.set_role(last_role)
            for item in short_term_items:
                memory_core._short_term.append(item)
            memory_core._session_id = selected.replace("session_", "").replace(".jsonl", "")
            memory_core._session_file = Path(f"tmp/memory/sessions/{selected}")

            for msg in reversed(loaded_hist):
                content = msg.get("content", "")
                if isinstance(content, str) and content.startswith("AGENT:"):
                    agent_id = content.split(":", 1)[1].strip()
                    if agent_id and agent_id != "None":
                        self.orchestrator._last_agent_id = agent_id
                        base_name = agent_id.split("_")[0].lower() if "_" in agent_id else agent_id
                        if base_name:
                            self.orchestrator.skill_name = base_name
                            self.orchestrator.set_role(base_name)
                        break

            reset_global_tokens()
            in_tok, _ = count_tokens(loaded_hist, "")
            add_global_tokens(in_tok)

            if loaded_mode and loaded_mode != "orchestrator":
                self.orchestrator.set_mode(loaded_mode.upper())
                CC.print(f"[bold cyan][+] Mode restored: {loaded_mode.upper()}[/bold cyan]")
            if last_role and last_role != "system":
                self.orchestrator.skill_name = last_role
                CC.print(f"[bold cyan][+] Last agent restored: {last_role.capitalize()}[/bold cyan]")
            CC.print(f"[green]Session '{selected}' loaded ({len(loaded_hist)} msgs).[/green]")
        except Exception as e:
            CC.print(f"[red]Load error: {self._escape(str(e))}[/red]")
        return True

    async def _compact_memory(self) -> bool:
        if not self.orchestrator:
            CC.print("[red]System not initialized.[/red]")
            return True

        recent = memory_core.get_recent_memories(limit=50)
        if not recent:
            CC.print("[yellow]No short-term memories to compact.[/yellow]")
            return True

        summary = f"Compacted {len(recent)} memories from session {memory_core._session_id}"
        memory_core.add_memory(summary, category="compacted")
        memory_core.clear_short_term()
        CC.print(f"[bold green][+] Memory compacted: {len(recent)} items -> long-term storage[/bold green]")
        return True

    def settings_menu(self) -> None:
        while True:
            console.clear()
            table = Table(title="CONFIGURATION", border_style="purple")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Parameter", style="magenta")
            table.add_column("Value", style="green")

            table.add_row("1", "Provider", self.config.provider.upper())
            table.add_row("2", "Model", get_model_name(self.config.provider, self.config))
            table.add_row("3", "API Key", "********" if self._has_api_key() else "[red]NOT SET[/red]")
            table.add_row("4", "Debug Mode", str(self.config.debug_mode))
            table.add_row("5", "Critic Enabled", str(self.config.critic_enabled))
            table.add_row("6", "History Trim", f"{'ON' if self.config.history_trim_enabled else 'OFF'} (max: {self.config.history_trim_size})")
            table.add_row("7", "Memory Enabled", str(self.config.memory_enabled))
            table.add_row("8", "Auto Mode", f"{'ON' if self.orchestrator._auto_mode else 'OFF'} ({self.config.auto_interval // 60} min)")
            table.add_row("9", "MCP Servers", "Manage...")

            CC.print(table)
            CC.print("\n[dim]ID to edit, 'b' to return[/dim]")

            try:
                choice = Prompt.ask("[bold purple]Config[/bold purple]")
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
                    '8': self._auto_interval_menu,
                    '9': self._mcp_menu,
                }

                if choice in handlers:
                    handlers[choice]()

            except (KeyboardInterrupt, EOFError):
                break

    def _toggle_config(self, attr: str) -> None:
        current = getattr(self.config, attr, False)
        setattr(self.config, attr, not current)
        self.config.save()

    def _run_menu(self, title: str, items: List[tuple]) -> None:
        while True:
            console.clear()
            CC.print(f"[bold]{title}[/bold]")
            for i, (label, _) in enumerate(items, 1):
                CC.print(f"  [{i}] {label}")
            CC.print("  [b] Back")

            choice = Prompt.ask("[bold purple]Choice[/bold purple]")
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

        def set_size():
            try:
                size = IntPrompt.ask("Trim size", default=self.config.history_trim_size)
                self.config.history_trim_size = max(5, min(100, size))
                self.config.save()
            except ValueError:
                pass

        status = 'enabled' if self.config.history_trim_enabled else 'disabled'
        self._run_menu(
            f"History Trim Settings (Current: {status}, Size: {self.config.history_trim_size})",
            [("Toggle Enable/Disable", toggle), ("Set Trim Size", set_size)]
        )

    def _auto_interval_menu(self) -> None:
        def set_interval():
            try:
                current_min = self.config.auto_interval // 60
                minutes = IntPrompt.ask("Interval (minutes)", default=current_min, show_default=True)
                self.config.auto_interval = max(60, min(3600, minutes * 60))
                self.config.save()
                CC.print(f"[green]Auto interval set to {self.config.auto_interval // 60} minutes[/green]")
            except ValueError:
                pass

        self._run_menu(
            f"Auto Interval Settings (Current: {self.config.auto_interval // 60} minutes)",
            [("Set interval (minutes)", set_interval)]
        )

    def _has_api_key(self) -> bool:
        provider_settings = getattr(self.config, self.config.provider, None)
        return bool(provider_settings and getattr(provider_settings, "api_key", None))

    def _select_provider(self) -> None:
        providers = list_providers()
        console.clear()
        CC.print("[bold purple]SELECT PROVIDER:[/bold purple]\n")
        for i, meta in enumerate(providers, 1):
            CC.print(f"[{i}] {meta.display_name}")

        choice = IntPrompt.ask("Select", choices=[str(i) for i in range(1, len(providers)+1)])
        selected = providers[choice-1]

        if self.orchestrator:
            self.orchestrator.switch_provider(selected.name)
        else:
            self.config.provider = selected.name
            self.config.save()

        if selected.name not in ("pollinations", "local"):
            self._input_api_key()

    def _select_model(self) -> None:
        provider_settings = getattr(self.config, self.config.provider, None)
        if provider_settings is None:
            CC.print("[red]No settings for current provider.[/red]")
            return

        if self.config.provider == "gemini":
            self._select_gemini_model()
            return

        current = getattr(provider_settings, "model", "default")
        new_model = Prompt.ask("Model", default=current)
        if hasattr(provider_settings, "model"):
            provider_settings.model = new_model
            self.config.save()

    def _select_gemini_model(self) -> None:
        from src.providers.gemini import fetch_available_models

        if not self.config.gemini.api_key:
            CC.print("[red]API Key required to fetch models.[/red]")
            Prompt.ask("Press Enter...")
            return

        try:
            with console.status("[bold purple]Fetching models...[/bold purple]"):
                models = fetch_available_models(self.config.gemini.api_key)
            console.clear()
            CC.print("[bold purple]Available Models:[/bold purple]\n")
            for i, m in enumerate(models, 1):
                CC.print(f"[{i}] {m}")
            choice = IntPrompt.ask("Select", choices=[str(i) for i in range(1, len(models)+1)])
            self.config.gemini.model = models[choice-1]
            self.config.save()
        except Exception as e:
            CC.print(f"[red]Error: {self._escape(str(e))}[/red]")

    def _input_api_key(self) -> None:
        provider_settings = getattr(self.config, self.config.provider, None)
        if provider_settings and hasattr(provider_settings, "api_key"):
            provider_settings.api_key = Prompt.ask(f"{self.config.provider.title()} API Key")
            self.config.save()

    def _mcp_menu(self) -> None:
        while True:
            console.clear()
            table = Table(title="MCP SERVERS", border_style="purple")
            table.add_column("ID", style="dim", width=4)
            table.add_column("Server", style="bold green")
            table.add_column("Status", style="magenta")
            table.add_column("Command", style="dim")

            servers = list(self.config.mcp_servers.keys())
            for i, name in enumerate(servers, 1):
                cfg = self.config.mcp_servers[name]
                status = "[green]ON[/green]" if cfg.enabled else "[red]OFF[/red]"
                cmd = "[cyan](internal)[/cyan]" if cfg.command == "internal" else cfg.command
                table.add_row(str(i), name, status, cmd)

            CC.print(table)
            CC.print("\n[dim]ID to toggle, 'b' to back[/dim]")

            choice = Prompt.ask("[bold purple]MCP[/bold purple]")
            if choice.lower() in ('b', 'back'):
                break
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(servers):
                    name = servers[idx]
                    self.config.mcp_servers[name].enabled = not self.config.mcp_servers[name].enabled
                    self.config.save()
                    CC.print(f"[yellow]Toggled {name}[/yellow]")
            except ValueError:
                pass

    async def chat_loop(self) -> None:
        if not self.orchestrator:
            await self._init_system()

        self.print_banner()
        CC.print("\n[dim]Ready. Commands: /help. Ctrl+C to interrupt.[/dim]\n")

        while True:
            _interrupt_event.clear()
            if self.orchestrator:
                self.orchestrator.interrupt_event.clear()

            try:
                user_input = Prompt.ask(f"[dim]📊 Tokens {get_global_tokens()}[/dim]") if self._show_usage else Prompt.ask("")
            except KeyboardInterrupt:
                CC.print("\n[yellow]Returning to menu...[/yellow]")
                return

            if not user_input.strip():
                continue
            if user_input.lower() in ('exit', 'quit', 'q', '/q'):
                raise SystemExit(0)
            if user_input.lower() in ('back', '/back', 'menu'):
                return
            if user_input.startswith("/"):
                await self.handle_system_command(user_input)
                continue

            await self._process_task(user_input)

    async def _process_task(self, user_input: str) -> None:
        if not self.orchestrator:
            console.print("[bold red]Error: System not initialized[/bold red]")
            return

        with self._active_lock:
            self._user_active = True
            self.orchestrator._user_active = True
        try:
            response = await self.orchestrator.process(user_input)
            if response and response.strip():
                CC.print(Panel(Markdown(response), border_style="purple"))
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopping task...[/yellow]")
            self.orchestrator.request_interrupt()
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {self._escape(str(e))}")
        finally:
            with self._active_lock:
                self._user_active = False
                self.orchestrator._user_active = False

    def _escape(self, text: str) -> str:
        return text.replace('[', '\\[')

    async def run(self) -> None:
        while True:
            self.print_banner()
            try:
                choice = Prompt.ask("[bold purple]Enter[/bold purple]", default="1")

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
                    self.settings_menu()
                else:
                    console.print("[yellow]Invalid choice. Use 1, 2, 3 or /q to exit.[/yellow]")

            except (KeyboardInterrupt, EOFError):
                await self._cleanup()
                raise SystemExit(0)

    async def _cleanup(self) -> None:
        if self.orchestrator:
            self.orchestrator._auto_mode = False
            try:
                await self.orchestrator.cleanup()
            except RuntimeError:
                pass


def main():
    signal.signal(signal.SIGINT, _signal_handler)
    cli = ZervGenCLI()
    try:
        asyncio.run(cli.run())
    except SystemExit:
        pass


if __name__ == "__main__":
    main()
