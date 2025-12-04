import sys
import asyncio
import random
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt, IntPrompt
from rich.table import Table

from src.config import load_config
from src.providers.pollinations import PollinationsProvider
from src.providers.gemini import GeminiProvider, GEMINI_MODELS
from src.core.orchestrator import Orchestrator

console = Console()

LOADING_PHRASES = [
    "Thinking...",
    "Orchestrating...",
    "Aligning parameters...",
    "Synthesizing...",
    "Analyzing intent...",
    "Processing..."
]

class ZervGenCLI:
    def __init__(self):
        self.config = load_config()
        self._init_system()

    def _init_system(self):
        if self.config.provider == "gemini":
            try:
                provider = GeminiProvider(self.config.gemini)
            except Exception:
                self.config.provider = "pollinations"
                provider = PollinationsProvider(self.config.pollinations)
        else:
            provider = PollinationsProvider(self.config.pollinations)
            
        self.orchestrator = Orchestrator(provider, self.config)

    def print_banner(self):
        console.clear()
        banner = """
[bold purple]███████╗███████╗██████╗ ██╗   ██╗ ██████╗ ███████╗███╗   ██╗[/bold purple]
[bold purple]╚══███╔╝██╔════╝██╔══██╗██║   ██║██╔════╝ ██╔════╝████╗  ██║[/bold purple]
[bold blue]  ███╔╝ █████╗  ██████╔╝██║   ██║██║  ███╗█████╗  ██╔██╗ ██║[/bold blue]
[bold blue] ███╔╝  ██╔══╝  ██╔══██╗╚██╗ ██╔╝██║   ██║██╔══╝  ██║╚██╗██║[/bold blue]
[bold purple]███████╗███████╗██║  ██║ ╚████╔╝ ╚██████╔╝███████╗██║ ╚████║[/bold purple]
[bold purple]╚══════╝╚══════╝╚═╝  ╚═╝  ╚═══╝   ╚═════╝ ╚══════╝╚═╝  ╚═══╝[/bold purple]
[dim]SYSTEM ONLINE[/dim]
        """
        console.print(Panel(banner, border_style="purple", expand=False))

    def select_gemini_model(self):
        console.clear()
        console.print("[bold purple]Available Models:[/bold purple]\n")
        for idx, model in enumerate(GEMINI_MODELS, 1):
            console.print(f"[cyan]{idx}.[/cyan] {model}")
        
        console.print("\n[dim]Enter number[/dim]")
        try:
            choice = IntPrompt.ask("Select", choices=[str(i) for i in range(1, len(GEMINI_MODELS)+1)])
            self.config.gemini.model = GEMINI_MODELS[choice - 1]
        except Exception: pass

    def settings_menu(self):
        while True:
            console.clear()
            table = Table(title="CONFIGURATION", border_style="purple")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Parameter", style="magenta")
            table.add_column("Value", style="green")
            
            # Dynamic Rows based on Provider
            table.add_row("1", "Provider", self.config.provider.upper())
            
            if self.config.provider == "gemini":
                # GEMINI CONTEXT
                cfg = self.config.gemini
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("2", "Model", cfg.model)
                table.add_row("3", "API Key", key_display)
            else:
                # POLLINATIONS CONTEXT
                cfg = self.config.pollinations
                key_display = "********" if cfg.api_key else "NOT SET"
                table.add_row("2", "Model", cfg.text_model)
                table.add_row("3", "API Key", key_display)
                table.add_row("4", "Voice", cfg.voice)
            
            console.print(table)
            console.print("\n[dim]ID to edit, 'b'/'q' to return[/dim]")
            
            try:
                choice = Prompt.ask("[bold purple]Config[/bold purple]")
                if choice.lower() in ['b', 'q', 'exit', 'quit']: break
                
                if choice == '1':
                    self.config.provider = Prompt.ask("Provider", choices=["pollinations", "gemini"], default=self.config.provider)
                
                elif self.config.provider == "gemini":
                    if choice == '2': self.select_gemini_model()
                    elif choice == '3': self.config.gemini.api_key = Prompt.ask("API Key")
                
                else: # Pollinations
                    if choice == '2': 
                        self.config.pollinations.text_model = Prompt.ask("Model", choices=["openai", "mistral", "searchgpt"], default="openai")
                    elif choice == '3':
                        self.config.pollinations.api_key = Prompt.ask("API Key")
                    elif choice == '4':
                        self.config.pollinations.voice = Prompt.ask("Voice", choices=["alloy", "echo", "nova", "shimmer"], default="nova")
                
                self.config.save()
                self._init_system()
            except KeyboardInterrupt: break

    async def chat_loop(self):
        self.print_banner()
        console.print("[dim]Ready. Type 'exit' to quit.[/dim]\n")
        
        while True:
            try:
                user_input = Prompt.ask("[bold purple]USER[/bold purple]")
                if user_input.lower() in ['exit', 'quit', 'q']: sys.exit(0)
                if not user_input.strip(): continue

                # Dynamic Loading Word
                status_word = random.choice(LOADING_PHRASES)
                
                with console.status(f"[bold purple]{status_word}[/bold purple]", spinner="dots"):
                    response = await self.orchestrator.process(user_input)
                
                console.print(Panel(Markdown(response), border_style="purple"))
                
            except KeyboardInterrupt:
                sys.exit(0)
            except Exception as e:
                console.print(f"[bold red]Error:[/bold red] {e}")

    async def run(self):
        self.print_banner()
        while True:
            try:
                console.print("\n[1] Start")
                console.print("[2] Config")
                console.print("[3] Exit")
                
                choice = Prompt.ask("Select", choices=["1", "2", "3"])
                
                if choice == "1": await self.chat_loop()
                elif choice == "2": 
                    self.settings_menu()
                    self.print_banner()
                elif choice == "3": sys.exit(0)
            except KeyboardInterrupt: sys.exit(0)

def main():
    cli = ZervGenCLI()
    try: asyncio.run(cli.run())
    except KeyboardInterrupt: sys.exit(0)