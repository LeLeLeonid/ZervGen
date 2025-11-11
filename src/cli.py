from rich.console import Console
from rich.panel import Panel
from .config import settings
from .tools.pollinations_client import PollinationsClient, ApiClientError
from .agents.supervisor import Supervisor
from .agents.executors.tool_executor import ToolExecutor

console = Console()

def run():
    
    """Initializes and starts the main CLI loop for the Supervisor."""
    console.print(Panel("[bold green]ZervGen Supervisor Initialized[/bold green]", 
                      title="Welcome", subtitle="Enter a high-level goal."))
    console.print("[yellow]Hint: The agent is stateless (has no memory). To refine a result, restate your full goal.[]")
    console.print("[]Example: 'Create the same cyberpunk city image, but make it 1920x1080'[]\n")
    
    try:
        client = PollinationsClient(api_token=settings.pollinations_api_token)
        supervisor = Supervisor(client=client)
        executor = ToolExecutor(client=client)
    except Exception as e:
        console.print(f"[bold red]Initialization Error:[/bold red] {e}")
        return

    while True:
        try:
            user_goal = console.input("[bold magenta]Goal > [/bold magenta]").strip()
            if not user_goal: continue
            if user_goal.lower() == 'exit': break
            with console.status("[yellow]Supervisor is creating a plan...[/yellow]", spinner="dots"):
                plan = supervisor.create_plan(user_goal)
            
            console.print("[bold blue]Execution Plan:[/bold blue]")
            console.print(plan)
            step_num = 1
            for step in plan:
                with console.status(f"[yellow]Executor running step {step_num}/{len(plan)}: {step.get('tool_name')}...[/yellow]"):
                    result = executor.execute_step(step)
                
                console.print(f"[bold green]Step {step_num} Result:[/bold green]")
                console.print(Panel(result, border_style="green"))
                step_num += 1

        except (ValueError, ApiClientError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
        except KeyboardInterrupt:
            break
            
    console.print("\n[bold yellow]Shutting down.[/bold yellow]")