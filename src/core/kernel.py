from pathlib import Path
from ..tools.pollinations_client import PollinationsClient

class Kernel:
    """The core "headless" logic engine of ZervGen, using a dispatcher pattern."""
    def __init__(self, client: PollinationsClient):
        self.client = client
        self.output_dir = Path("outputs")
        self.output_dir.mkdir(exist_ok=True)
        
        # Dispatcher maps tool names to the methods that handle them.
        self._dispatcher = {
            "text": self._execute_text_generation,
            "image": self._execute_image_generation,
        }
        self.available_tools = list(self._dispatcher.keys())

    def process_command(self, command: str) -> str:
        """Parses a command, finds the right tool via the dispatcher, and executes it."""
        try:
            tool_name, prompt = command.split(":", 1)
            tool_name = tool_name.strip().lower()
            prompt = prompt.strip()
        except ValueError:
            raise ValueError("Invalid command format. Use 'tool: prompt'.")

        if tool_name not in self._dispatcher:
            raise ValueError(f"Unknown tool: '{tool_name}'. Available: {', '.join(self.available_tools)}.")

        # Call the appropriate method from the dispatcher
        handler_method = self._dispatcher[tool_name]
        return handler_method(prompt)

    def _execute_text_generation(self, prompt: str) -> str:
        return self.client.generate_text(prompt)

    def _execute_image_generation(self, prompt: str) -> str:
        image_bytes = self.client.generate_image(prompt)
        safe_filename = "".join(c for c in prompt if c.isalnum() or c in " _-").rstrip()[:50]
        output_path = self.output_dir / f"{safe_filename or 'image'}.jpg"
        
        with open(output_path, "wb") as f:
            f.write(image_bytes)
        
        return f"Image successfully saved to '{output_path.resolve()}'"