import subprocess
import sys
from ..protocol import TaskOutput

def execute(code: str) -> TaskOutput:
    try:
        process = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30
        )
        if process.returncode == 0:
            return TaskOutput(status="success", result=process.stdout.strip())
        else:
            return TaskOutput(status="error", result=process.stderr.strip())
    except Exception as e:
        return TaskOutput(status="error", result=str(e))