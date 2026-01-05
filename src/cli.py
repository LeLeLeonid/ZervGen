import sys
import asyncio
import random
import datetime
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
        self._init_system()

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
[dim]v1.2.2 - Evolution[/dim]
        """
        console.print(Panel(banner, border_style="purple", expand=False))
        
        try:
            stats = memory_core.stats
            console.print(f"[dim]Memory: {stats['total_memories']} nodes | Provider: {self.config.provider.upper()}[/dim]")
        except:
            pass

    def handle_system_command(self, cmd: str) -> bool:
        valid_commands = ["/history", "/time", "/clear", "/memory", "/evolve", "/search"]
        
        if cmd == "/history":
            console.print(Panel(str(self.orchestrator.history), title="Session History"))
            return True
        elif cmd == "/time":
            console.print(f"[cyan]{datetime.datetime.now()}[/cyan]")
            return True
        elif cmd == "/clear":
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
        
        console.print(f"[red]Unknown command: {cmd}[/red]")
        console.print(f"[dim]Available: {', '.join(valid_commands)}[/dim]")
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
            
            if self.config.provider == "gemini":
                cfg = self.config.gemini
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("4", "Model", cfg.model)
                table.add_row("5", "API Key", key_display)
            elif self.config.provider == "openrouter":
                cfg = self.config.openrouter
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("4", "Model", cfg.model)
                table.add_row("5", "API Key", key_display)
            else:
                cfg = self.config.pollinations
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("4", "Model", cfg.text_model)
                table.add_row("5", "API Key", key_display)
                table.add_row("6", "Voice", cfg.voice)
            
            console.print(table)
            console.print("\n[dim]ID to edit, 'b' to return[/dim]")
            
            try:
                choice = Prompt.ask("[bold purple]Config[/bold purple]")
                if choice.lower() in ['b', 'q', 'exit']: break
                
                if choice == '1':
                    console.print("\n[1] Pollinations\n[2] Gemini\n[3] OpenRouter")
                    p_choice = IntPrompt.ask("Select", choices=["1", "2", "3"])
                    if p_choice == 1: self.config.provider = "pollinations"
                    elif p_choice == 2: 
                        self.config.provider = "gemini"
                        if not self.config.gemini.api_key: self.config.gemini.api_key = Prompt.ask("Enter Gemini API Key")
                    elif p_choice == 3: 
                        self.config.provider = "openrouter"
                        if not self.config.openrouter.api_key: self.config.openrouter.api_key = Prompt.ask("Enter OpenRouter API Key")
                    self.config.save()
                    self._init_system()
                    continue

                if choice == '2':
                    self.config.debug_mode = not self.config.debug_mode
                    self.config.save()
                    continue
                if choice == '3':
                    self.config.require_approval = not self.config.require_approval
                    self.config.save()
                    continue

                if self.config.provider == "gemini":
                    if choice == '4': self.select_gemini_model()
                    elif choice == '5': self.config.gemini.api_key = Prompt.ask("API Key")
                elif self.config.provider == "openrouter":
                    if choice == '4': self.config.openrouter.model = Prompt.ask("Model ID")
                    elif choice == '5': self.config.openrouter.api_key = Prompt.ask("API Key")
                else: 
                    if choice == '4': self.config.pollinations.text_model = Prompt.ask("Model")
                    elif choice == '5': self.config.pollinations.api_key = Prompt.ask("API Key")
                    elif choice == '6': self.config.pollinations.voice = Prompt.ask("Voice")
                
                self.config.save()
                self._init_system()
                
            except (KeyboardInterrupt, EOFError):
                break

    async def chat_loop(self):
        self.print_banner()
        console.print("[dim]Ready. Commands: /history, /time, /clear, /memory, /evolve, /search. Ctrl+C to exit.[/dim]\n")
        
        while True:
            try:
                user_input = Prompt.ask("[bold purple]USER[/bold purple]")
                if not user_input.strip(): continue
                if user_input.lower() in ['exit', 'quit']: sys.exit(0)
                
                if user_input.startswith("/"):
                    if self.handle_system_command(user_input): continue

                status_word = random.choice(LOADING_PHRASES)
                with console.status(f"[bold purple]{status_word}[/bold purple]", spinner="dots"):
                    response = await self.orchestrator.process(user_input)
                
                if response:
                    console.print(Panel(Markdown(response), border_style="purple"))
                
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Session Interrupted.[/yellow]")
                break
            except Exception as e:
                console.print(f"[bold red]Error:[/bold red] {e}")

    async def run(self):
        self.print_banner()
        while True:
            try:
                console.print("\n[1] Start Chat")
                console.print("[2] Configuration")
                console.print("[3] Exit")
                choice = IntPrompt.ask("Select", choices=["1", "2", "3"])
                if choice == 1: await self.chat_loop()
                elif choice == 2: 
                    self.settings_menu()
                    self.print_banner()
                elif choice == 3: sys.exit(0)
            except (KeyboardInterrupt, EOFError):
                console.print("\n[bold red]Shutdown.[/bold red]")
                sys.exit(0)

def main():
    cli = ZervGenCLI()
    try: asyncio.run(cli.run())
    except (KeyboardInterrupt, EOFError): sys.exit(0)