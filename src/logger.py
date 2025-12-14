import json
import time
from pathlib import Path
from datetime import datetime

class SessionLogger:
    def __init__(self):
        self.log_dir = Path("tmp")
        self.log_dir.mkdir(exist_ok=True)
        self.session_file = self.log_dir / f"session_{int(time.time())}.jsonl"

    def log(self, role: str, event: str, data: dict = None):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "role": role,
            "event": event,
            "data": data or {}
        }
        with open(self.session_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

sys_logger = SessionLogger()