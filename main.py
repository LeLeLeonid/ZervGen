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
    """Interrupt whatever is blocking, so an open prompt cannot swallow Ctrl-C."""
    raise KeyboardInterrupt


async def main():
    loop = asyncio.get_running_loop()
    def _ignore_shutdown_exceptions(loop, context):
        """Swallow the teardown noise that fires while transports are being closed."""
        exc = context.get("exception")
        message = context.get("message", "")
        if isinstance(exc, (RuntimeError, OSError)) and "Event loop is closed" in (str(exc) or message):
            return
        if "Event loop is closed" in message:
            return
        loop.default_exception_handler(context)
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
        try:
            asyncio.run(graceful_shutdown(signal.SIGINT))
        except Exception:
            os._exit(0)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {str(e).replace('[', '\\[')}")
        sys.exit(1)


if __name__ == "__main__":
    run_app()