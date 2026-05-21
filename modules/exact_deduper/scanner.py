from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
import re
import subprocess
from typing import Iterable

from modules.exact_deduper.models import FileEntry
from modules.exact_deduper.utils import hash_file

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"}


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


def _normalize_filter_paths(paths: list[Path | str] | None) -> list[Path]:
    try:
        return [Path(p).resolve() for p in paths] if paths else []
    except Exception:
        return []


def _is_allowed(path: Path, include_paths: list[Path], ignore_paths: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    if include_paths:
        try:
            if not any(resolved.is_relative_to(allowed) for allowed in include_paths):
                return False
        except AttributeError:
            resolved_str = str(resolved)
            if not any(str(allowed) in resolved_str for allowed in include_paths):
                return False

    if ignore_paths:
        try:
            if any(resolved.is_relative_to(blocked) for blocked in ignore_paths):
                return False
        except AttributeError:
            resolved_str = str(resolved)
            if any(str(blocked) in resolved_str for blocked in ignore_paths):
                return False

    return True


def _discover_files(
    root: Path,
    recursive: bool,
    min_size_bytes: int,
    progress_callback,
    cancel_check,
    include_dirs: list[Path | str] | None,
    ignore_dirs: list[Path | str] | None,
    mode: str,
) -> tuple[list[FileEntry], int] | None:
    files: list[FileEntry] = []

    include_paths = _normalize_filter_paths(include_dirs)
    ignore_paths = _normalize_filter_paths(ignore_dirs)

    _emit(progress_callback, "scan_start", root=str(root), mode=mode)
    walker: Iterable[Path] = root.rglob("*") if recursive else root.iterdir()
    file_count = 0

    for path in walker:
        if _should_cancel(cancel_check):
            _emit(progress_callback, "scan_cancelled")
            return None

        if not path.is_file():
            continue

        if not _is_allowed(path, include_paths, ignore_paths):
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

    return files, file_count


def _normalize_name_for_matching(path: Path) -> str:
    name = path.stem.lower()
    name = re.sub(r"\bcopy\b", "", name)
    name = re.sub(r"\(\d+\)$", "", name)
    name = re.sub(r"[ _-]\d+$", "", name)
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return " ".join(name.split())


def _probe_video_duration_seconds(path: Path) -> float | None:
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return None

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return None

    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        duration = float(raw)
    except ValueError:
        return None
    return duration if duration > 0 else None


def _compute_relative_score(a: FileEntry, b: FileEntry) -> float:
    name_a = _normalize_name_for_matching(a.path)
    name_b = _normalize_name_for_matching(b.path)
    name_score = SequenceMatcher(None, name_a, name_b).ratio()

    large = max(a.size, b.size, 1)
    size_score = min(a.size, b.size) / large

    duration_score = None
    if a.duration_seconds and b.duration_seconds and a.duration_seconds > 0 and b.duration_seconds > 0:
        larger = max(a.duration_seconds, b.duration_seconds)
        duration_score = max(0.0, 1.0 - abs(a.duration_seconds - b.duration_seconds) / larger)

    weights: list[tuple[float, float]] = [
        (name_score, 0.5),
        (size_score, 0.35),
    ]
    if duration_score is not None:
        weights.append((duration_score, 0.15))

    weighted_total = sum(score * weight for score, weight in weights)
    total_weight = sum(weight for _, weight in weights)
    if total_weight <= 0:
        return 0.0
    return weighted_total / total_weight


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
    """
    root = Path(root)
    discovered = _discover_files(
        root=root,
        recursive=recursive,
        min_size_bytes=min_size_bytes,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        include_dirs=include_dirs,
        ignore_dirs=ignore_dirs,
        mode="exact",
    )
    if discovered is None:
        return []
    files, file_count = discovered

    size_groups: dict[int, list[FileEntry]] = defaultdict(list)
    for entry in files:
        size_groups[entry.size].append(entry)

    candidate_groups = {size: group for size, group in size_groups.items() if len(group) > 1}
    duplicate_groups = []

    total_to_hash = sum(len(g) for g in candidate_groups.values())
    _emit(progress_callback, "hash_start", total=total_to_hash)

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
                duplicate_groups.append(
                    {
                        "mode": "exact",
                        "size": size,
                        "hash": file_hash,
                        "files": entries,
                    }
                )

    _emit(progress_callback, "scan_complete", groups=len(duplicate_groups), files_scanned=file_count, mode="exact")
    return duplicate_groups


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int):
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


def scan_for_relative_duplicates(
    root: Path | str,
    recursive: bool = True,
    min_size_bytes: int = 1,
    progress_callback=None,
    cancel_check=None,
    include_dirs: list[Path | str] | None = None,
    ignore_dirs: list[Path | str] | None = None,
    min_relative_match_pct: float = 85.0,
) -> list[dict]:
    root = Path(root)
    discovered = _discover_files(
        root=root,
        recursive=recursive,
        min_size_bytes=min_size_bytes,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        include_dirs=include_dirs,
        ignore_dirs=ignore_dirs,
        mode="relative",
    )
    if discovered is None:
        return []
    files, file_count = discovered

    for entry in files:
        if _should_cancel(cancel_check):
            _emit(progress_callback, "scan_cancelled")
            return []
        entry.duration_seconds = _probe_video_duration_seconds(entry.path)

    n = len(files)
    if n < 2:
        _emit(progress_callback, "scan_complete", groups=0, files_scanned=file_count, mode="relative")
        return []

    min_threshold = max(0.0, min(float(min_relative_match_pct), 100.0)) / 100.0
    near_name_gate = min(0.65, max(0.35, min_threshold - 0.2))
    near_size_gate = min(0.9, max(0.5, min_threshold - 0.15))

    total_pairs = (n * (n - 1)) // 2
    _emit(progress_callback, "compare_start", total=total_pairs)

    uf = _UnionFind(n)
    accepted_pairs: dict[tuple[int, int], float] = {}

    pair_idx = 0
    for i in range(n - 1):
        file_a = files[i]
        name_a = _normalize_name_for_matching(file_a.path)

        for j in range(i + 1, n):
            if _should_cancel(cancel_check):
                _emit(progress_callback, "scan_cancelled")
                return []

            file_b = files[j]
            pair_idx += 1

            if pair_idx % 250 == 0 or pair_idx == total_pairs:
                _emit(progress_callback, "compare_progress", current=pair_idx, total=total_pairs)

            # First pass gate to keep first implementation reasonably fast.
            if file_a.path.suffix.lower() != file_b.path.suffix.lower():
                continue

            name_b = _normalize_name_for_matching(file_b.path)
            fast_name = SequenceMatcher(None, name_a, name_b).ratio()
            if fast_name < near_name_gate:
                continue

            large_size = max(file_a.size, file_b.size, 1)
            fast_size = min(file_a.size, file_b.size) / large_size
            if fast_size < near_size_gate:
                continue

            score = _compute_relative_score(file_a, file_b)
            if score >= min_threshold:
                uf.union(i, j)
                accepted_pairs[(i, j)] = score

    groups_by_root: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        groups_by_root[uf.find(idx)].append(idx)

    relative_groups: list[dict] = []
    for members in groups_by_root.values():
        if len(members) < 2:
            continue

        member_set = set(members)
        member_scores = [
            score
            for (i, j), score in accepted_pairs.items()
            if i in member_set and j in member_set
        ]
        if member_scores:
            relative_match_pct = round((sum(member_scores) / len(member_scores)) * 100, 1)
        else:
            relative_match_pct = round(min_threshold * 100, 1)

        entries = [files[idx] for idx in members]
        relative_groups.append(
            {
                "mode": "relative",
                "size": max((entry.size for entry in entries), default=0),
                "hash": None,
                "files": entries,
                "relative_match_pct": relative_match_pct,
            }
        )

    _emit(progress_callback, "scan_complete", groups=len(relative_groups), files_scanned=file_count, mode="relative")
    return relative_groups


def scan_for_duplicates(
    root: Path | str,
    mode: str = "exact",
    recursive: bool = True,
    min_size_bytes: int = 1,
    progress_callback=None,
    cancel_check=None,
    include_dirs: list[Path | str] | None = None,
    ignore_dirs: list[Path | str] | None = None,
    min_relative_match_pct: float = 85.0,
) -> list[dict]:
    selected_mode = (mode or "exact").strip().lower()
    if selected_mode == "relative":
        return scan_for_relative_duplicates(
            root=root,
            recursive=recursive,
            min_size_bytes=min_size_bytes,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            include_dirs=include_dirs,
            ignore_dirs=ignore_dirs,
            min_relative_match_pct=min_relative_match_pct,
        )
    return scan_for_exact_duplicates(
        root=root,
        recursive=recursive,
        min_size_bytes=min_size_bytes,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        include_dirs=include_dirs,
        ignore_dirs=ignore_dirs,
    )

