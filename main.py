import sys
import traceback
from pathlib import Path
from rich.console import Console

sys.path.append(str(Path(__file__).parent))

from src.cli import main

console = Console()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        console.print("\n[bold red]CRITICAL SYSTEM CRASH[/bold red]")
        console.print(traceback.format_exc())
        input("\nPress Enter to exit...")
        sys.exit(1)