from collections import defaultdict
from difflib import SequenceMatcher
import math
from pathlib import Path
import re
import subprocess
from typing import Iterable

from modules.exact_deduper.models import FileEntry
from modules.exact_deduper.utils import hash_file

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"}

# Relative matching is intentionally a heuristic.  These limits keep a folder
# with many generic names (for example, thousands of screenshots) from turning
# a scan into a quadratic number of comparisons.
MAX_RELATIVE_SIGNATURE_BUCKET = 200
MAX_RELATIVE_NGRAMS = 12


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


def _relative_name_signatures(normalized_name: str) -> set[str]:
    """Return inexpensive, reasonably distinctive lookup keys for a name."""
    compact = normalized_name.replace(" ", "")
    signatures = set()

    # Long words make good blocks for reordered or lightly edited filenames.
    tokens = sorted(
        {token for token in normalized_name.split() if len(token) >= 4},
        key=lambda token: (-len(token), token),
    )
    signatures.update(f"token:{token}" for token in tokens[:4])

    # Four-character slices also cover a single edited word such as
    # "holidayvideo" -> "holidayvideos".  Sample long names so one unusually
    # long filename cannot dominate the index.
    if len(compact) >= 4:
        starts = range(0, len(compact) - 3)
        if len(compact) > MAX_RELATIVE_NGRAMS + 3:
            step = (len(compact) - 4) / max(1, MAX_RELATIVE_NGRAMS - 1)
            starts = {round(position * step) for position in range(MAX_RELATIVE_NGRAMS)}
        signatures.update(f"gram:{compact[start:start + 4]}" for start in starts)

    return signatures


def _relative_size_bucket(size: int, size_gate: float) -> int:
    """Group sizes where files in the same bucket pass the fast size gate."""
    if size <= 0:
        return 0
    # With a 0.70 gate, one bucket spans less than 1 / 0.70 times in size.
    bucket_base = 1.0 / max(size_gate, 0.01)
    return int(math.log(size) / math.log(bucket_base))


def _add_block_candidate_pairs(
    indices: list[int],
    files: list[FileEntry],
    size_gate: float,
    candidate_pairs: set[tuple[int, int]],
):
    """Add a linear number of useful comparison pairs from one name block.

    A previous version compared every member of every extension with every
    other member.  Here each size-compatible block is connected to an anchor,
    plus neighbouring anchors.  This preserves the useful transitive grouping
    behaviour while avoiding a combinatorial explosion for common names.
    """
    if len(indices) < 2:
        return

    by_size_bucket: dict[int, list[int]] = defaultdict(list)
    for index in indices:
        by_size_bucket[_relative_size_bucket(files[index].size, size_gate)].append(index)

    ordered_buckets = sorted(by_size_bucket)
    previous_anchor = None
    for bucket in ordered_buckets:
        members = by_size_bucket[bucket]
        anchor = members[0]
        for index in members[1:]:
            candidate_pairs.add((min(anchor, index), max(anchor, index)))
        if previous_anchor is not None:
            candidate_pairs.add((min(previous_anchor, anchor), max(previous_anchor, anchor)))
        previous_anchor = anchor


def _build_relative_candidate_pairs(
    files: list[FileEntry],
    size_gate: float,
    progress_callback=None,
    cancel_check=None,
) -> tuple[list[str], set[tuple[int, int]]] | None:
    """Build a bounded set of plausible relative-match comparisons.

    Files must share an extension.  Exact normalized names are always indexed;
    fuzzy blocks use distinctive word and four-character signatures, while
    exceptionally common signatures are ignored.  The final scorer remains
    the authority on whether a pair is a relative match.
    """
    normalized_names = [_normalize_name_for_matching(entry.path) for entry in files]
    exact_name_blocks: dict[tuple[str, str], list[int]] = defaultdict(list)
    fuzzy_blocks: dict[tuple[str, str], list[int]] = defaultdict(list)

    for index, entry in enumerate(files):
        if _should_cancel(cancel_check):
            return None
        suffix = entry.path.suffix.lower()
        name = normalized_names[index]
        if name:
            exact_name_blocks[(suffix, name)].append(index)
            for signature in _relative_name_signatures(name):
                fuzzy_blocks[(suffix, signature)].append(index)

    candidate_pairs: set[tuple[int, int]] = set()
    for indices in exact_name_blocks.values():
        _add_block_candidate_pairs(indices, files, size_gate, candidate_pairs)

    # Generic signatures such as "gram:tion" are not selective enough to be
    # useful and were the source of most of the old scan's work.
    for indices in fuzzy_blocks.values():
        if len(indices) <= MAX_RELATIVE_SIGNATURE_BUCKET:
            _add_block_candidate_pairs(indices, files, size_gate, candidate_pairs)

    _emit(progress_callback, "candidate_plan", candidates=len(candidate_pairs), files=len(files))
    return normalized_names, candidate_pairs


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


def _compute_relative_score(a: FileEntry, b: FileEntry, name_score: float | None = None) -> float:
    if name_score is None:
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

    n = len(files)
    if n < 2:
        _emit(progress_callback, "scan_complete", groups=0, files_scanned=file_count, mode="relative")
        return []

    min_threshold = max(0.0, min(float(min_relative_match_pct), 100.0)) / 100.0
    near_name_gate = min(0.65, max(0.35, min_threshold - 0.2))
    near_size_gate = min(0.9, max(0.5, min_threshold - 0.15))

    candidate_data = _build_relative_candidate_pairs(
        files,
        near_size_gate,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    if candidate_data is None:
        _emit(progress_callback, "scan_cancelled")
        return []
    normalized_names, candidate_pairs = candidate_data

    # ffprobe is an external process and was previously run for every video in
    # the folder.  Only candidates can influence a relative-match result, so
    # defer probes until after the cheap name/size blocking step.
    candidate_video_indices = sorted({
        index
        for pair in candidate_pairs
        for index in pair
        if files[index].path.suffix.lower() in VIDEO_EXTENSIONS
    })
    _emit(progress_callback, "metadata_start", total=len(candidate_video_indices))
    for position, index in enumerate(candidate_video_indices, start=1):
        if _should_cancel(cancel_check):
            _emit(progress_callback, "scan_cancelled")
            return []
        files[index].duration_seconds = _probe_video_duration_seconds(files[index].path)
        if position % 25 == 0 or position == len(candidate_video_indices):
            _emit(progress_callback, "metadata_progress", current=position, total=len(candidate_video_indices))

    total_pairs = len(candidate_pairs)
    _emit(progress_callback, "compare_start", total=total_pairs)

    uf = _UnionFind(n)
    accepted_pairs: dict[tuple[int, int], float] = {}

    pair_idx = 0
    progress_step = max(1, total_pairs // 300)
    for i, j in sorted(candidate_pairs):
        if _should_cancel(cancel_check):
            _emit(progress_callback, "scan_cancelled")
            return []

        file_a = files[i]
        file_b = files[j]
        pair_idx += 1

        if pair_idx % progress_step == 0 or pair_idx == total_pairs:
            _emit(progress_callback, "compare_progress", current=pair_idx, total=total_pairs)

        fast_name = SequenceMatcher(None, normalized_names[i], normalized_names[j]).ratio()
        if fast_name < near_name_gate:
            continue

        large_size = max(file_a.size, file_b.size, 1)
        fast_size = min(file_a.size, file_b.size) / large_size
        if fast_size < near_size_gate:
            continue

        score = _compute_relative_score(file_a, file_b, name_score=fast_name)
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

