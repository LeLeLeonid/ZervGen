import sys
import asyncio
import os
import signal
import logging
from pathlib import Path
from rich.console import Console

console = Console()


async def graceful_shutdown(sig):
    console.print("\n[dim]Shutting down gracefully...[/dim]")
    try:
        from src.core.memory import memory_core
        if memory_core:
            memory_core._kg.save()
        from src.core.provider import close_http_client
        await close_http_client()
    except Exception as e:
        console.print(f"[dim]Warning: Could not save session: {e}[/dim]")
    sys.exit(0)


def handle_exit(sig, frame):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(graceful_shutdown(sig))
        else:
            os._exit(0)
    except RuntimeError:
        os._exit(0)


async def main():
    loop = asyncio.get_running_loop()
    def _ignore_shutdown_exceptions(l, context):
        if sys.meta_path is None:
            return
        l.default_exception_handler(context)
    loop.set_exception_handler(_ignore_shutdown_exceptions)
    from src.cli import ZervGenCLI
    from src.utils import setup_logging
    setup_logging(level=logging.INFO)
    cli = ZervGenCLI()
    try:
        await cli.run()
    finally:
        await cli._cleanup()


def run_app():
    is_windows = sys.platform == 'win32'
    
    if is_windows:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    else:
        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if is_windows:
            try:
                asyncio.run(graceful_shutdown(signal.SIGINT))
            except Exception:
                os._exit(0)
        else:
            os._exit(0)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {str(e).replace('[', '\\[')}")
        sys.exit(1)


if __name__ == "__main__":
    run_app()