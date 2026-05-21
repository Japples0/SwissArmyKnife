from pathlib import Path
from collections import defaultdict
from typing import Iterable

from modules.exact_deduper.models import FileEntry
from modules.exact_deduper.utils import hash_file

def _emit(callback, event, **data):
    if callback:
        try:
            callback(event, data)
        except Exception:
            # Scanner must NEVER crash because UI misbehaved
            pass

def _should_cancel(cancel_check):
    try:
        return cancel_check and cancel_check()
    except Exception:
        return False

def scan_for_exact_duplicates(
    root: Path | str,
    recursive: bool = True,
    min_size_bytes: int = 1,
    progress_callback=None,
    cancel_check=None,
    include_dirs: list[Path | str] | None = None,
    ignore_dirs: list[Path | str] | None = None,
) -> list[dict]:


    """
    Scan a directory for exact (byte-for-byte) duplicate files.

    Returns a list of duplicate groups:
    [
        {
            "size": int,
            "hash": str,
            "files": list[FileEntry]
        }
    ]
    """

    root = Path(root)
    files: list[FileEntry] = []

    # Normalize include/ignore directories (absolute paths)
    try:
        include_paths = [Path(p).resolve() for p in include_dirs] if include_dirs else []
    except Exception:
        include_paths = []
    try:
        ignore_paths = [Path(p).resolve() for p in ignore_dirs] if ignore_dirs else []
    except Exception:
        ignore_paths = []

    def _allowed(p: Path) -> bool:
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if include_paths:
            try:
                if not any(rp.is_relative_to(ip) for ip in include_paths):
                    return False
            except AttributeError:
                # Fallback if is_relative_to unavailable
                rp_str = str(rp)
                if not any(str(ip) in rp_str for ip in include_paths):
                    return False
        if ignore_paths:
            try:
                if any(rp.is_relative_to(ip) for ip in ignore_paths):
                    return False
            except AttributeError:
                rp_str = str(rp)
                if any(str(ip) in rp_str for ip in ignore_paths):
                    return False
        return True

    # --------------------
    # Pass 1: Discover files
    # --------------------
    _emit(progress_callback, "scan_start", root=str(root))
    walker: Iterable[Path]
    walker = root.rglob("*") if recursive else root.iterdir()

    file_count = 0

    for path in walker:
        if _should_cancel(cancel_check):
            _emit(progress_callback, "scan_cancelled")
            return []

        if not path.is_file():
            continue

        # Honor expert include/ignore filters, if any
        if not _allowed(path):
            continue

        try:
            size = path.stat().st_size
        except OSError:
            continue

        if size < min_size_bytes:
            continue

        files.append(FileEntry(path=path, size=size))
        file_count += 1

        if file_count % 100 == 0:
            _emit(progress_callback, "file_discovered", count=file_count)

    # --------------------
    # Pass 2: Group by size
    # --------------------
    size_groups: dict[int, list[FileEntry]] = defaultdict(list)
    for entry in files:
        size_groups[entry.size].append(entry)

    # Only groups with more than one file are interesting
    candidate_groups = {
        size: group
        for size, group in size_groups.items()
        if len(group) > 1
    }

    # --------------------
    # Pass 3: Hash & group
    # --------------------
    duplicate_groups = []

    total_to_hash = sum(len(g) for g in candidate_groups.values())
    _emit(
        progress_callback,
        "hash_start",
        total=total_to_hash
    )

    hashed = 0

    for size, group in candidate_groups.items():
        hash_groups = defaultdict(list)

        for entry in group:
            if _should_cancel(cancel_check):
                _emit(progress_callback, "scan_cancelled")
                return []

            try:
                entry.hash = hash_file(entry.path)
            except OSError:
                continue

            hash_groups[entry.hash].append(entry)

            hashed += 1
            _emit(
                progress_callback,
                "hash_progress",
                current=hashed,
                total=total_to_hash,
                file=str(entry.path),
            )

        for file_hash, entries in hash_groups.items():
            if len(entries) > 1:
                duplicate_groups.append({
                    "size": size,
                    "hash": file_hash,
                    "files": entries
                })

    _emit(
        progress_callback,
        "scan_complete",
        groups=len(duplicate_groups),
        files_scanned=file_count
    )

    return duplicate_groups

