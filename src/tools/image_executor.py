from pathlib import Path
from ..protocol import TaskOutput
from .pollinations_client import PollinationsClient

def execute(client: PollinationsClient, **params) -> TaskOutput:
    try:
        image_bytes = client.generate_image(**params)
        
        output_dir = Path("outputs")
        output_dir.mkdir(exist_ok=True)
        
        prompt_for_filename = params.get('prompt', 'image')
        safe_filename = "".join(c for c in prompt_for_filename if c.isalnum() or c in " _-").rstrip()[:50]
        output_path = output_dir / f"{safe_filename}.jpg"
        
        with open(output_path, "wb") as f:
            f.write(image_bytes)
        
        return TaskOutput(status="success", result=f"Image saved to '{output_path.resolve()}'")
    except Exception as e:
        return TaskOutput(status="error", result=str(e))