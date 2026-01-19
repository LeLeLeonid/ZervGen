import docker
import os
import tarfile
import io
from pathlib import Path
from typing import Optional

class SandboxManager:
    def __init__(self, image: str = "python:3.11-slim", timeout: int = 30):
        self.image = image
        self.timeout = timeout
        self.client = None
        try:
            self.client = docker.from_env()
            self.client.ping()
            print(f"[Sandbox] Docker Connected. Pulling {image}...")
            try:
                self.client.images.pull(image)
            except:
                print(f"[Sandbox] Warning: Could not pull {image}, trying local...")      
        except Exception as e:
            return
            print(f"[Sandbox] Docker Error: {e}")
            print("[Sandbox] TIP: Ensure Docker Desktop is running and 'Expose daemon on tcp://localhost:2375' is OFF (or configured correctly).")
            self.client = None

    def is_active(self):
        return self.client is not None

    def execute(self, code: str, work_dir: str = "./tmp/workspace") -> str:
        """
        Runs Python code in a disposable container.
        Mounts 'work_dir' to /app so files persist if needed.
        """
        if not self.client:
            return "Error: Docker not available. Cannot execute safely."
        
        abs_work_dir = os.path.abspath(work_dir)
        os.makedirs(abs_work_dir, exist_ok=True)

        container = None
        try:
            container = self.client.containers.run(
                self.image,
                command="sleep infinity",
                working_dir="/app",
                volumes={abs_work_dir: {'bind': '/app', 'mode': 'rw'}},
                detach=True,
                mem_limit="512m",
                network_mode="none"
            )
            wrapped_code = f"""
import sys
try:
{'\n'.join(['    ' + line for line in code.splitlines()])}
except Exception as e:
    print(f"Runtime Error: {{e}}", file=sys.stderr)
"""
            
            exec_result = container.exec_run(
                ["python", "-c", wrapped_code],
                workdir="/app"
            )
            
            output = exec_result.output.decode("utf-8")
            
            if exec_result.exit_code != 0:
                return f"[EXIT CODE {exec_result.exit_code}]\n{output}"
            
            return output if output.strip() else "[Success: No Output]"

        except Exception as e:
            return f"Sandbox Exception: {e}"
            
        finally:
            if container:
                try:
                    container.remove(force=True)
                except: pass

sandbox = SandboxManager()