from rich.console import Console
from rich.panel import Panel
from pathlib import Path
from .config import settings
from .tools.pollinations_client import PollinationsClient
from .core.toolbox import Toolbox
from .core.orchestrator import Orchestrator

console = Console()

def run():
    console.print(Panel("[bold green]ZervGen Initialized[/bold green]"))
    try:
        project_root = Path(__file__).parent.parent
        client = PollinationsClient(api_token=settings.pollinations_api_token)
        toolbox = Toolbox(client=client, project_root=project_root)
        orchestrator = Orchestrator(toolbox=toolbox, llm_client=client)
        console.print("Enter your goal, or 'list'/'help'/'exit'.\n")
    except Exception as e:
        console.print(f"[bold red]Fatal Error:[/bold red] {e}")
        return

    while True:
        try:
            user_input = console.input("[bold magenta]Goal > [/bold magenta]").strip()
            if not user_input: continue
            command = user_input.lower()
            if command in ('exit','quit','q'):
                break
            elif command in ('help','sos'):
                console.print("Enter a high-level goal for the Orchestrator to execute.")
                console.print("Example: 'Create a FullHD, high-quality image of a cyberpunk cat.'")
                continue
            elif command in ('list','ls'):
                console.print("[bold]Available Tools:[/bold]")
                console.print(toolbox.get_manifest_for_prompt())
                continue

            orchestrator.execute_goal(user_input)

        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
        except KeyboardInterrupt:
            break
            
    console.print("\n[bold yellow]Shutting down.[/bold yellow]")