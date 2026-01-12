import sys
import asyncio
import random
import datetime
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt, IntPrompt
from rich.table import Table

from src.config import load_config
from src.providers.pollinations import PollinationsProvider
from src.providers.gemini import GeminiProvider, fetch_available_models
from src.providers.openrouter import OpenRouterProvider
from src.core.orchestrator import Orchestrator
from src.core.memory import memory_core

console = Console()

LOADING_PHRASES = [
    "Thinking...", 
    "Orchestrating...", 
    "Aligning parameters...",
    "Synthesizing...", 
    "Analyzing intent...", 
    "Processing...",
    "Consulting the Hive...",
    "Optimizing neural pathways...",
    "Querying external nodes...",
    "Constructing response matrix...",
    "Synchronizing agents...",
    "Accessing core mainframe...",
    "Decrypting user intent...",
    "Routing data packets..."
]

class ZervGenCLI:
    def __init__(self):
        self.config = load_config()
        self.orchestrator = None # Lazy init to speed up startup

    def _init_system(self):
        try:
            if self.config.provider == "gemini":
                provider = GeminiProvider(self.config.gemini)
            elif self.config.provider == "openrouter":
                provider = OpenRouterProvider(self.config.openrouter)
            else:
                provider = PollinationsProvider(self.config.pollinations)
            
            self.orchestrator = Orchestrator(provider, self.config)

        except Exception as e:
            console.print(f"\n[bold red]SYSTEM INITIALIZATION ERROR: {e}[/bold red]")
            console.print("[yellow]Falling back to Pollinations (Safe Mode)...[/yellow]")
            
            self.config.provider = "pollinations"
            provider = PollinationsProvider(self.config.pollinations)
            self.orchestrator = Orchestrator(provider, self.config)
            
            Prompt.ask("\n[bold white on red] Press Enter to acknowledge [/bold white on red]")

    def print_banner(self):
        console.clear()
        banner = """
[bold purple]███████╗███████╗██████╗ ██╗   ██╗ ██████╗ ███████╗███╗   ██╗[/bold purple]
[bold purple]╚══███╔╝██╔════╝██╔══██╗██║   ██║██╔════╝ ██╔════╝████╗  ██║[/bold purple]
[bold blue]  ███╔╝ █████╗  ██████╔╝██║   ██║██║  ███╗█████╗  ██╔██╗ ██║[/bold blue]
[bold blue] ███╔╝  ██╔══╝  ██╔══██╗╚██╗ ██╔╝██║   ██║██╔══╝  ██║╚██╗██║[/bold blue]
[bold purple]███████╗███████╗██║  ██║ ╚████╔╝ ╚██████╔╝███████╗██║ ╚████║[/bold purple]
[bold purple]╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚══════╝╚═╝  ╚═══╝[/bold purple]
[dim]v1.3.1 - Neural Graph Core[/dim]
        """
        console.print(Panel(banner, border_style="purple", expand=False))
        
        try:
            stats = memory_core.stats
            mem_info = f"{stats['total_memories']} nodes"
            prov_info = self.config.provider.upper()
            if self.config.provider == "gemini": prov_info += f" ({self.config.gemini.model})"
            elif self.config.provider == "openrouter": prov_info += f" ({self.config.openrouter.model})"
            
            console.print(f"[dim]Memory: {mem_info} | Provider: {prov_info}[/dim]")
        except:
            pass

    def handle_system_command(self, cmd: str) -> bool:
        valid_commands = ["/history", "/time", "/clear", "/memory", "/evolve", "/search", "/help"]
        
        if cmd == "/help":
            help_text = """
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
            """
            console.print(Panel(help_text, title="Help", border_style="blue"))
            return True
            
        elif cmd == "/history":
            if self.orchestrator and self.orchestrator.history:
                console.print(Panel(str(self.orchestrator.history[-3:]), title="Recent History (Last 3)"))
            else:
                console.print("[dim]No history yet.[/dim]")
            return True
            
        elif cmd == "/time":
            console.print(f"[cyan]{datetime.datetime.now()}[/cyan]")
            return True
            
        elif cmd == "/clear":
            if self.orchestrator:
                self.orchestrator.history = []
            console.print("[yellow]Session History Cleared.[/yellow]")
            return True
            
        elif cmd == "/memory":
            stats = memory_core.get_stats()
            console.print(Panel(stats, title="Memory System Stats", border_style="green"))
            return True
            
        elif cmd == "/evolve":
            with console.status("[bold purple]Triggering self-evolution...[/bold purple]"):
                result = memory_core.evolve()
            console.print(Panel(result, title="Evolution Result", border_style="purple"))
            return True
            
        elif cmd == "/search":
            query = Prompt.ask("Search query")
            with console.status("[bold purple]Searching memory...[/bold purple]"):
                result = memory_core.search_memory(query, mode="semantic")
            console.print(Panel(result, title="Search Results", border_style="cyan"))
            return True
        
        elif cmd == "/mode":
            parts = cmd.split()
            if len(parts) > 1:
                mode = parts[1]
                if self.orchestrator.set_mode(mode):
                    console.print(f"[green]Switched to {mode.upper()} mode.[/green]")
                else:
                    console.print(f"[red]Mode '{mode}' not found.[/red]")
            else:
                console.print(f"[dim]Current mode: {self.orchestrator.current_mode}[/dim]")
            return True

        elif cmd == "/load":
            import os
            sessions_dir = Path("tmp/memory/sessions")
            if not sessions_dir.exists():
                console.print("[red]No sessions found.[/red]")
                return True
 
            files = sorted([f for f in os.listdir(sessions_dir) if f.endswith(".jsonl")], reverse=True)
            if not files:
                console.print("[dim]No session logs found.[/dim]")
                return True
                
            console.print("\n[bold purple]AVAILABLE SESSIONS:[/bold purple]")
            for i, f in enumerate(files[:10]): 
                console.print(f"[{i+1}] {f}")
            
            try:
                choice = IntPrompt.ask("Load Session #", choices=[str(i+1) for i in range(len(files[:10]))])
                selected_file = files[choice-1]
                loaded_hist = memory_core.load_session_from_file(selected_file)
                self.orchestrator.history = loaded_hist
                console.print(f"[green]Session '{selected_file}' loaded ({len(loaded_hist)} msgs).[/green]")
            except Exception as e:
                console.print(f"[red]Load error: {e}[/red]")
            return True

        # Unknown command handler
        console.print(f"[red]Unknown command: {cmd}[/red]")
        console.print(f"[dim]Type /help for list of commands.[/dim]")
        return True

    def select_gemini_model(self):
        if not self.config.gemini.api_key:
            console.print("[red]API Key required to fetch models.[/red]")
            Prompt.ask("Press Enter...")
            return

        try:
            with console.status("[bold purple]Fetching available models from Google...[/bold purple]"):
                models = fetch_available_models(self.config.gemini.api_key)
            
            console.clear()
            console.print("[bold purple]Available Google Models:[/bold purple]\n")
            for idx, model in enumerate(models, 1):
                console.print(f"[cyan]{idx}.[/cyan] {model}")
            
            console.print("\n[dim]Enter number[/dim]")
            choice = IntPrompt.ask("Select", choices=[str(i) for i in range(1, len(models)+1)])
            self.config.gemini.model = models[choice - 1]
        except Exception as e:
            console.print(f"[red]Error fetching models: {e}[/red]")
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

            else:
                cfg = self.config.pollinations
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("5", "Model", cfg.text_model)
                table.add_row("6", "API Key", key_display)
                table.add_row("7", "Voice", cfg.voice)
            
            console.print(table)
            console.print("\n[dim]ID to edit, 'b' to return[/dim]")
            
            try:
                choice = Prompt.ask("[bold purple]Config[/bold purple]")
                if choice.lower() in ['back', 'b', 'q', '/q', '/exit']: break
                
                if choice == '1':
                    console.print("\n[1] Pollinations\n[2] Gemini\n[3] OpenRouter")
                    p_choice = IntPrompt.ask("Select", choices=["1", "2", "3"])
                    
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
                    
                    self.config.save()
                    # Re-init not needed here, will happen on chat entry
                    continue

                if choice == '2':
                    self.config.debug_mode = not self.config.debug_mode
                    self.config.save()
                    continue
                
                if choice == '3':
                    self.config.require_approval = not self.config.require_approval
                    self.config.save()
                    continue

                if choice == '4':
                    self.config.log_truncation = not self.config.log_truncation
                    self.config.save()
                    continue

                if self.config.provider == "gemini":
                    if choice == '5': self.select_gemini_model()
                    elif choice == '6': self.config.gemini.api_key = Prompt.ask("API Key")
                
                elif self.config.provider == "openrouter":
                    if choice == '5':
                        console.print("[dim]Enter full model ID (e.g. 'anthropic/claude-3.5-sonnet')[/dim]")
                        self.config.openrouter.model = Prompt.ask("Model ID")
                    elif choice == '6':
                        self.config.openrouter.api_key = Prompt.ask("API Key")

                else: 
                    if choice == '5': 
                        self.config.pollinations.text_model = Prompt.ask("Model", choices=["openai", "mistral", "searchgpt"], default="openai")
                    elif choice == '6':
                        self.config.pollinations.api_key = Prompt.ask("API Key")
                    elif choice == '7':
                        self.config.pollinations.voice = Prompt.ask("Voice", choices=["alloy", "echo", "nova", "shimmer"], default="nova")
                
                self.config.save()
                
            except (KeyboardInterrupt, EOFError):
                break

    async def chat_loop(self):
        # Lazy init system to ensure config is fresh
        if not self.orchestrator:
            self._init_system()
        
        self.print_banner()
        console.print("[dim]Ready. Commands: /help. Ctrl+C to cancel generation.[/dim]\n")
        
        while True:
            try:
                try:
                    user_input = Prompt.ask("[bold purple]USER[/bold purple]")
                except KeyboardInterrupt:
                    console.print("\n[yellow]Returning to Menu...[/yellow]")
                    return

                if not user_input.strip(): continue

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
                        console.print(Panel(Markdown(response), border_style="purple"))
                
                except asyncio.CancelledError:
                    console.print("\n[bold red]Generation Cancelled.[/bold red]")
                except KeyboardInterrupt:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    console.print("\n[bold red]✋ Stopped.[/bold red]")
            except EOFError:
                return
            except Exception as e:
                console.print(f"[bold red]System Error:[/bold red] {e}")

    async def run(self):
        while True:
            self.print_banner()
            console.print("\n[1] Start Chat")
            console.print("[2] Configuration")
            console.print("[3] Exit")
            
            try:
                choice = IntPrompt.ask("Select", choices=["1", "2", "3"])
                
                if choice == 1: await self.chat_loop()
                elif choice == 2: self.settings_menu()
                elif choice == 3: sys.exit(0)
            except (KeyboardInterrupt, EOFError):
                console.print("\n[bold red]Shutdown.[/bold red]")
                sys.exit(0)

def main():
    cli = ZervGenCLI()
    try:
        asyncio.run(cli.run())
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)