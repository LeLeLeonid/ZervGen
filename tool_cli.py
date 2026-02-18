import sys
import asyncio
import inspect
import json
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from src.tools import TOOL_REGISTRY

sys.path.append(str(Path(__file__).parent))

console = Console()

async def run_tool_session():
    while True:
        console.clear()
        console.print(Panel("[bold purple]ZERVGEN DEBUGGER (GOD MODE)[/bold purple]", expand=False))
        
        tools = list(TOOL_REGISTRY.keys())
        table = Table(show_header=True, header_style="bold magenta", box=None)
        table.add_column("ID", style="dim", width=4)
        table.add_column("Tool Name", style="bold green")
        
        # NEW OPTION
        table.add_row("0", "[bold yellow]SIMULATE BRAIN (You act as the AI)[/bold yellow]")
        table.add_row("", "") # Spacer

        for idx, name in enumerate(tools, 1):
            args = str(inspect.signature(TOOL_REGISTRY[name]))
            table.add_row(str(idx), f"{name}[dim]{args}[/dim]")

        console.print(table)
        console.print("\n[dim]Ctrl+C to Cancel | 'q' to Exit[/dim]")

        try:
            choice = Prompt.ask("[bold purple]Select Option[/bold purple]")
            
            if choice.lower() in ['q', 'exit']:
                break

            # Handle SIMULATE BRAIN mode (option 0)
            if choice.strip() == "0":
                console.print("\n[bold yellow]SIMULATE BRAIN MODE[/bold yellow]")
                console.print("[dim]Enter JSON with 'tool' and 'args' keys[/dim]")
                json_input = Prompt.ask("[bold cyan]JSON Input[/bold cyan]")

                try:
                    data = json.loads(json_input)
                    tool_name = data.get("tool")
                    args = data.get("args", {})

                    if not tool_name:
                        console.print("[bold red]Error: 'tool' key missing in JSON[/bold red]")
                        await asyncio.sleep(2)
                        continue

                    if tool_name not in TOOL_REGISTRY:
                        console.print(f"[bold red]Error:[/bold red] Tool '{tool_name}' not found.")
                        await asyncio.sleep(2)
                        continue

                    func = TOOL_REGISTRY[tool_name]

                    with console.status(f"[bold green]Executing {tool_name}...[/bold green]"):
                        if inspect.iscoroutinefunction(func):
                            result = await func(**args)
                        else:
                            result = func(**args)

                    console.print(Panel(str(result), title=f"[bold green]OBSERVATION (System Output)[/bold green]"))
                    Prompt.ask("\n[dim]Press Enter to return...[/dim]")
                    continue

                except json.JSONDecodeError:
                    console.print("[bold red]Invalid JSON. Simulation Failed.[/bold red]")
                    await asyncio.sleep(2)
                    continue
                except Exception as e:
                    console.print(f"[bold red]Simulation Error:[/bold red] {e}")
                    Prompt.ask("Press Enter...")
                    continue

            # --- STANDARD TOOL TESTER ---
            idx = int(choice) - 1
            if 0 <= idx < len(tools):
                tool_name = tools[idx]
                func = TOOL_REGISTRY[tool_name]
                sig = inspect.signature(func)
                
                kwargs = {}
                console.print(f"\n[bold]Configuring: {tool_name}[/bold]")
                
                for param_name in sig.parameters:
                    # Skip kwargs/args
                    if param_name in ['kwargs', 'args']: continue
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
                await asyncio.sleep(0.5)

        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            await asyncio.sleep(0.5)
            continue
        except ValueError:
            pass
        except Exception as e:
            console.print(f"[bold red]System Error:[/bold red] {e}")
            Prompt.ask("Press Enter...")

if __name__ == "__main__":
    try:
        asyncio.run(run_tool_session())
    except KeyboardInterrupt:
        sys.exit(0)