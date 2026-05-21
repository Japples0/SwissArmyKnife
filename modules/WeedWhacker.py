import csv
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# Windows file attribute flags that can hint at cloud placeholder/sync behavior.
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
SHA_LIKE_RE = re.compile(r"^[A-Fa-f0-9]{48,128}$")
INVALID_WIN_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_PATH_WARNING = 245
MAX_FILENAME_LEN = 180
SYNC_ISSUE_STATUSES = (
    "Sync Pending",
    "Excluded from Sync",
)


@dataclass
class SyncFinding:
    path: str
    status: str
    reason: str
    attrs: str


def _safe_get_file_attrs(path: str) -> int | None:
    """Read Windows file attributes using Kernel32. Returns None if unavailable."""
    if os.name != "nt":
        return None

    try:
        import ctypes

        get_attrs = ctypes.windll.kernel32.GetFileAttributesW
        get_attrs.argtypes = [ctypes.c_wchar_p]
        get_attrs.restype = ctypes.c_uint32
        attrs = int(get_attrs(path))
        if attrs == INVALID_FILE_ATTRIBUTES:
            return None
        return attrs
    except Exception:
        return None


def _decode_attr_flags(attrs: int | None) -> str:
    if attrs is None:
        return ""

    flags = []
    if attrs & FILE_ATTRIBUTE_OFFLINE:
        flags.append("OFFLINE")
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        flags.append("REPARSE_POINT")
    if attrs & FILE_ATTRIBUTE_RECALL_ON_OPEN:
        flags.append("RECALL_ON_OPEN")
    if attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
        flags.append("RECALL_ON_DATA_ACCESS")
    if attrs & FILE_ATTRIBUTE_PINNED:
        flags.append("PINNED")
    if attrs & FILE_ATTRIBUTE_UNPINNED:
        flags.append("UNPINNED")
    if attrs & FILE_ATTRIBUTE_SYSTEM:
        flags.append("SYSTEM")
    return ", ".join(flags)


def _is_sha_like_name(filename_stem: str) -> bool:
    return bool(SHA_LIKE_RE.fullmatch(filename_stem or ""))


def _looks_problematic_for_sync(path: Path) -> bool:
    return (
        _is_sha_like_name(path.stem)
        or len(path.name) >= 120
        or len(str(path)) >= MAX_PATH_WARNING
    )


def _sanitize_filename_component(value: str, fallback: str = "RecoveredFile") -> str:
    cleaned = INVALID_WIN_NAME_RE.sub("_", value or "")
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


def _truncate_filename(name: str, max_len: int = MAX_FILENAME_LEN) -> str:
    if len(name) <= max_len:
        return name
    p = Path(name)
    ext = p.suffix
    stem = p.stem
    max_stem = max(8, max_len - len(ext))
    short_stem = stem[:max_stem].rstrip(" .")
    if not short_stem:
        short_stem = "RecoveredFile"
    return f"{short_stem}{ext}"


def _next_available_path(base_dir: Path, filename: str) -> Path:
    candidate = base_dir / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    ext = candidate.suffix
    idx = 2
    while True:
        option = base_dir / f"{stem}_{idx}{ext}"
        if not option.exists():
            return option
        idx += 1


def _is_subpath(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _for_fs(path: Path) -> str:
    """
    Convert to extended-length path form on Windows.
    This helps operations on extra-long paths.
    """
    raw = str(path)
    if os.name != "nt":
        return raw
    if raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def _analyze_file(path: str) -> list[SyncFinding]:
    """
    Heuristic iCloud issue detector for Windows.

    It flags files that look like:
    - Sync Pending (cloud placeholder/offline/unpinned states)
    - Excluded from Sync (likely unsupported/ignored patterns)
    """
    attrs = _safe_get_file_attrs(path)
    attr_text = _decode_attr_flags(attrs)
    findings: list[SyncFinding] = []

    pending_reasons = []
    if attrs is not None:
        if attrs & FILE_ATTRIBUTE_OFFLINE:
            pending_reasons.append("Marked OFFLINE")
        if attrs & FILE_ATTRIBUTE_RECALL_ON_OPEN:
            pending_reasons.append("Has RECALL_ON_OPEN placeholder state")
        if attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
            pending_reasons.append("Has RECALL_ON_DATA_ACCESS placeholder state")
        if (attrs & FILE_ATTRIBUTE_UNPINNED) and not (attrs & FILE_ATTRIBUTE_PINNED):
            pending_reasons.append("Marked UNPINNED")

    if pending_reasons:
        findings.append(
            SyncFinding(
                path=path,
                status="Sync Pending",
                reason="; ".join(pending_reasons),
                attrs=attr_text,
            )
        )

    excluded_reasons = []
    p = Path(path)
    low_name = p.name.lower()

    # Common files that cloud providers often skip or treat as transient.
    if low_name.startswith("~$"):
        excluded_reasons.append("Office lock/temp file (~$ prefix)")
    if low_name.endswith((".tmp", ".part", ".partial", ".download", ".crdownload")):
        excluded_reasons.append("Temporary/incomplete file extension")

    # Path length can be a practical sync blocker for some setups.
    if len(str(p)) >= MAX_PATH_WARNING:
        excluded_reasons.append(f"Path is very long (>={MAX_PATH_WARNING} chars)")

    # Links/reparse entries are often problematic for cloud sync roots.
    try:
        if p.is_symlink():
            excluded_reasons.append("Symbolic link")
    except Exception:
        pass

    if attrs is not None and (attrs & FILE_ATTRIBUTE_SYSTEM):
        excluded_reasons.append("SYSTEM attribute is set")

    if _is_sha_like_name(p.stem):
        excluded_reasons.append("Filename looks like SHA-style hash")

    if excluded_reasons:
        findings.append(
            SyncFinding(
                path=path,
                status="Excluded from Sync",
                reason="; ".join(excluded_reasons),
                attrs=attr_text,
            )
        )

    return findings


class WeedWhackerFrame(ttk.Frame):
    def __init__(self, parent, app=None):
        super().__init__(parent, padding=10)
        self.app = app

        self._state = self._load_state()
        initial_root = self._pick_existing_dir(
            self._state.get("last_root_dir"),
            Path.home(),
        )
        initial_quarantine = self._pick_existing_dir(
            self._state.get("last_quarantine_dir"),
            Path.home() / "SAF_Quarantine",
            Path.home(),
        )

        self.root_path_var = tk.StringVar(value=initial_root)
        self.quarantine_path_var = tk.StringVar(value=initial_quarantine)
        self.rename_to_folder_var = tk.BooleanVar(value=True)
        self.rename_only_problematic_var = tk.BooleanVar(value=True)
        self.restore_use_original_name_var = tk.BooleanVar(value=False)
        self.summary_var = tk.StringVar(value="Ready.")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._event_queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._scan_thread: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._scanning = False
        self._quarantining = False
        self._total_files = 0
        self._scanned_files = 0
        self._flagged_files = 0
        self._error_count = 0

        self._item_meta: dict[str, dict] = {}

        self._build_ui()
        self._on_toggle_change("init")
        self._refresh_action_states()

    def _build_ui(self):
        header = ttk.LabelFrame(self, text="iCloud Weed Whacker", padding=10)
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Root Folder:").grid(row=0, column=0, sticky="w")
        root_entry = ttk.Entry(header, textvariable=self.root_path_var)
        root_entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        root_entry.bind("<Return>", lambda _e: self._on_start_scan())
        self.browse_root_btn = ttk.Button(header, text="Browse...", command=self._browse_root)
        self.browse_root_btn.grid(row=0, column=2, sticky="ew")

        ttk.Label(header, text="Quarantine Folder:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        q_entry = ttk.Entry(header, textvariable=self.quarantine_path_var)
        q_entry.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        self.browse_quarantine_btn = ttk.Button(header, text="Browse...", command=self._browse_quarantine)
        self.browse_quarantine_btn.grid(row=1, column=2, sticky="ew", pady=(8, 0))

        controls = ttk.Frame(header)
        controls.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        controls.columnconfigure(8, weight=1)

        self.start_btn = ttk.Button(controls, text="Scan iCloud Status", command=self._on_start_scan)
        self.start_btn.grid(row=0, column=0, padx=(0, 8))

        self.cancel_btn = ttk.Button(controls, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=(0, 8))

        self.clear_btn = ttk.Button(controls, text="Clear Results", command=self._clear_results)
        self.clear_btn.grid(row=0, column=2, padx=(0, 8))

        self.open_selected_btn = ttk.Button(controls, text="Open Selected", command=self._open_selected)
        self.open_selected_btn.grid(row=0, column=3, padx=(0, 8))

        self.quarantine_selected_btn = ttk.Button(
            controls,
            text="Quarantine Selected",
            command=self._on_quarantine_selected,
        )
        self.quarantine_selected_btn.grid(row=0, column=4, padx=(0, 8))

        self.quarantine_all_btn = ttk.Button(
            controls,
            text="Quarantine All Flagged",
            command=self._on_quarantine_all,
        )
        self.quarantine_all_btn.grid(row=0, column=5, padx=(0, 8))

        self.restore_manifest_btn = ttk.Button(
            controls,
            text="Restore From Manifest",
            command=self._on_restore_manifest,
        )
        self.restore_manifest_btn.grid(row=0, column=6, padx=(0, 8))

        self.reindex_manifest_btn = ttk.Button(
            controls,
            text="Reindex Quarantine",
            command=self._on_reindex_manifest,
        )
        self.reindex_manifest_btn.grid(row=0, column=7, padx=(0, 8))

        self.progress = ttk.Progressbar(
            controls,
            orient="horizontal",
            mode="determinate",
            variable=self.progress_var,
            maximum=100,
        )
        self.progress.grid(row=0, column=8, sticky="ew")

        options = ttk.Frame(header)
        options.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.rename_to_folder_chk = ttk.Checkbutton(
            options,
            text="Rename quarantined files to enclosing folder name",
            variable=self.rename_to_folder_var,
            command=lambda: self._on_toggle_change("rename"),
        )
        self.rename_to_folder_chk.grid(row=0, column=0, sticky="w")
        self.rename_only_problematic_chk = ttk.Checkbutton(
            options,
            text="Only rename SHA-like / long-path filenames",
            variable=self.rename_only_problematic_var,
            command=lambda: self._on_toggle_change("problematic"),
        )
        self.rename_only_problematic_chk.grid(row=0, column=1, sticky="w", padx=(16, 0))
        self.restore_original_name_chk = ttk.Checkbutton(
            options,
            text="Use original filenames when restoring (riskier for long paths)",
            variable=self.restore_use_original_name_var,
            command=lambda: self._on_toggle_change("restore"),
        )
        self.restore_original_name_chk.grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.open_quarantine_btn = ttk.Button(options, text="Open Quarantine Folder", command=self._open_quarantine)
        self.open_quarantine_btn.grid(row=0, column=3, padx=(16, 0))

        safety_note = ttk.Label(
            header,
            text=(
                "Safety: Restore can recover renamed quarantine files by size/hash. "
                "Use 'Reindex Quarantine' to refresh manifest paths before restore."
            ),
            foreground="#444",
        )
        safety_note.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        summary = ttk.Label(self, textvariable=self.summary_var, anchor="w")
        summary.pack(fill="x", pady=(8, 8))

        results_frame = ttk.LabelFrame(self, text="Flagged Files", padding=8)
        results_frame.pack(fill="both", expand=True)
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)

        columns = ("status", "name", "folder", "reason")
        self.tree = ttk.Treeview(results_frame, columns=columns, show="headings", selectmode="extended")

        self.tree.heading("status", text="Status")
        self.tree.heading("name", text="File")
        self.tree.heading("folder", text="Folder")
        self.tree.heading("reason", text="Why Flagged")

        self.tree.column("status", width=150, anchor="w")
        self.tree.column("name", width=220, anchor="w")
        self.tree.column("folder", width=360, anchor="w")
        self.tree.column("reason", width=430, anchor="w")

        ybar = ttk.Scrollbar(results_frame, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(results_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")

        self.tree.bind("<Double-1>", self._on_tree_double_click)

        log_frame = ttk.LabelFrame(self, text="Scan Log", padding=8)
        log_frame.pack(fill="both", expand=False, pady=(8, 0))

        self.log_text = tk.Text(log_frame, height=8, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def notify_mode_change(self):
        # This module currently does not vary behavior by app mode.
        pass

    def _on_toggle_change(self, changed: str):
        """
        Keep rename/restore choices predictable and avoid conflicting combinations.
        """
        if changed == "restore" and self.restore_use_original_name_var.get():
            # Restore-original mode: disable quarantine renaming options.
            self.rename_to_folder_var.set(False)
            self.rename_only_problematic_var.set(False)

        if changed in {"rename", "problematic"}:
            if self.rename_only_problematic_var.get():
                self.rename_to_folder_var.set(True)

            if self.rename_to_folder_var.get():
                # Quarantine renaming active: keep restore mode in safer default.
                self.restore_use_original_name_var.set(False)

        if not self.rename_to_folder_var.get():
            self.rename_only_problematic_var.set(False)

        if hasattr(self, "rename_only_problematic_chk"):
            state = "normal" if self.rename_to_folder_var.get() else "disabled"
            self.rename_only_problematic_chk.configure(state=state)

    def _state_file(self) -> Path:
        return Path(__file__).resolve().with_name("weedwhacker_state.json")

    def _load_state(self) -> dict:
        try:
            state_path = self._state_file()
            if state_path.exists():
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception:
            pass
        return {}

    def _save_state(self):
        try:
            state_path = self._state_file()
            tmp_path = state_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            tmp_path.replace(state_path)
        except Exception:
            # State persistence should never interrupt core workflow.
            pass

    def _remember_dir(self, key: str, value: str | Path):
        try:
            p = Path(value).expanduser()
            if p.exists() and p.is_file():
                p = p.parent
            self._state[key] = str(p)
            self._save_state()
        except Exception:
            pass

    def _pick_existing_dir(self, *candidates) -> str:
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                p = Path(candidate).expanduser()
                if p.exists() and p.is_file():
                    p = p.parent
                if p.exists() and p.is_dir():
                    return str(p)
            except Exception:
                continue
        return str(Path.home())

    def _is_busy(self) -> bool:
        return self._scanning or self._quarantining

    def _refresh_action_states(self):
        busy = self._is_busy()
        passive_state = "disabled" if busy else "normal"

        self.start_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if self._scanning else "disabled")
        self.clear_btn.configure(state=passive_state)
        self.open_selected_btn.configure(state=passive_state)
        self.quarantine_selected_btn.configure(state=passive_state)
        self.quarantine_all_btn.configure(state=passive_state)
        self.restore_manifest_btn.configure(state=passive_state)
        self.reindex_manifest_btn.configure(state=passive_state)
        self.browse_root_btn.configure(state=passive_state)
        self.browse_quarantine_btn.configure(state=passive_state)
        self.open_quarantine_btn.configure(state=passive_state)
        if hasattr(self, "rename_to_folder_chk"):
            self.rename_to_folder_chk.configure(state=passive_state)
        if hasattr(self, "restore_original_name_chk"):
            self.restore_original_name_chk.configure(state=passive_state)
        if hasattr(self, "rename_only_problematic_chk"):
            if busy:
                self.rename_only_problematic_chk.configure(state="disabled")
            else:
                toggle_state = "normal" if self.rename_to_folder_var.get() else "disabled"
                self.rename_only_problematic_chk.configure(state=toggle_state)

    def _browse_root(self):
        initial_dir = self._pick_existing_dir(self.root_path_var.get(), self._state.get("last_root_dir"))
        path = filedialog.askdirectory(title="Select iCloud root folder", initialdir=initial_dir)
        if path:
            self.root_path_var.set(path)
            self._remember_dir("last_root_dir", path)

    def _browse_quarantine(self):
        initial_dir = self._pick_existing_dir(
            self.quarantine_path_var.get(),
            self._state.get("last_quarantine_dir"),
            self.root_path_var.get(),
        )
        path = filedialog.askdirectory(
            title="Select quarantine folder (outside iCloud root)",
            initialdir=initial_dir,
        )
        if path:
            self.quarantine_path_var.set(path)
            self._remember_dir("last_quarantine_dir", path)

    def _open_quarantine(self):
        q = Path(self.quarantine_path_var.get().strip() or "").expanduser()
        if not q:
            return
        try:
            if not q.exists():
                q.mkdir(parents=True, exist_ok=True)
            self._remember_dir("last_quarantine_dir", q)
            self._reveal_in_explorer(str(q), select=False)
        except Exception as exc:
            messagebox.showerror("Open Failed", f"Could not open quarantine folder:\n{exc}")

    def _log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_results(self):
        if self._is_busy():
            messagebox.showinfo("Busy", "Please wait until the current operation is finished.")
            return

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._item_meta.clear()
        self._flagged_files = 0
        self._scanned_files = 0
        self._error_count = 0
        self.progress_var.set(0)
        self.summary_var.set("Ready.")

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_cancel(self):
        if not self._scanning:
            return
        self._cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self._log("Cancel requested. Stopping after current file...")

    def _on_start_scan(self):
        if self._is_busy():
            messagebox.showinfo("Busy", "Another operation is already in progress.")
            return

        root = Path(self.root_path_var.get().strip() or "").expanduser()
        if not root.exists() or not root.is_dir():
            messagebox.showerror("Invalid Folder", "Please choose a valid root folder before scanning.")
            return
        self._remember_dir("last_root_dir", root)

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._item_meta.clear()
        self._reset_stats()

        self._cancel_event.clear()
        self._scanning = True
        self._refresh_action_states()
        self.progress_var.set(0)
        self.summary_var.set("Scanning...")
        self._log(f"Starting scan: {root}")
        self._log(f"Sync issue checks: {', '.join(SYNC_ISSUE_STATUSES)}")

        self._scan_thread = threading.Thread(target=self._scan_worker, args=(root,), daemon=True)
        self._scan_thread.start()
        self.after(100, self._drain_events)

    def _reset_stats(self):
        self._total_files = 0
        self._scanned_files = 0
        self._flagged_files = 0
        self._error_count = 0

    def _scan_worker(self, root: Path):
        started = time.time()
        try:
            total = 0
            for _, _, files in os.walk(root):
                if self._cancel_event.is_set():
                    self._event_queue.put(("canceled", {"phase": "count"}))
                    return
                total += len(files)

            self._event_queue.put(("total", {"total": total}))

            scanned = 0
            flagged = 0
            errors = 0

            for dirpath, _, files in os.walk(root):
                for filename in files:
                    if self._cancel_event.is_set():
                        self._event_queue.put(
                            (
                                "canceled",
                                {
                                    "phase": "scan",
                                    "scanned": scanned,
                                    "flagged": flagged,
                                    "errors": errors,
                                    "elapsed": time.time() - started,
                                },
                            )
                        )
                        return

                    full_path = os.path.join(dirpath, filename)
                    scanned += 1

                    try:
                        file_findings = _analyze_file(full_path)
                        for finding in file_findings:
                            flagged += 1
                            self._event_queue.put(
                                (
                                    "finding",
                                    {
                                        "path": finding.path,
                                        "status": finding.status,
                                        "reason": finding.reason,
                                        "attrs": finding.attrs,
                                    },
                                )
                            )
                    except Exception as exc:
                        errors += 1
                        self._event_queue.put(
                            (
                                "error",
                                {
                                    "path": full_path,
                                    "error": str(exc),
                                },
                            )
                        )

                    if scanned % 50 == 0 or scanned == total:
                        self._event_queue.put(("progress", {"scanned": scanned, "total": total}))

            self._event_queue.put(
                (
                    "done",
                    {
                        "scanned": scanned,
                        "flagged": flagged,
                        "errors": errors,
                        "elapsed": time.time() - started,
                    },
                )
            )
        except Exception as exc:
            self._event_queue.put(
                (
                    "fatal",
                    {
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
            )

    def _drain_events(self):
        had_events = False

        while True:
            try:
                event, data = self._event_queue.get_nowait()
            except queue.Empty:
                break

            had_events = True

            if event == "total":
                self._total_files = int(data["total"])
                if self._total_files <= 0:
                    self.progress_var.set(100)
                    self.summary_var.set("No files found under selected root.")
                else:
                    self.summary_var.set(f"Scanning 0 / {self._total_files} files...")
            elif event == "progress":
                self._scanned_files = int(data["scanned"])
                total = int(data["total"]) if data["total"] else 0
                pct = 100.0 if total <= 0 else (100.0 * self._scanned_files / total)
                self.progress_var.set(pct)
                self.summary_var.set(
                    f"Scanning {self._scanned_files} / {total} files. "
                    f"Flagged: {self._flagged_files} | Errors: {self._error_count}"
                )
            elif event == "finding":
                self._flagged_files += 1
                full_path = str(data["path"])
                p = Path(full_path)

                status = str(data["status"])
                reason = str(data["reason"])
                attrs = str(data.get("attrs", "")).strip()
                display_reason = reason
                if attrs and self._is_dev_or_expert():
                    display_reason = f"{reason} | attrs: {attrs}"

                iid = self.tree.insert(
                    "",
                    "end",
                    values=(status, p.name, str(p.parent), display_reason),
                )
                self._item_meta[iid] = {
                    "path": full_path,
                    "status": status,
                    "reason": reason,
                    "attrs": attrs,
                }
            elif event == "error":
                self._error_count += 1
                self._log(f"Error reading {data['path']}: {data['error']}")
            elif event == "done":
                self._scanned_files = int(data["scanned"])
                self._error_count = int(data["errors"])
                elapsed = float(data["elapsed"])
                self.progress_var.set(100.0)
                self.summary_var.set(
                    f"Done. Scanned: {self._scanned_files}, "
                    f"Flagged: {self._flagged_files}, Errors: {self._error_count}, "
                    f"Time: {elapsed:.1f}s"
                )
                self._log(self.summary_var.get())
                self._finish_scan()
            elif event == "canceled":
                elapsed = float(data.get("elapsed", 0.0))
                self.summary_var.set(
                    f"Canceled. Scanned: {data.get('scanned', self._scanned_files)}, "
                    f"Flagged: {data.get('flagged', self._flagged_files)}, "
                    f"Errors: {data.get('errors', self._error_count)}, "
                    f"Time: {elapsed:.1f}s"
                )
                self._log(self.summary_var.get())
                self._finish_scan()
            elif event == "fatal":
                self._log("Fatal scan error:")
                self._log(str(data.get("error", "Unknown error")))
                self._log(str(data.get("traceback", "")))
                self.summary_var.set("Scan failed. See log for details.")
                self._finish_scan()

        if self._scanning:
            self.after(120, self._drain_events)
        elif had_events:
            # final refresh after state changes
            self.update_idletasks()

    def _finish_scan(self):
        self._scanning = False
        self._refresh_action_states()

    def _on_tree_double_click(self, _event):
        self._open_selected()

    def _open_selected(self):
        selection = self.tree.selection()
        if not selection:
            return

        first_meta = self._item_meta.get(selection[0], {})
        path = first_meta.get("path")
        if not path:
            return

        self._reveal_in_explorer(path, select=True)

    def _on_quarantine_selected(self):
        item_ids = list(self.tree.selection())
        self._quarantine_items(item_ids, mode_label="selected")

    def _on_quarantine_all(self):
        item_ids = list(self.tree.get_children())
        self._quarantine_items(item_ids, mode_label="all flagged")

    def _quarantine_items(self, item_ids: list[str], mode_label: str):
        if self._is_busy():
            messagebox.showinfo("Busy", "Please wait until the current operation is finished.")
            return
        if not item_ids:
            messagebox.showinfo("No Files", "No flagged files are selected.")
            return

        root = Path(self.root_path_var.get().strip() or "").expanduser()
        q_raw = self.quarantine_path_var.get().strip()
        if not q_raw:
            messagebox.showerror("Missing Quarantine Folder", "Please choose a quarantine folder first.")
            return
        q_root = Path(q_raw).expanduser()

        try:
            q_root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Quarantine Error", f"Cannot create quarantine folder:\n{exc}")
            return
        self._remember_dir("last_quarantine_dir", q_root)

        if root.exists() and root.is_dir() and _is_subpath(q_root, root):
            messagebox.showerror(
                "Unsafe Quarantine Location",
                "Quarantine folder must be outside the selected root folder.",
            )
            return

        grouped: dict[str, dict] = {}
        for iid in item_ids:
            meta = self._item_meta.get(iid)
            if not meta:
                continue
            src = str(meta.get("path", ""))
            if not src:
                continue
            bucket = grouped.setdefault(
                src,
                {
                    "path": src,
                    "statuses": set(),
                    "reasons": set(),
                    "item_ids": [],
                },
            )
            bucket["statuses"].add(str(meta.get("status", "")))
            bucket["reasons"].add(str(meta.get("reason", "")))
            bucket["item_ids"].append(iid)

        records = list(grouped.values())
        if not records:
            messagebox.showinfo("No Files", "No valid flagged files were found to quarantine.")
            return

        confirm = messagebox.askyesno(
            "Confirm Quarantine",
            f"This will move {len(records)} unique file(s) ({mode_label}) to:\n\n{q_root}\n\nContinue?",
        )
        if not confirm:
            return

        session_dir = self._make_session_dir(q_root)
        session_dir.mkdir(parents=True, exist_ok=True)

        self._quarantining = True
        self._refresh_action_states()
        self.progress_var.set(0)
        self.summary_var.set(f"Quarantining {len(records)} files...")
        self._log(f"Starting quarantine session: {session_dir}")

        rename_counts_by_folder: dict[str, int] = {}
        manifest_rows: list[dict] = []
        moved = 0
        failed = 0
        missing = 0

        try:
            total = len(records)
            for idx, rec in enumerate(records, start=1):
                src_path = Path(rec["path"])
                statuses = sorted([s for s in rec["statuses"] if s])
                reasons = sorted([r for r in rec["reasons"] if r])

                self.progress_var.set((100.0 * idx) / max(total, 1))
                self.summary_var.set(
                    f"Quarantining {idx}/{total} files. "
                    f"Moved: {moved} | Failed: {failed} | Missing: {missing}"
                )
                self.update_idletasks()

                if not src_path.exists():
                    missing += 1
                    self._log(f"Missing (already moved/deleted): {src_path}")
                    continue

                target_name = self._build_quarantine_filename(src_path, rename_counts_by_folder)
                target_name = _truncate_filename(target_name)
                dst_path = _next_available_path(session_dir, target_name)

                try:
                    shutil.move(_for_fs(src_path.resolve()), _for_fs(dst_path.resolve(strict=False)))
                    moved += 1
                    identity = self._capture_identity(dst_path)

                    manifest_rows.append(
                        {
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "original_path": str(src_path),
                            "quarantine_path": str(dst_path),
                            "original_name": src_path.name,
                            "quarantine_name": dst_path.name,
                            "size_bytes": identity.get("size_bytes", ""),
                            "mtime_ns": identity.get("mtime_ns", ""),
                            "sha256": identity.get("sha256", ""),
                            "statuses": " | ".join(statuses),
                            "reasons": " | ".join(reasons),
                        }
                    )

                    for iid in rec["item_ids"]:
                        if iid in self._item_meta:
                            self.tree.delete(iid)
                            self._item_meta.pop(iid, None)
                except Exception as exc:
                    failed += 1
                    self._log(f"Move failed: {src_path} -> {dst_path} | {exc}")

            manifest_path = self._write_manifest(session_dir, manifest_rows)
            self._write_session_notes(session_dir, manifest_path)

            self._flagged_files = len(self.tree.get_children())
            self.summary_var.set(
                f"Quarantine complete. Moved: {moved}, Failed: {failed}, Missing: {missing}. "
                f"Remaining flagged rows: {self._flagged_files}"
            )
            self._log(self.summary_var.get())
            self._log(f"Manifest: {manifest_path}")

            messagebox.showinfo(
                "Quarantine Complete",
                f"Moved: {moved}\nFailed: {failed}\nMissing: {missing}\n\nManifest:\n{manifest_path}",
            )
        finally:
            self._quarantining = False
            self._refresh_action_states()

    def _make_session_dir(self, quarantine_root: Path) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = quarantine_root / f"weedwhacker_quarantine_{stamp}"
        if not base.exists():
            return base
        idx = 2
        while True:
            candidate = quarantine_root / f"weedwhacker_quarantine_{stamp}_{idx}"
            if not candidate.exists():
                return candidate
            idx += 1

    def _build_quarantine_filename(self, src_path: Path, rename_counts_by_folder: dict[str, int]) -> str:
        should_rename = bool(self.rename_to_folder_var.get())
        if should_rename and self.rename_only_problematic_var.get():
            should_rename = _looks_problematic_for_sync(src_path)

        if not should_rename:
            return _sanitize_filename_component(src_path.name, fallback="RecoveredFile")

        folder_label = _sanitize_filename_component(src_path.parent.name, fallback="RecoveredFile")
        folder_key = str(src_path.parent.resolve())
        ext = src_path.suffix

        next_num = rename_counts_by_folder.get(folder_key, 0) + 1
        rename_counts_by_folder[folder_key] = next_num

        suffix = "" if next_num == 1 else f"_{next_num}"
        max_base_len = max(8, MAX_FILENAME_LEN - len(ext) - len(suffix))
        base = folder_label[:max_base_len].rstrip(" .")
        if not base:
            base = "RecoveredFile"
        return f"{base}{suffix}{ext}"

    def _capture_identity(self, path: Path) -> dict[str, str]:
        """
        Capture stable identity fields for relocation/restore resilience.
        """
        out = {"size_bytes": "", "mtime_ns": "", "sha256": ""}
        try:
            fs_path = _for_fs(path.resolve(strict=False))
            st = os.stat(fs_path)
            out["size_bytes"] = str(st.st_size)
            out["mtime_ns"] = str(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        except Exception:
            return out

        try:
            h = hashlib.sha256()
            with open(_for_fs(path.resolve(strict=False)), "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            out["sha256"] = h.hexdigest()
        except Exception:
            pass
        return out

    def _build_quarantine_search_roots(self, manifest_path: Path) -> list[Path]:
        candidates = [
            manifest_path.parent,
            manifest_path.parent.parent,
            Path(self.quarantine_path_var.get().strip() or "").expanduser(),
            Path(self._state.get("last_quarantine_dir") or "").expanduser(),
        ]
        roots: list[Path] = []
        seen = set()
        for c in candidates:
            try:
                if not c:
                    continue
                rc = c.resolve()
                if str(rc) in seen:
                    continue
                if rc.exists() and rc.is_dir():
                    roots.append(rc)
                    seen.add(str(rc))
            except Exception:
                continue
        return roots

    def _build_quarantine_search_index(self, roots: list[Path]) -> dict:
        """
        Build a lightweight index by size/name and hash on demand.
        """
        size_map: dict[str, list[Path]] = {}
        name_map: dict[str, list[Path]] = {}
        size_by_path: dict[str, str] = {}
        hash_cache: dict[str, str] = {}

        for root in roots:
            for dirpath, _, files in os.walk(root):
                for filename in files:
                    p = Path(dirpath) / filename
                    p_key = str(p.resolve(strict=False))
                    try:
                        st = os.stat(_for_fs(p.resolve(strict=False)))
                        size_str = str(st.st_size)
                    except Exception:
                        continue

                    size_map.setdefault(size_str, []).append(p)
                    name_map.setdefault(filename.lower(), []).append(p)
                    size_by_path[p_key] = size_str

        return {
            "roots": roots,
            "size_map": size_map,
            "name_map": name_map,
            "size_by_path": size_by_path,
            "hash_cache": hash_cache,
        }

    def _sha256_cached(self, path: Path, search_index: dict) -> str:
        key = str(path.resolve(strict=False))
        if key in search_index["hash_cache"]:
            return search_index["hash_cache"][key]
        val = self._capture_identity(path).get("sha256", "")
        search_index["hash_cache"][key] = val
        return val

    def _resolve_quarantine_source(self, row: dict, search_index: dict) -> tuple[Path | None, str]:
        """
        Resolve source file even if quarantine filename/path changed.
        """
        src_raw = (row.get("quarantine_path") or "").strip()
        if src_raw:
            src_path = Path(src_raw)
            if src_path.exists():
                return src_path, "manifest_path_exists"

        size_str = (row.get("size_bytes") or "").strip()
        sha = (row.get("sha256") or "").strip().lower()
        quarantine_name = (row.get("quarantine_name") or "").strip().lower()
        original_name = (row.get("original_name") or "").strip().lower()

        if size_str and sha:
            candidates = search_index["size_map"].get(size_str, [])
            hash_hits: list[Path] = []
            for cand in candidates:
                cand_hash = self._sha256_cached(cand, search_index).lower()
                if cand_hash and cand_hash == sha:
                    hash_hits.append(cand)
            if len(hash_hits) == 1:
                return hash_hits[0], "recovered_by_sha256_size"
            if len(hash_hits) > 1:
                return None, f"ambiguous_sha256_size_matches={len(hash_hits)}"

        if size_str:
            for name in (quarantine_name, original_name):
                if not name:
                    continue
                matches = []
                for cand in search_index["name_map"].get(name, []):
                    p_key = str(cand.resolve(strict=False))
                    if search_index["size_by_path"].get(p_key, "") == size_str:
                        matches.append(cand)
                if len(matches) == 1:
                    return matches[0], "recovered_by_name_size"
                if len(matches) > 1:
                    return None, f"ambiguous_name_size_matches={len(matches)}"

        for name in (quarantine_name, original_name):
            if not name:
                continue
            matches = search_index["name_map"].get(name, [])
            if len(matches) == 1:
                return matches[0], "recovered_by_name_only"
            if len(matches) > 1:
                return None, f"ambiguous_name_only_matches={len(matches)}"

        return None, "not_found_by_identity"

    def _write_csv_rows(self, path: Path, rows: list[dict], fieldnames: list[str]):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _manifest_fieldnames(self, rows: list[dict]) -> list[str]:
        base = [
            "timestamp",
            "original_path",
            "quarantine_path",
            "original_name",
            "quarantine_name",
            "size_bytes",
            "mtime_ns",
            "sha256",
            "statuses",
            "reasons",
        ]
        seen = set(base)
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    base.append(key)
                    seen.add(key)
        return base

    def _write_manifest(self, session_dir: Path, rows: list[dict]) -> Path:
        manifest_path = session_dir / "manifest.csv"
        fieldnames = self._manifest_fieldnames(rows)
        self._write_csv_rows(manifest_path, rows, fieldnames)
        return manifest_path

    def _write_session_notes(self, session_dir: Path, manifest_path: Path):
        notes_path = session_dir / "README.txt"
        lines = [
            "Swiss Army Knife - WeedWhacker Quarantine Session",
            "",
            f"Created: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Manifest: {manifest_path.name}",
            "",
            "What this session contains:",
            "- Files moved out of iCloud root for manual review.",
            "- Filenames may have been replaced using enclosing folder names.",
            "- If multiple files came from the same folder, _2, _3, etc were applied.",
            "",
            "Restore guidance:",
            "- Use manifest.csv to map each quarantined file back to its original path.",
            "- Restore can recover renamed files by using size/hash identity when available.",
            "- If you renamed or moved files in quarantine, run 'Reindex Quarantine' first.",
        ]
        notes_path.write_text("\n".join(lines), encoding="utf-8")

    def _on_reindex_manifest(self):
        if self._is_busy():
            messagebox.showinfo("Busy", "Please wait until the current operation is finished.")
            return

        manifest_file = filedialog.askopenfilename(
            title="Select manifest.csv to reindex",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
            initialdir=self._pick_existing_dir(
                self._state.get("last_manifest_dir"),
                self.quarantine_path_var.get(),
                self._state.get("last_quarantine_dir"),
            ),
        )
        if not manifest_file:
            return

        manifest_path = Path(manifest_file)
        self._remember_dir("last_manifest_dir", manifest_path.parent)
        if not manifest_path.exists():
            messagebox.showerror("Manifest Missing", "Selected manifest file does not exist.")
            return

        try:
            with manifest_path.open("r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception as exc:
            messagebox.showerror("Manifest Error", f"Could not read manifest:\n{exc}")
            return

        if not rows:
            messagebox.showinfo("Nothing To Reindex", "Manifest has no rows.")
            return

        roots = self._build_quarantine_search_roots(manifest_path)
        if not roots:
            messagebox.showerror(
                "Search Roots Missing",
                "No valid quarantine folders were found to search for renamed files.",
            )
            return

        roots_text = "\n".join(str(r) for r in roots)
        confirm = messagebox.askyesno(
            "Confirm Reindex",
            f"Reindex {len(rows)} manifest rows using search roots:\n\n{roots_text}\n\nContinue?",
        )
        if not confirm:
            return

        self._quarantining = True
        self._refresh_action_states()
        self.progress_var.set(0)
        self.summary_var.set(f"Reindexing manifest: {manifest_path.name}")
        self._log(f"Starting manifest reindex: {manifest_path}")

        search_index = self._build_quarantine_search_index(roots)
        updated_rows: list[dict] = []
        report_rows: list[dict] = []
        resolved = 0
        updated_paths = 0
        unresolved = 0
        ambiguous = 0
        total = len(rows)

        try:
            for idx, row in enumerate(rows, start=1):
                self.progress_var.set((100.0 * idx) / max(total, 1))
                self.summary_var.set(
                    f"Reindex {idx}/{total}. Resolved: {resolved} | Updated: {updated_paths} | "
                    f"Unresolved: {unresolved} | Ambiguous: {ambiguous}"
                )
                self.update_idletasks()

                row_copy = dict(row)
                old_path = (row_copy.get("quarantine_path") or "").strip()

                src, resolution = self._resolve_quarantine_source(row_copy, search_index)
                if src is None:
                    if resolution.startswith("ambiguous"):
                        ambiguous += 1
                    else:
                        unresolved += 1
                    report_rows.append(
                        {
                            "row": idx,
                            "status": "unresolved",
                            "resolution": resolution,
                            "old_quarantine_path": old_path,
                            "new_quarantine_path": "",
                            "message": "Could not map row to a unique file",
                        }
                    )
                    updated_rows.append(row_copy)
                    continue

                src_resolved = str(src.resolve(strict=False))
                row_copy["quarantine_path"] = src_resolved
                row_copy["quarantine_name"] = src.name
                ident = self._capture_identity(src)
                row_copy["size_bytes"] = ident.get("size_bytes", row_copy.get("size_bytes", ""))
                row_copy["mtime_ns"] = ident.get("mtime_ns", row_copy.get("mtime_ns", ""))
                row_copy["sha256"] = ident.get("sha256", row_copy.get("sha256", ""))

                resolved += 1
                if old_path != src_resolved:
                    updated_paths += 1

                report_rows.append(
                    {
                        "row": idx,
                        "status": "resolved",
                        "resolution": resolution,
                        "old_quarantine_path": old_path,
                        "new_quarantine_path": src_resolved,
                        "message": "",
                    }
                )
                updated_rows.append(row_copy)

            stamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = manifest_path.with_name(f"{manifest_path.stem}.backup_{stamp}{manifest_path.suffix}")
            shutil.copy2(manifest_path, backup_path)

            fieldnames = self._manifest_fieldnames(updated_rows)
            self._write_csv_rows(manifest_path, updated_rows, fieldnames)

            report_path = manifest_path.with_name(f"reindex_report_{stamp}.csv")
            self._write_csv_rows(
                report_path,
                report_rows,
                ["row", "status", "resolution", "old_quarantine_path", "new_quarantine_path", "message"],
            )

            self.summary_var.set(
                f"Reindex complete. Resolved: {resolved}, Updated paths: {updated_paths}, "
                f"Unresolved: {unresolved}, Ambiguous: {ambiguous}"
            )
            self._log(self.summary_var.get())
            self._log(f"Manifest backup: {backup_path}")
            self._log(f"Reindex report: {report_path}")

            messagebox.showinfo(
                "Reindex Complete",
                f"Resolved: {resolved}\nUpdated paths: {updated_paths}\n"
                f"Unresolved: {unresolved}\nAmbiguous: {ambiguous}\n\n"
                f"Backup:\n{backup_path}\n\nReport:\n{report_path}",
            )
        finally:
            self._quarantining = False
            self._refresh_action_states()

    def _on_restore_manifest(self):
        if self._is_busy():
            messagebox.showinfo("Busy", "Please wait until the current operation is finished.")
            return

        manifest_file = filedialog.askopenfilename(
            title="Select quarantine manifest.csv",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
            initialdir=self._pick_existing_dir(
                self._state.get("last_manifest_dir"),
                self.quarantine_path_var.get(),
                self._state.get("last_quarantine_dir"),
            ),
        )
        if not manifest_file:
            return

        manifest_path = Path(manifest_file)
        self._remember_dir("last_manifest_dir", manifest_path.parent)
        if not manifest_path.exists():
            messagebox.showerror("Manifest Missing", "Selected manifest file does not exist.")
            return

        try:
            with manifest_path.open("r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception as exc:
            messagebox.showerror("Manifest Error", f"Could not read manifest:\n{exc}")
            return

        required = {"original_path", "quarantine_path", "original_name", "quarantine_name"}
        if not rows:
            messagebox.showinfo("Nothing To Restore", "Manifest has no rows.")
            return
        missing_cols = [col for col in required if col not in rows[0]]
        if missing_cols:
            messagebox.showerror(
                "Manifest Error",
                "Manifest is missing required columns:\n" + ", ".join(missing_cols),
            )
            return

        restore_mode = "original filenames" if self.restore_use_original_name_var.get() else "quarantine filenames"
        confirm = messagebox.askyesno(
            "Confirm Restore",
            f"Restore {len(rows)} entries using {restore_mode}?\n\nManifest:\n{manifest_path}",
        )
        if not confirm:
            return

        self._quarantining = True
        self._refresh_action_states()
        self.progress_var.set(0)
        self.summary_var.set(f"Restoring from manifest: {manifest_path.name}")
        self._log(f"Starting restore from manifest: {manifest_path}")

        roots = self._build_quarantine_search_roots(manifest_path)
        if not roots:
            self._quarantining = False
            self._refresh_action_states()
            messagebox.showerror(
                "Search Roots Missing",
                "No valid quarantine folders were found to search for renamed files.",
            )
            return
        self._log("Quarantine search roots:")
        for r in roots:
            self._log(f"  - {r}")
        search_index = self._build_quarantine_search_index(roots)

        restored = 0
        skipped = 0
        failed = 0
        recovered = 0
        unresolved = 0
        ambiguous = 0
        report_rows: list[dict] = []
        total = len(rows)

        try:
            for idx, row in enumerate(rows, start=1):
                original_raw = (row.get("original_path") or "").strip()
                original_name = (row.get("original_name") or "").strip()
                quarantine_name = (row.get("quarantine_name") or "").strip()

                self.progress_var.set((100.0 * idx) / max(total, 1))
                self.summary_var.set(
                    f"Restoring {idx}/{total}. Restored: {restored} | Recovered: {recovered} | "
                    f"Skipped: {skipped} | Failed: {failed}"
                )
                self.update_idletasks()

                src, resolution = self._resolve_quarantine_source(row, search_index)
                if src is None:
                    if resolution.startswith("ambiguous"):
                        ambiguous += 1
                    else:
                        unresolved += 1
                    skipped += 1
                    report_rows.append(
                        {
                            "status": "skipped_unresolved_source",
                            "resolution": resolution,
                            "source": (row.get("quarantine_path") or "").strip(),
                            "destination": "",
                            "message": "Could not locate a unique quarantine file by path/identity",
                        }
                    )
                    continue

                if resolution != "manifest_path_exists":
                    recovered += 1

                if not original_raw:
                    skipped += 1
                    report_rows.append(
                        {
                            "status": "skipped_missing_original_path",
                            "resolution": resolution,
                            "source": str(src),
                            "destination": "",
                            "message": "Original path missing in manifest",
                        }
                    )
                    continue

                original = Path(original_raw)
                dest_dir = original.parent
                use_original = bool(self.restore_use_original_name_var.get())
                preferred_name = original_name if use_original else quarantine_name
                if not preferred_name:
                    preferred_name = original.name if use_original else src.name

                safe_name = _truncate_filename(_sanitize_filename_component(preferred_name, "RestoredFile"))
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_path = _next_available_path(dest_dir, safe_name)

                try:
                    shutil.move(_for_fs(src.resolve()), _for_fs(dest_path.resolve(strict=False)))
                    restored += 1
                    report_rows.append(
                        {
                            "status": "restored",
                            "resolution": resolution,
                            "source": str(src),
                            "destination": str(dest_path),
                            "message": "",
                        }
                    )
                except Exception as exc:
                    failed += 1
                    report_rows.append(
                        {
                            "status": "failed",
                            "resolution": resolution,
                            "source": str(src),
                            "destination": str(dest_path),
                            "message": str(exc),
                        }
                    )
                    self._log(f"Restore failed: {src} -> {dest_path} | {exc}")

            report_path = manifest_path.with_name(
                f"restore_report_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            )
            with report_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["status", "resolution", "source", "destination", "message"],
                )
                writer.writeheader()
                for row in report_rows:
                    writer.writerow(row)

            self.summary_var.set(
                f"Restore complete. Restored: {restored}, Recovered by identity: {recovered}, "
                f"Skipped: {skipped}, Failed: {failed}, Unresolved: {unresolved}, Ambiguous: {ambiguous}"
            )
            self._log(self.summary_var.get())
            self._log(f"Restore report: {report_path}")
            messagebox.showinfo(
                "Restore Complete",
                f"Restored: {restored}\nRecovered by identity: {recovered}\nSkipped: {skipped}\n"
                f"Failed: {failed}\nUnresolved: {unresolved}\nAmbiguous: {ambiguous}\n\n"
                f"Report:\n{report_path}",
            )
        finally:
            self._quarantining = False
            self._refresh_action_states()

    def _reveal_in_explorer(self, path: str, select: bool = True):
        try:
            p = Path(path)
            if os.name == "nt":
                if select:
                    # For files: open Explorer with the file selected.
                    if not p.exists():
                        os.startfile(str(p.parent))
                        return
                    if p.is_dir():
                        os.startfile(str(p))
                        return

                    target = os.path.normpath(str(p))
                    # Try multiple invocation styles; different Windows setups vary.
                    commands = [
                        ["explorer.exe", f'/select,"{target}"'],
                        ["explorer.exe", "/select,", target],
                        ["explorer.exe", f"/select,{target}"],
                    ]
                    for cmd in commands:
                        try:
                            subprocess.Popen(cmd)
                            return
                        except Exception:
                            continue
                    # Fallback to opening parent directory.
                    os.startfile(str(p.parent))
                    return

                # For folders: open directly at the target folder.
                open_target = p if p.is_dir() else p.parent
                os.startfile(str(open_target))
            elif sys.platform == "darwin":
                if select and p.exists() and p.is_file():
                    subprocess.Popen(["open", "-R", str(p)])
                else:
                    open_target = p if p.is_dir() else p.parent
                    subprocess.Popen(["open", str(open_target)])
            else:
                open_target = p.parent if select and p.is_file() else (p if p.is_dir() else p.parent)
                subprocess.Popen(["xdg-open", str(open_target)])
        except Exception as exc:
            messagebox.showerror("Open Failed", f"Could not open file location:\n{exc}")

    def _is_dev_or_expert(self) -> bool:
        if not self.app:
            return False

        try:
            dev = bool(getattr(self.app, "dev_mode_enabled").get())
        except Exception:
            dev = False

        try:
            expert = bool(getattr(self.app, "expert_mode_enabled").get())
        except Exception:
            expert = False

        return dev or expert


def get_module(parent, app=None):
    return WeedWhackerFrame(parent, app=app)
