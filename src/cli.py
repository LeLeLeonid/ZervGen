import sys
import asyncio
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt, IntPrompt
from rich.table import Table
from rich.align import Align

from src.config import load_config
from src.providers.pollinations import PollinationsProvider
from src.providers.gemini import GeminiProvider, fetch_available_models
from src.providers.openrouter import OpenRouterProvider
from src.providers.openai import OpenAIProvider
from src.providers.anthropic import AnthropicProvider
from src.core.orchestrator import Orchestrator
from src.core.memory import memory_core

console = Console()

class CC:
    @staticmethod
    def print(*args, **kwargs):
        for arg in args:
            console.print(Align.center(arg, vertical="middle"))

class ZervGenCLI:
    def __init__(self):
        self.config = load_config()
        self.orchestrator = None

    def _init_system(self):
        try:
            provider = self._get_provider()
            self.orchestrator = Orchestrator(provider, self.config)

            if self.config.mcp_enabled:
                loop = asyncio.get_event_loop()
                loop.create_task(self.orchestrator.mcp.connect_all())

        except Exception as e:
            CC.print(f"\n[bold red]SYSTEM INITIALIZATION ERROR: {e}[/bold red]")
            CC.print("[yellow]Falling back to Pollinations (Safe Mode)...[/yellow]")

            self.config.provider = "pollinations"
            provider = PollinationsProvider(self.config.pollinations)
            self.orchestrator = Orchestrator(provider, self.config)

            Prompt.ask("\n[bold white on red] Press Enter to acknowledge [/bold white on red]")

    def _get_provider(self):
        if self.config.provider == "gemini":
            return GeminiProvider(self.config.gemini)
        elif self.config.provider == "openrouter":
            return OpenRouterProvider(self.config.openrouter)
        elif self.config.provider == "openai":
            return OpenAIProvider(self.config.openai)
        elif self.config.provider == "anthropic":
            return AnthropicProvider(self.config.anthropic)
        else:
            return PollinationsProvider(self.config.pollinations)

    def print_banner(self):
        console.clear()
        banner_text = """
[bold purple]███████╗███████╗██████╗ ██╗   ██╗ ██████╗ ███████╗███╗   ██╗[/bold purple]
[bold purple]╚══███╔╝██╔════╝██╔══██╗██║   ██║██╔════╝ ██╔════╝████╗  ██║[/bold purple]
[bold blue]  ███╔╝ █████╗  ██████╔╝██║   ██║██║  ███╗█████╗  ██╔██╗ ██║[/bold blue]
[bold blue] ███╔╝  ██╔══╝  ██╔══██╗╚██╗ ██╔╝██║   ██║██╔══╝  ██║╚██╗██║[/bold blue]
[bold purple]███████╗███████╗██║  ██║ ╚████╔╝ ╚██████╔╝███████╗██║ ╚████║[/bold purple]
[bold purple]╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚══════╝╚═╝  ╚═══╝[/bold purple]
[dim]v1.4.1 - Lightning Core[/dim]
        """

        try:
            stats = memory_core.stats
            mem_count = stats.get('total_memories', 0)
        except: mem_count = "?"

        stats_grid = Table.grid(expand=True)
        stats_grid.add_column(justify="center", ratio=1)

        status_text = f"[dim]🧠 Memory: [cyan]{mem_count}[/cyan] | 🔌 Provider: [cyan]{self.config.provider.upper()}[/cyan] | 🤖 Model: [cyan]{self._get_model_name()}[/cyan][/dim]"
        stats_grid.add_row(status_text)

        menu_table = Table(box=None, show_header=False, padding=(0, 2))
        menu_table.add_column(justify="right", style="bold purple")
        menu_table.add_column(justify="left")
        menu_table.add_row("[1]", "Start Chat")
        menu_table.add_row("[2]", "Configuration")
        menu_table.add_row("[3]", "Exit")

        master_layout = Table.grid(padding=1, expand=True)
        master_layout.add_column(justify="center")

        master_layout.add_row(Panel(banner_text, border_style="purple", expand=False))
        master_layout.add_row(stats_grid)
        master_layout.add_row("")
        master_layout.add_row(menu_table)

        console.print(Align.center(master_layout, vertical="middle"))

    def _get_model_name(self):
        if self.config.provider == "openrouter": return self.config.openrouter.model.split("/")[-1]
        if self.config.provider == "openai": return self.config.openai.model
        if self.config.provider == "anthropic": return self.config.anthropic.model
        return "Default"

    def handle_system_command(self, cmd: str) -> bool:
        from src.config import MODES
        from src.skills_loader import get_all_roles
        import datetime
        valid_commands = ["/history", "/time", "/clear", "/memory", "/evolve", "/search", "/help", "/mode", "/role"]
        parts = cmd.split()
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else None

        if cmd == "/help":
            roles_map = get_all_roles()
            roles_list = "\n".join([f"- [cyan]{k}[/cyan]: {v.description}" for k, v in roles_map.items()])
            modes_list = "\n".join([f"- [green]{k}[/green]: {v['description']}" for k, v in MODES.items()])
            help_text = f"""
[bold]System Commands:[/bold]
/history - Show recent conversation context
/time    - Show current system time
/clear   - Clear short-term conversation history
/memory  - Show long-term memory statistics
/evolve  - Force memory consolidation/evolution
/search  - Semantic search in long-term memory
/load    - Load your sessions
/q     - Quit application
back     - Return to main menu

[bold white]AVAILABLE ROLES:[/bold white]
{roles_list}

[bold white]AVAILABLE MODES:[/bold white]
{modes_list}
            """
            console.print(Panel(help_text, title="Help", border_style="blue"))
            return True

        elif cmd == "/history":
            if self.orchestrator and self.orchestrator.history:
                CC.print(Panel(str(self.orchestrator.history[-3:]), title="Recent History (Last 3)"))
            else:
                CC.print("[dim]No history yet.[/dim]")
            return True

        elif cmd == "/time":
            CC.print(f"[cyan]{datetime.datetime.now()}[/cyan]")
            return True

        elif cmd == "/clear":
            if self.orchestrator:
                self.orchestrator.history = []
            CC.print("[yellow]Session History Cleared.[/yellow]")
            return True

        elif cmd == "/memory":
            stats = memory_core.get_stats()
            CC.print(Panel(stats, title="Memory System Stats", border_style="green"))
            return True

        elif cmd == "/evolve":
            with console.status("[bold purple]Triggering self-evolution...[/bold purple]"):
                result = memory_core.evolve()
            CC.print(Panel(result, title="Evolution Result", border_style="purple"))
            return True

        elif cmd == "/search":
            query = Prompt.ask("Search query")
            with console.status("[bold purple]Searching memory...[/bold purple]"):
                result = memory_core.search_memory(query, mode="semantic")
            CC.print(Panel(result, title="Search Results", border_style="cyan"))
            return True

        elif cmd == "/role":
            if not args:
                CC.print(f"[bold]Current Role:[/bold] {self.orchestrator.current_role}")
                return True
            
            if self.orchestrator.set_role(args):
                CC.print(f"[bold green]⚡ ROLE UPDATED: {args.upper()}[/bold green]")
                if hasattr(self.orchestrator, 'current_worker'):
                    delattr(self.orchestrator, 'current_worker')
            else:
                CC.print(f"[bold red]❌ ERROR:[/bold red] Role '{args}' not found in src/skills/")
            return True
        
        elif cmd == "/mode":
            if not args:
                CC.print(f"[bold]Current Mode:[/bold] {self.orchestrator.current_mode}")
                return True
            
            mode_key = args.upper()
            if self.orchestrator.set_mode(mode_key):
                CC.print(f"[bold green]⚡ MODE SHIFTED: {mode_key}[/bold green]")
                if hasattr(self.orchestrator, 'current_worker'):
                    delattr(self.orchestrator, 'current_worker')
            else:
                CC.print(f"[bold red]❌ ERROR:[/bold red] Mode '{mode_key}' invalid. Check /help")
            return True

        elif cmd == "/load":
            import os
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
                selected_file = files[choice-1]
                loaded_hist = memory_core.load_session_from_file(selected_file)
                self.orchestrator.history = loaded_hist
                CC.print(f"[green]Session '{selected_file}' loaded ({len(loaded_hist)} msgs).[/green]")
            except Exception as e:
                CC.print(f"[red]Load error: {e}[/red]")
            return True

        CC.print(f"[red]Unknown command: {cmd}[/red]")
        CC.print(f"[dim]Type /help for list of commands.[/dim]")
        return True

    def select_gemini_model(self):
        if not self.config.gemini.api_key:
            CC.print("[red]API Key required to fetch models.[/red]")
            Prompt.ask("Press Enter...")
            return

        try:
            with console.status("[bold purple]Fetching available models from Google...[/bold purple]"):
                models = fetch_available_models(self.config.gemini.api_key)

            console.clear()
            CC.print("[bold purple]Available Google Models:[/bold purple]\n")
            for idx, model in enumerate(models, 1):
                CC.print(f"[cyan]{idx}.[/cyan] {model}")

            CC.print("\n[dim]Enter number[/dim]")
            choice = IntPrompt.ask("Select", choices=[str(i) for i in range(1, len(models)+1)])
            self.config.gemini.model = models[choice - 1]
        except Exception as e:
            CC.print(f"[red]Error fetching models: {e}[/red]")
            Prompt.ask("Press Enter...")

    def settings_menu(self):
        while True:
            console.clear()
            table = Table(title="CONFIGURATION", border_style="purple")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Parameter", style="magenta")
            table.add_column("Value", style="green")

            table.add_row("1", "Active Provider", self.config.provider.upper())
            table.add_row("2", "Debug Mode", str(self.config.debug_mode))
            table.add_row("3", "Require Approval", str(self.config.require_approval))
            table.add_row("4", "Log Truncation", str(self.config.log_truncation))

            if self.config.provider == "gemini":
                cfg = self.config.gemini
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("5", "Model", cfg.model)
                table.add_row("6", "API Key", key_display)

            elif self.config.provider == "openrouter":
                cfg = self.config.openrouter
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("5", "Model", cfg.model)
                table.add_row("6", "API Key", key_display)
                table.add_row("8", "Vision Model", cfg.vision_model)

            elif self.config.provider == "openai":
                cfg = self.config.openai
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("5", "Model", cfg.model)
                table.add_row("6", "API Key", key_display)

            elif self.config.provider == "anthropic":
                cfg = self.config.anthropic
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("5", "Model", cfg.model)
                table.add_row("6", "API Key", key_display)

            else:
                cfg = self.config.pollinations
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("5", "Model", cfg.text_model)
                table.add_row("6", "API Key", key_display)
                table.add_row("8", "Voice", cfg.voice)

            table.add_row("7", "MCP Servers", "Manage...")

            CC.print(table)
            CC.print("\n[dim]ID to edit, 'b' to return[/dim]")

            try:
                choice = Prompt.ask("[bold purple]Config[/bold purple]")
                if choice.lower() in ['back', 'b', 'q', '/q', '/exit']:
                    break

                if choice == '1':
                    self._handle_provider_selection()
                elif choice == '2':
                    self.config.debug_mode = not self.config.debug_mode
                    self.config.save()
                elif choice == '3':
                    self.config.require_approval = not self.config.require_approval
                    self.config.save()
                elif choice == '4':
                    self.config.log_truncation = not self.config.log_truncation
                    self.config.save()
                elif choice == '5':
                    self._handle_model_selection()
                elif choice == '6':
                    self._handle_api_key_input()
                elif choice == '7':
                    self.mcp_settings_menu()
                elif choice == '8':
                    if self.config.provider == "openrouter":
                        CC.print("[dim]Enter full vision model ID (e.g. 'google/gemini-2.0-flash-exp:free')[/dim]")
                        self.config.openrouter.vision_model = Prompt.ask("Vision Model ID")
                    else:
                        self._handle_voice_selection()

                self.config.save()

            except (KeyboardInterrupt, EOFError):
                break

    def _handle_provider_selection(self):
        CC.print("\n[1] Pollinations\n[2] Gemini\n[3] OpenRouter\n[4] OpenAI\n[5] Anthropic")
        p_choice = IntPrompt.ask("Select", choices=["1", "2", "3", "4", "5"])

        if p_choice == 1:
            self.config.provider = "pollinations"
        elif p_choice == 2:
            self.config.provider = "gemini"
            if not self.config.gemini.api_key:
                self.config.gemini.api_key = Prompt.ask("Enter Gemini API Key")
        elif p_choice == 3:
            self.config.provider = "openrouter"
            if not self.config.openrouter.api_key:
                self.config.openrouter.api_key = Prompt.ask("Enter OpenRouter API Key")
        elif p_choice == 4:
            self.config.provider = "openai"
            if not self.config.openai.api_key:
                self.config.openai.api_key = Prompt.ask("Enter OpenAI API Key")
        elif p_choice == 5:
            self.config.provider = "anthropic"
            if not self.config.anthropic.api_key:
                self.config.anthropic.api_key = Prompt.ask("Enter Anthropic API Key")

        self.config.save()

    def _handle_model_selection(self):
        if self.config.provider == "gemini":
            self.select_gemini_model()
        elif self.config.provider == "openrouter":
            CC.print("[dim]Enter full model ID (e.g. 'anthropic/claude-3.5-sonnet')[/dim]")
            self.config.openrouter.model = Prompt.ask("Model ID")
            CC.print("[dim]Enter Vision Model ID (Leave empty to keep current)[/dim]")
            v_model = Prompt.ask("Vision Model ID", default=self.config.openrouter.vision_model)
            if v_model:
                self.config.openrouter.vision_model = v_model
        elif self.config.provider == "openai":
            CC.print("[dim]Enter OpenAI model (e.g. 'gpt-4o')[/dim]")
            self.config.openai.model = Prompt.ask("Model", default="gpt-4o")
        elif self.config.provider == "anthropic":
            CC.print("[dim]Enter Anthropic model (e.g. 'claude-3-5-sonnet-20240620')[/dim]")
            self.config.anthropic.model = Prompt.ask("Model", default="claude-3-5-sonnet-20240620")
        else:
            self.config.pollinations.text_model = Prompt.ask("Model", choices=["openai", "mistral", "searchgpt"], default="openai")

    def _handle_api_key_input(self):
        if self.config.provider == "gemini":
            self.config.gemini.api_key = Prompt.ask("API Key")
        elif self.config.provider == "openrouter":
            self.config.openrouter.api_key = Prompt.ask("API Key")
        elif self.config.provider == "openai":
            self.config.openai.api_key = Prompt.ask("API Key")
        elif self.config.provider == "anthropic":
            self.config.anthropic.api_key = Prompt.ask("API Key")
        else:
            self.config.pollinations.api_key = Prompt.ask("API Key")

    def _handle_voice_selection(self):
        self.config.pollinations.voice = Prompt.ask("Voice", choices=["alloy", "echo", "nova", "shimmer"], default="nova")

    def mcp_settings_menu(self):
        import time
        while True:
            console.clear()
            table = Table(title="MCP SERVER MANAGEMENT", border_style="purple")
            table.add_column("ID", style="dim", width=4)
            table.add_column("Server", style="bold green")
            table.add_column("Status", style="magenta")
            table.add_column("Command", style="dim")

            server_names = list(self.config.mcp_servers.keys())

            for idx, name in enumerate(server_names, 1):
                cfg = self.config.mcp_servers[name]
                status = "[green]ENABLED[/green]" if cfg.enabled else "[red]DISABLED[/red]"
                table.add_row(str(idx), name, status, cfg.command)

            CC.print(table)
            CC.print("\n[dim]Enter ID to toggle, 'b' to back[/dim]")

            choice = Prompt.ask("[bold purple]MCP Config[/bold purple]")
            if choice.lower() in ['b', 'back']:
                break

            try:
                idx = int(choice) - 1
                if 0 <= idx < len(server_names):
                    name = server_names[idx]
                    self.config.mcp_servers[name].enabled = not self.config.mcp_servers[name].enabled
                    self.config.save()
                    CC.print(f"[yellow]Toggled {name}. Restart required to apply.[/yellow]")
                    time.sleep(.5)
            except ValueError:
                pass

    async def chat_loop(self):
        if not self.orchestrator:
            self._init_system()

        self.print_banner()
        CC.print("\n[dim]Ready. Commands: /help. Ctrl+C to cancel generation.[/dim]\n")

        while True:
            try:
                try:
                    user_input = Prompt.ask("[bold purple]USER[/bold purple]")
                except KeyboardInterrupt:
                    CC.print("\n[yellow]Returning to Menu...[/yellow]")
                    return

                if not user_input.strip():
                    continue

                if user_input.lower() in ['exit', 'quit', 'q', '/q']:
                    sys.exit(0)
                if user_input.lower() in ['back', 'menu', 'b', '/back']:
                    return

                if user_input.startswith("/"):
                    if self.handle_system_command(user_input):
                        continue

                try:
                    task = asyncio.create_task(self.orchestrator.process(user_input))
                    response = await task

                    if response and response.strip():
                        CC.print(Panel(Markdown(response), border_style="purple"))

                except asyncio.CancelledError:
                    CC.print("\n[bold red]Generation Cancelled.[/bold red]")
                except KeyboardInterrupt:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    CC.print("\n[bold red]✋ Stopped.[/bold red]")
            except EOFError:
                return
            except Exception as e:
                CC.print(f"[bold red]System Error:[/bold red] {e}")

    async def run(self):
        while True:
            self.print_banner()
            CC.print("\n")
            try:
                choice = IntPrompt.ask(
                    "",
                    choices=["1", "2", "3"],
                    show_choices=False,
                    show_default=False
                )

                if choice == 1: await self.chat_loop()
                elif choice == 2: self.settings_menu()
                elif choice == 3: sys.exit(0)
            except (KeyboardInterrupt, EOFError):
                CC.print("\n[bold red]Shutdown.[/bold red]")
                sys.exit(0)

def main():
    cli = ZervGenCLI()
    try:
        asyncio.run(cli.run())
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)