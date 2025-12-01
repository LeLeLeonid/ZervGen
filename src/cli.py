import sys
import asyncio
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.table import Table

from src.config import load_config
from src.providers.pollinations import PollinationsProvider
from src.core.orchestrator import Orchestrator

console = Console()

class ZervGenCLI:
    def __init__(self):
        self.config = load_config()
        self._init_system()

    def _init_system(self):
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
        """
        console.print(Panel(banner, border_style="purple", expand=False))

    def settings_menu(self):
        while True:
            console.clear()
            table = Table(title="SYSTEM CONFIGURATION", border_style="purple")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Parameter", style="magenta")
            table.add_column("Current Value", style="green")
            
            p_cfg = self.config.pollinations
            table.add_row("1", "Provider", self.config.provider)
            table.add_row("2", "Text Model", p_cfg.text_model)
            table.add_row("3", "Image Model", p_cfg.image_model)
            table.add_row("4", "Voice", p_cfg.voice)
            table.add_row("5", "Reasoning", str(p_cfg.reasoning_effort))
            
            console.print(table)
            console.print("\n[dim]Enter ID to edit or 'b' to return.[/dim]")
            
            choice = Prompt.ask("[bold purple]Config[/bold purple]")
            
            if choice == 'b': break
            
            if choice == '2':
                p_cfg.text_model = Prompt.ask("Model", choices=["openai", "mistral", "searchgpt"], default=p_cfg.text_model)
            elif choice == '3':
                p_cfg.image_model = Prompt.ask("Image Model", choices=["flux", "turbo"], default=p_cfg.image_model)
            elif choice == '4':
                p_cfg.voice = Prompt.ask("Voice", choices=["alloy", "echo", "nova", "shimmer"], default=p_cfg.voice)
            elif choice == '5':
                p_cfg.reasoning_effort = Prompt.ask("Reasoning (High Tier Only)", choices=["minimal", "low", "medium", "high"], default="minimal")
            
            self.config.save()
            self._init_system()

    async def chat_loop(self):
        self.print_banner()
        console.print("[dim]Commands: /image <prompt>, /speak <text>, /exit[/dim]\n")
        
        while True:
            try:
                user_input = Prompt.ask("[bold purple]USER[/bold purple]")
                
                if user_input.lower() in ['exit', 'quit']:
                    break
                if not user_input.strip(): continue

                with console.status("[bold purple]Thinking...[/bold purple]", spinner="dots"):
                    response = await self.orchestrator.process(user_input)
                
                console.print(Panel(Markdown(response), border_style="purple"))
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                console.print(f"[bold red]System Error:[/bold red] {e}")

    async def run(self):
        self.print_banner()
        while True:
            console.print("\n[1] Initialize Orchestrator")
            console.print("[2] System Configuration")
            console.print("[3] Terminate Session")
            
            choice = Prompt.ask("Select", choices=["1", "2", "3"])
            
            if choice == "1":
                await self.chat_loop()
            elif choice == "2":
                self.settings_menu()
                self.print_banner()
            elif choice == "3":
                sys.exit(0)

def main():
    cli = ZervGenCLI()
    asyncio.run(cli.run())