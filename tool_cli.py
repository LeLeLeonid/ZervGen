import sys
import asyncio
import inspect
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt

sys.path.append(str(Path(__file__).parent))

from src.tools import TOOL_REGISTRY

console = Console()

async def run_tool_session():
    while True:
        console.clear()
        console.print(Panel("[bold cyan]MANUAL TOOL DEBUGGER[/bold cyan]", expand=False))
        
        tools = list(TOOL_REGISTRY.keys())
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("ID", style="dim", width=4)
        table.add_column("Tool Name", style="bold green")
        table.add_column("Arguments", style="cyan")

        for idx, name in enumerate(tools, 1):
            func = TOOL_REGISTRY[name]
            sig = inspect.signature(func)
            args = ", ".join([p.name for p in sig.parameters.values()])
            table.add_row(str(idx), name, args)

        console.print(table)
        console.print("\n[dim]Type 'q' to exit[/dim]")

        choice = Prompt.ask("[bold yellow]Select Tool ID[/bold yellow]")
        
        if choice.lower() in ['q', 'exit']:
            break

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(tools):
                tool_name = tools[idx]
                func = TOOL_REGISTRY[tool_name]
                sig = inspect.signature(func)
                
                kwargs = {}
                console.print(f"\n[bold]Configuring: {tool_name}[/bold]")
                
                for param_name in sig.parameters:
                    val = Prompt.ask(f"Enter value for [cyan]{param_name}[/cyan]")
                    kwargs[param_name] = val

                with console.status("[bold green]Executing...[/bold green]"):
                    if inspect.iscoroutinefunction(func):
                        result = await func(**kwargs)
                    else:
                        result = func(**kwargs)

                console.print(Panel(str(result), title=f"[bold green]Result: {tool_name}[/bold green]"))
                Prompt.ask("\n[dim]Press Enter to continue...[/dim]")
            else:
                console.print("[red]Invalid ID[/red]")
                await asyncio.sleep(1)
        except ValueError:
            pass
        except Exception as e:
            console.print(f"[bold red]Execution Error:[/bold red] {e}")
            Prompt.ask("Press Enter...")

if __name__ == "__main__":
    try:
        asyncio.run(run_tool_session())
    except KeyboardInterrupt:
        sys.exit(0)