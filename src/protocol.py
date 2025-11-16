from dataclasses import dataclass, field
from typing import Any, Dict, Literal

@dataclass
class TaskInput:
    task_name: str
    params: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TaskOutput:
    status: Literal["success", "error"]
    result: Any