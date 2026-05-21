from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileEntry:
    path: Path
    size: int
    hash: str | None = None
    duration_seconds: float | None = None
