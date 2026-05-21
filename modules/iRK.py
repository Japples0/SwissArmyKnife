import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


@dataclass(frozen=True)
class IRKRule:
    keyword: str
    issue: str
    severity: str
    actions: tuple[str, ...]


DEFAULT_IRK_RULES: tuple[IRKRule, ...] = (
    IRKRule(
        keyword="excluded from sync",
        issue="File excluded from iCloud sync",
        severity="medium",
        actions=(
            "Inspect filename/path length and remove unsupported characters.",
            "Move temporary lock files or partial downloads outside the iCloud root.",
            "Re-run a WeedWhacker scan and quarantine problematic files if needed.",
        ),
    ),
    IRKRule(
        keyword="ckerror",
        issue="CloudKit error response",
        severity="high",
        actions=(
            "Restart iCloud services and retry after a short delay.",
            "Confirm Apple ID session is healthy and not requiring re-authentication.",
            "If repeated, sign out/in to iCloud and trigger a clean sync cycle.",
        ),
    ),
    IRKRule(
        keyword="quota",
        issue="iCloud storage quota pressure",
        severity="high",
        actions=(
            "Check available iCloud storage and free space if near capacity.",
            "Pause large sync workloads until quota is stabilized.",
            "Resume sync and verify backlog clears without new quota errors.",
        ),
    ),
    IRKRule(
        keyword="network",
        issue="Connectivity-related sync instability",
        severity="medium",
        actions=(
            "Validate internet stability and avoid captive/filtered networks.",
            "Retry sync on a stable network and observe if errors stop recurring.",
            "If persistent, reset network stack and retry iCloud login.",
        ),
    ),
    IRKRule(
        keyword="auth",
        issue="Authentication/session failure",
        severity="high",
        actions=(
            "Re-authenticate the Apple ID account used for iCloud.",
            "Confirm system clock/timezone are correct to avoid token issues.",
            "Trigger a manual iCloud sync refresh after successful sign-in.",
        ),
    ),
    IRKRule(
        keyword="throttle",
        issue="Service-side throttling",
        severity="medium",
        actions=(
            "Back off high-frequency file operations for 5-15 minutes.",
            "Batch sync-heavy changes instead of constant incremental updates.",
            "Retry and monitor for repeated throttling events.",
        ),
    ),
)


LOG_LIKE_SUFFIXES = {".log", ".txt", ".trace", ".err", ".out", ".json"}


def _match_rule(line: str) -> IRKRule | None:
    low = line.lower()
    for rule in DEFAULT_IRK_RULES:
        if rule.keyword in low:
            return rule
    return None


class ICloudRepairKitFrame(ttk.Frame):
    def __init__(self, parent, app=None):
        super().__init__(parent, padding=10)
        self.app = app

        self._state = self._load_state()
        self.log_path_var = tk.StringVar(value="")
        self.poll_interval_var = tk.DoubleVar(value=float(self._state.get("poll_interval", 1.0)))
        self.scan_existing_var = tk.BooleanVar(value=bool(self._state.get("scan_existing", False)))
        self.recursive_scan_var = tk.BooleanVar(value=bool(self._state.get("recursive_scan", True)))
        self.summary_var = tk.StringVar(value="Ready.")

        self._event_queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._watch_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._watching = False
        self._positions: dict[str, int] = {}
        self._missing_reported: set[str] = set()
        self._empty_folder_reported: set[str] = set()
        self._events_meta: dict[str, dict] = {}
        self._events_count = 0

        self._build_ui()
        self._set_details("Select an event to view recommended actions.")
        for path in self._state.get("log_paths", []):
            self._add_watched_path(path, save=False)
        self._save_state()
        self._refresh_action_states()

    def notify_mode_change(self):
        # This module currently does not vary behavior by app mode.
        pass

    def _build_ui(self):
        header = ttk.LabelFrame(self, text="iRK (iCloud Repair Kit)", padding=10)
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="Log Source:").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(header, textvariable=self.log_path_var)
        entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        entry.bind("<Return>", lambda _e: self._add_typed_path())

        self.browse_btn = ttk.Button(header, text="Add File(s)...", command=self._browse_add_files)
        self.browse_btn.grid(row=0, column=2, sticky="ew")

        self.add_folder_btn = ttk.Button(header, text="Add Folder...", command=self._browse_add_folder)
        self.add_folder_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0))

        self.add_typed_btn = ttk.Button(header, text="Add Typed Path", command=self._add_typed_path)
        self.add_typed_btn.grid(row=0, column=4, sticky="ew", padx=(8, 0))

        watched = ttk.LabelFrame(header, text="Watched Logs", padding=8)
        watched.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        watched.columnconfigure(0, weight=1)

        self.log_list = tk.Listbox(watched, height=5, selectmode="extended", exportselection=False)
        self.log_list.grid(row=0, column=0, sticky="ew")

        self.remove_btn = ttk.Button(watched, text="Remove Selected", command=self._remove_selected_paths)
        self.remove_btn.grid(row=0, column=1, padx=(8, 0), sticky="n")

        controls = ttk.Frame(header)
        controls.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        controls.columnconfigure(6, weight=1)

        self.start_btn = ttk.Button(controls, text="Start Listener", command=self._start_listener)
        self.start_btn.grid(row=0, column=0, padx=(0, 8))

        self.stop_btn = ttk.Button(controls, text="Stop Listener", command=self._stop_listener)
        self.stop_btn.grid(row=0, column=1, padx=(0, 8))

        self.clear_events_btn = ttk.Button(controls, text="Clear Events", command=self._clear_events)
        self.clear_events_btn.grid(row=0, column=2, padx=(0, 16))

        ttk.Label(controls, text="Poll (sec):").grid(row=0, column=3, sticky="w")
        self.poll_spin = ttk.Spinbox(
            controls,
            from_=0.2,
            to=10.0,
            increment=0.2,
            textvariable=self.poll_interval_var,
            width=7,
            command=self._save_state,
        )
        self.poll_spin.grid(row=0, column=4, padx=(6, 16))

        self.scan_existing_chk = ttk.Checkbutton(
            controls,
            text="Read existing log contents on start",
            variable=self.scan_existing_var,
            command=self._save_state,
        )
        self.scan_existing_chk.grid(row=0, column=5, sticky="w")

        self.recursive_scan_chk = ttk.Checkbutton(
            controls,
            text="Recursive folder scan (includes CloudKit subfolders)",
            variable=self.recursive_scan_var,
            command=self._save_state,
        )
        self.recursive_scan_chk.grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        summary = ttk.Label(self, textvariable=self.summary_var, anchor="w")
        summary.pack(fill="x", pady=(8, 8))

        rules_frame = ttk.LabelFrame(self, text="Keyword Action Rules", padding=8)
        rules_frame.pack(fill="x")
        rules_frame.columnconfigure(0, weight=1)

        rule_cols = ("keyword", "severity", "issue", "actions")
        self.rule_tree = ttk.Treeview(rules_frame, columns=rule_cols, show="headings", height=6)
        self.rule_tree.heading("keyword", text="Keyword")
        self.rule_tree.heading("severity", text="Severity")
        self.rule_tree.heading("issue", text="Issue")
        self.rule_tree.heading("actions", text="Recommended Action (Summary)")
        self.rule_tree.column("keyword", width=150, anchor="w")
        self.rule_tree.column("severity", width=90, anchor="center")
        self.rule_tree.column("issue", width=280, anchor="w")
        self.rule_tree.column("actions", width=430, anchor="w")
        self.rule_tree.grid(row=0, column=0, sticky="ew")

        for rule in DEFAULT_IRK_RULES:
            summary_action = rule.actions[0] if rule.actions else ""
            self.rule_tree.insert("", "end", values=(rule.keyword, rule.severity.upper(), rule.issue, summary_action))

        events_frame = ttk.LabelFrame(self, text="Detected iCloud Log Events", padding=8)
        events_frame.pack(fill="both", expand=True, pady=(8, 0))
        events_frame.columnconfigure(0, weight=1)
        events_frame.rowconfigure(0, weight=1)

        event_cols = ("time", "severity", "keyword", "file", "excerpt")
        self.event_tree = ttk.Treeview(events_frame, columns=event_cols, show="headings", selectmode="browse")
        self.event_tree.heading("time", text="Time")
        self.event_tree.heading("severity", text="Severity")
        self.event_tree.heading("keyword", text="Keyword")
        self.event_tree.heading("file", text="Log File")
        self.event_tree.heading("excerpt", text="Matched Log Line")
        self.event_tree.column("time", width=150, anchor="w")
        self.event_tree.column("severity", width=90, anchor="center")
        self.event_tree.column("keyword", width=150, anchor="w")
        self.event_tree.column("file", width=180, anchor="w")
        self.event_tree.column("excerpt", width=480, anchor="w")

        ybar = ttk.Scrollbar(events_frame, orient="vertical", command=self.event_tree.yview)
        self.event_tree.configure(yscrollcommand=ybar.set)
        self.event_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        self.event_tree.bind("<<TreeviewSelect>>", self._on_event_selected)

        details_frame = ttk.LabelFrame(self, text="Recommended Actions", padding=8)
        details_frame.pack(fill="both", expand=False, pady=(8, 0))
        self.details_text = tk.Text(details_frame, height=7, wrap="word")
        self.details_text.pack(fill="both", expand=True)
        self.details_text.configure(state="disabled")

    def _state_file(self) -> Path:
        return Path(__file__).resolve().with_name("irk_state.json")

    def _load_state(self) -> dict:
        try:
            p = self._state_file()
            if p.exists():
                raw = p.read_text(encoding="utf-8")
                import json

                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_state(self):
        paths = [self.log_list.get(i) for i in range(self.log_list.size())] if hasattr(self, "log_list") else []
        payload = {
            "log_paths": paths,
            "poll_interval": float(self.poll_interval_var.get() or 1.0),
            "scan_existing": bool(self.scan_existing_var.get()),
            "recursive_scan": bool(self.recursive_scan_var.get()),
        }
        try:
            import json

            p = self._state_file()
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def _add_watched_path(self, path: str, save: bool = True):
        text = (path or "").strip()
        if not text:
            return
        normalized = str(Path(text).expanduser())
        existing = {self.log_list.get(i) for i in range(self.log_list.size())}
        if normalized not in existing:
            self.log_list.insert("end", normalized)
            if save:
                self._save_state()

    def _browse_add_files(self):
        selected = filedialog.askopenfilenames(
            title="Select iCloud log files",
            filetypes=[("Log/Text", "*.log *.txt"), ("All Files", "*.*")],
        )
        for path in selected:
            self._add_watched_path(path, save=False)
        self._save_state()

    def _browse_add_folder(self):
        selected = filedialog.askdirectory(title="Select iCloud logs folder")
        if selected:
            self._add_watched_path(selected, save=True)

    def _add_typed_path(self):
        typed = self.log_path_var.get().strip()
        if not typed:
            return
        self._add_watched_path(typed, save=True)
        self.log_path_var.set("")

    def _remove_selected_paths(self):
        selection = list(self.log_list.curselection())
        if not selection:
            return
        for idx in reversed(selection):
            self.log_list.delete(idx)
        self._save_state()

    def _clear_events(self):
        for iid in self.event_tree.get_children():
            self.event_tree.delete(iid)
        self._events_meta.clear()
        self._events_count = 0
        self.summary_var.set("Event list cleared.")
        self._set_details("Select an event to view recommended actions.")

    def _start_listener(self):
        if self._watching:
            return

        paths = [self.log_list.get(i) for i in range(self.log_list.size())]
        if not paths:
            messagebox.showerror("No Logs Selected", "Add one or more log files before starting listener.")
            return

        poll_interval = max(0.2, float(self.poll_interval_var.get() or 1.0))
        self.poll_interval_var.set(poll_interval)
        self._save_state()

        self._positions.clear()
        self._missing_reported.clear()
        self._empty_folder_reported.clear()
        read_existing = bool(self.scan_existing_var.get())
        recursive_scan = bool(self.recursive_scan_var.get())

        self._stop_event.clear()
        self._watching = True
        self.summary_var.set(f"Listening to {len(paths)} log source(s)...")
        self._refresh_action_states()

        self._watch_thread = threading.Thread(
            target=self._watch_worker,
            args=(paths, poll_interval, read_existing, recursive_scan),
            daemon=True,
        )
        self._watch_thread.start()
        self.after(120, self._drain_events)

    def _stop_listener(self):
        if not self._watching:
            return
        self._stop_event.set()
        self.summary_var.set("Stopping listener...")
        self._refresh_action_states()

    def _looks_like_log_file(self, path: Path) -> bool:
        name_low = path.name.lower()
        stem_low = path.stem.lower()
        suffix_low = path.suffix.lower()
        if suffix_low in LOG_LIKE_SUFFIXES:
            return True
        if "cloudkit" in name_low:
            return True
        if "log" in stem_low:
            return True
        return False

    def _iter_source_files(self, source: str, recursive_scan: bool) -> tuple[list[str], str | None]:
        src_path = Path(source)
        if not src_path.exists():
            return [], "missing"
        if src_path.is_file():
            return [str(src_path)], None
        if not src_path.is_dir():
            return [], "unsupported"

        files: list[str] = []
        iterator = src_path.rglob("*") if recursive_scan else src_path.glob("*")
        for p in iterator:
            if not p.is_file():
                continue
            if self._looks_like_log_file(p):
                files.append(str(p))
        return sorted(set(files)), None

    def _watch_worker(
        self,
        paths: list[str],
        poll_interval: float,
        read_existing: bool,
        recursive_scan: bool,
    ):
        while not self._stop_event.is_set():
            for source in paths:
                if self._stop_event.is_set():
                    break

                files, source_error = self._iter_source_files(source, recursive_scan)
                if source_error == "missing":
                    if source not in self._missing_reported:
                        self._missing_reported.add(source)
                        self._event_queue.put(("notice", {"message": f"Waiting for source to appear: {source}"}))
                    continue
                if source_error == "unsupported":
                    self._event_queue.put(("error", {"message": f"Unsupported source type: {source}"}))
                    continue

                if source in self._missing_reported:
                    self._missing_reported.remove(source)
                    self._event_queue.put(("notice", {"message": f"Source detected: {source}"}))

                if not files:
                    if source not in self._empty_folder_reported:
                        self._empty_folder_reported.add(source)
                        self._event_queue.put(
                            (
                                "notice",
                                {"message": f"No log-like files found yet in folder: {source}"},
                            )
                        )
                    continue
                if source in self._empty_folder_reported:
                    self._empty_folder_reported.remove(source)
                    self._event_queue.put(("notice", {"message": f"Log files detected in folder: {source}"}))

                for file_path in files:
                    if self._stop_event.is_set():
                        break

                    p = Path(file_path)
                    try:
                        size = p.stat().st_size
                        if file_path not in self._positions:
                            self._positions[file_path] = 0 if read_existing else size

                        pos = self._positions.get(file_path, 0)
                        if size < pos:
                            pos = 0
                            self._event_queue.put(
                                (
                                    "notice",
                                    {"message": f"Log rotated/truncated, restarting read: {file_path}"},
                                )
                            )

                        if size <= pos:
                            self._positions[file_path] = pos
                            continue

                        with p.open("r", encoding="utf-8", errors="ignore") as f:
                            f.seek(pos)
                            block = f.read()
                            new_pos = f.tell()
                        self._positions[file_path] = new_pos

                        if not block:
                            continue

                        for line in block.splitlines():
                            clean = line.strip()
                            if not clean:
                                continue

                            rule = _match_rule(clean)
                            if not rule:
                                continue

                            self._event_queue.put(
                                (
                                    "finding",
                                    {
                                        "path": file_path,
                                        "line": clean,
                                        "keyword": rule.keyword,
                                        "issue": rule.issue,
                                        "severity": rule.severity,
                                        "actions": list(rule.actions),
                                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    },
                                )
                            )
                    except Exception as exc:
                        self._event_queue.put(
                            (
                                "error",
                                {"message": f"Listener error for {file_path}: {exc}"},
                            )
                        )

            time.sleep(poll_interval)

        self._event_queue.put(("stopped", {}))

    def _drain_events(self):
        while True:
            try:
                event, data = self._event_queue.get_nowait()
            except queue.Empty:
                break

            if event == "finding":
                self._events_count += 1
                full_path = str(data["path"])
                excerpt = str(data["line"])
                short_excerpt = excerpt if len(excerpt) <= 140 else (excerpt[:137] + "...")
                iid = self.event_tree.insert(
                    "",
                    "end",
                    values=(
                        str(data["timestamp"]),
                        str(data["severity"]).upper(),
                        str(data["keyword"]),
                        Path(full_path).name,
                        short_excerpt,
                    ),
                )
                self._events_meta[iid] = {
                    "timestamp": str(data["timestamp"]),
                    "severity": str(data["severity"]),
                    "keyword": str(data["keyword"]),
                    "issue": str(data["issue"]),
                    "path": full_path,
                    "line": excerpt,
                    "actions": list(data.get("actions", [])),
                }
                self.summary_var.set(
                    f"Listening... Events flagged: {self._events_count} | "
                    f"Watched logs: {self.log_list.size()}"
                )
            elif event == "notice":
                self.summary_var.set(str(data.get("message", "")))
            elif event == "error":
                self.summary_var.set(str(data.get("message", "Listener error")))
            elif event == "stopped":
                self._watching = False
                self.summary_var.set("Listener stopped.")
                self._refresh_action_states()

        if self._watching:
            self.after(150, self._drain_events)
        else:
            self._refresh_action_states()

    def _on_event_selected(self, _event=None):
        selection = self.event_tree.selection()
        if not selection:
            return
        meta = self._events_meta.get(selection[0])
        if not meta:
            return

        lines = [
            f"Time: {meta['timestamp']}",
            f"Severity: {str(meta['severity']).upper()}",
            f"Keyword: {meta['keyword']}",
            f"Issue: {meta['issue']}",
            f"Log File: {meta['path']}",
            "",
            "Matched line:",
            meta["line"],
            "",
            "Recommended actions:",
        ]
        actions = meta.get("actions", [])
        if actions:
            for idx, action in enumerate(actions, start=1):
                lines.append(f"{idx}. {action}")
        else:
            lines.append("1. Review this log line and re-run WeedWhacker scan.")

        self._set_details("\n".join(lines))

    def _set_details(self, text: str):
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", text)
        self.details_text.configure(state="disabled")

    def _refresh_action_states(self):
        if self._watching:
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.browse_btn.configure(state="disabled")
            self.add_folder_btn.configure(state="disabled")
            self.add_typed_btn.configure(state="disabled")
            self.remove_btn.configure(state="disabled")
            self.clear_events_btn.configure(state="disabled")
            self.poll_spin.configure(state="disabled")
            self.scan_existing_chk.configure(state="disabled")
            self.recursive_scan_chk.configure(state="disabled")
        else:
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.browse_btn.configure(state="normal")
            self.add_folder_btn.configure(state="normal")
            self.add_typed_btn.configure(state="normal")
            self.remove_btn.configure(state="normal")
            self.clear_events_btn.configure(state="normal")
            self.poll_spin.configure(state="normal")
            self.scan_existing_chk.configure(state="normal")
            self.recursive_scan_chk.configure(state="normal")

    def destroy(self):
        self._stop_event.set()
        self._watching = False
        super().destroy()


def get_module(parent, app=None):
    return ICloudRepairKitFrame(parent, app=app)
