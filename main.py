import sys
import asyncio
import os
import signal
from pathlib import Path
from rich.console import Console

sys.path.append(str(Path(__file__).parent))

from src.cli import main

console = Console()

def handle_exit(sig, frame):
    os._exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        os._exit(0)
    except Exception as e:
        if "RuntimeError" in str(e) or "CancelledError" in str(e):
            os._exit(0)
        console.print(f"\n[bold red]CRITICAL ERROR:[/bold red] {e}")
        sys.exit(1)