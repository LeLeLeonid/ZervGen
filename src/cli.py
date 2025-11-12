from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from .config import settings
from .tools.pollinations_client import PollinationsClient, ApiClientError
from .tools.pattern_manager import PatternManager
from .agents.supervisor import Supervisor
from .agents.executors.tool_executor import ToolExecutor


console = Console()

def print_help(pattern_manager: PatternManager):
    """Displays the help message with correct formatting."""
    console.print("\n[bold green]--- ZervGen Help ---[/bold green]")
    
    console.print("  - [cyan]create pattern: {description}[/cyan]: Create a new pattern.")
    console.print("  - [cyan]use {pattern_name}: {text}[/cyan]: Use an existing pattern.")
    console.print("  - [cyan]list[/cyan]: List all available patterns.")
    console.print("  - [cyan]exit[/cyan]: Quit the application.\n")

def run():
    """Initializes and starts the main CLI loop."""
    console.print(Panel("[bold green]ZervGen Framework Initialized[/bold green]", title="Welcome"))
    
    try:
        client = PollinationsClient(api_token=settings.pollinations_api_token)
        supervisor = Supervisor(client=client)
        executor = ToolExecutor(client=client)
        pattern_manager = PatternManager()
    except Exception as e:
        console.print(f"[bold red]Initialization Error:[/bold red] {e}")
        return

    meta_pattern_prompt = pattern_manager.get_pattern_prompt("pattern_engine")
    if not meta_pattern_prompt:
        console.print("[bold red]Fatal Error: Meta-pattern 'pattern_engine.md' not found![/bold red]")
        return

    console.print("Meta-pattern '[bold cyan]pattern_engine[/bold cyan]' is loaded.")
    available_patterns = pattern_manager.list_patterns()
    console.print(f"[bold]Available Patterns:[/bold] {', '.join(available_patterns) if available_patterns else 'None'}\n")

    while True:
        try:
            user_input = console.input("[bold magenta]ZervGen > [/bold magenta]").strip()
            if not user_input: continue
            
            command = user_input.lower()
            if command == 'exit': break
            if command == 'help':
                print_help(pattern_manager)
                continue
            if command == 'list':
                patterns = pattern_manager.list_patterns()
                console.print(f"[bold]Available Patterns:[/bold] {', '.join(patterns) if patterns else 'None'}")
                continue

            plan = []
            if command.startswith("create pattern:"):
                user_goal = user_input.split(":", 1)[1].strip()
                if not user_goal:
                    console.print("[bold red]Error: Goal for the new pattern is required.[/bold red]")
                    continue
                with console.status("[yellow]Supervisor is creating a plan to forge a new pattern...[/yellow]"):
                    plan = supervisor.create_pattern_creation_plan(meta_pattern_prompt, user_goal)
            
            elif command.startswith("use"):
                try:
                    args_string = user_input[3:].lstrip().lstrip(':').strip()
                    parts = args_string.split(':', 1)
                    if len(parts) != 2: raise ValueError("Missing ':' to separate pattern name from goal.")
                    pattern_name, user_goal = parts[0].strip(), parts[1].strip()
                    if not pattern_name or not user_goal: raise ValueError("Both pattern name and goal must not be empty.")
                except ValueError as e:
                    console.print(f"[bold red]Invalid format: {e}[/bold red]")
                    console.print("Correct format: `use [pattern_name]: [your goal]`")
                    continue

                pattern_prompt = pattern_manager.get_pattern_prompt(pattern_name)
                if not pattern_prompt:
                    console.print(f"[bold red]Error: Pattern '{pattern_name}' not found.[/bold red]")
                    continue

                plan = supervisor.create_plan_from_pattern(pattern_prompt, user_goal)
            else:
                print_help(pattern_manager)
                continue
            
            console.print("\n[bold blue]Execution Plan:[/bold blue]")
            console.print(plan)

            if not Confirm.ask("[bold green]Approve this plan for execution?[/bold green]"):
                console.print("[yellow]Plan rejected.[/yellow]")
                continue

            step_num = 1
            for step in plan:
                with console.status(f"[yellow]Executor running step {step_num}/{len(plan)}...[/yellow]"):
                    result = executor.execute_step(step)
                console.print(f"[bold green]Step {step_num} Result:[/bold green]")
                console.print(Panel(result, border_style="green"))
                step_num += 1

        except (ValueError, ApiClientError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
        except KeyboardInterrupt:
            break
            
    console.print("\n[bold yellow]Shutting down.[/bold yellow]")