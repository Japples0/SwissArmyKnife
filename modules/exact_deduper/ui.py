import tkinter as tk
import threading
import queue
import os
import sys
import subprocess
import shutil
import re
import json
from pathlib import Path
from tkinter import ttk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
from modules.exact_deduper.scanner import scan_for_duplicates


def get_module(parent, app):
    return ExactDeduperFrame(parent, app)


class ExactDeduperFrame(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

        self._loaded_state = self._load_state()
        self._suspend_state_save = False

        self.event_queue = queue.Queue()
        self.scan_thread = None
        self.scanning = False
        self.selected_path = None
        self.suspect_path = None
        self.cancel_requested = False
        self.results = []
        self._folder_label_to_path = {}

        self._build_ui()
        self.after(120, self._show_restore_prompt_on_load)


    def _choose_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.selected_path = os.path.abspath(path)
            self.path_label.config(text=self.selected_path)
            self._update_start_button_state()
            self._save_state()

    def _choose_suspect_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.suspect_path = os.path.abspath(path)
            self.suspect_label.config(text=self.suspect_path)
            self.log(f"Suspect folder set: {self.suspect_path}")
            self._save_state()

    def _clear_suspect_folder(self):
        self.suspect_path = None
        self.suspect_label.config(text="Not set")
        self.log("Suspect folder cleared.")
        self._save_state()

    def _build_ui(self):
        # ===== Top Controls =====
        controls = ttk.Frame(self)
        controls.pack(fill="x", padx=10, pady=8)

        self.path_label = ttk.Label(controls, text="No folder selected")
        self.path_label.pack(side="left", padx=10)

        browse_btn = ttk.Button(
            controls,
            text="Browse",
            command=self._choose_folder
        )
        browse_btn.pack(side="left")

        ttk.Label(controls, text="Mode:").pack(side="left", padx=(10, 4))
        self.scan_mode_var = tk.StringVar(value="exact")
        self.scan_mode_combo = ttk.Combobox(
            controls,
            state="readonly",
            textvariable=self.scan_mode_var,
            values=["exact", "relative"],
            width=10,
        )
        self.scan_mode_combo.pack(side="left")
        self.scan_mode_combo.bind("<<ComboboxSelected>>", self._on_scan_mode_changed)

        ttk.Label(controls, text="Min Relative %:").pack(side="left", padx=(10, 4))
        self.relative_threshold_var = tk.DoubleVar(value=85.0)
        self.relative_threshold_spin = ttk.Spinbox(
            controls,
            from_=50,
            to=100,
            increment=1,
            width=5,
            textvariable=self.relative_threshold_var,
        )
        self.relative_threshold_spin.pack(side="left")
        self.relative_threshold_var.trace_add("write", self._on_relative_threshold_change)

        self.start_btn = ttk.Button(
            controls,
            text="Run Exact Deduper",
            state="disabled",
            command=self._on_start
        )
        self.start_btn.pack(side="left")

        self.progress = ttk.Progressbar(
            controls,
            orient="horizontal",
            mode="determinate",
            length=300
        )
        self.progress.pack(side="left", padx=10)

        self.cancel_btn = ttk.Button(
            controls,
            text="Cancel",
            state="disabled",
            command=self._on_cancel
        )
        self.cancel_btn.pack(side="left", padx=(5, 0))

        suspect_controls = ttk.Frame(self)
        suspect_controls.pack(fill="x", padx=10, pady=(0, 4))

        ttk.Label(suspect_controls, text="Suspect folder (delete from first):").pack(side="left")

        self.suspect_label = ttk.Label(suspect_controls, text="Not set")
        self.suspect_label.pack(side="left", padx=(8, 10))

        ttk.Button(
            suspect_controls,
            text="Set Suspect Folder",
            command=self._choose_suspect_folder
        ).pack(side="left")

        ttk.Button(
            suspect_controls,
            text="Clear",
            command=self._clear_suspect_folder
        ).pack(side="left", padx=(6, 0))

        # Build expert UI container (packed when Expert Mode is enabled)
        self._build_expert_ui()

        # ===== Output + Preview Split =====
        top_split = ttk.PanedWindow(self, orient="horizontal")
        top_split.pack(fill="both", expand=True, padx=10, pady=8)

        # Left: Live Output
        log_frame = ttk.LabelFrame(top_split, text="Live Output")
        top_split.add(log_frame, weight=2)

        self.log_text = tk.Text(
            log_frame,
            height=15,
            wrap="word",
            state="disabled"
        )
        self.log_text.pack(fill="both", expand=True)

        # Right: Preview
        preview_frame = ttk.LabelFrame(top_split, text="Dual Preview")
        top_split.add(preview_frame, weight=2)

        # Canvas to render images; default black background
        self.preview_canvas = tk.Canvas(preview_frame, background="black", height=220)
        self.preview_canvas.pack(fill="both", expand=True)

        # Info about the selected file
        self.preview_info = ttk.Label(preview_frame, text="Select a file to preview", wraplength=380, justify="left")
        self.preview_info.pack(fill="x", pady=(6, 0))

        # Actions for the preview + deletion controls (moved here)
        btn_row = ttk.Frame(preview_frame)
        btn_row.pack(fill="x", pady=(4, 6))
        ttk.Button(btn_row, text="Open", command=self._open_selected_file).pack(side="left")
        ttk.Button(btn_row, text="Clear", command=self._clear_preview).pack(side="left", padx=6)

        # Delete actions live next to the viewer controls for quicker access
        self.delete_btn = ttk.Button(
            btn_row,
            text="Delete Duplicates (Keep One)",
            state="disabled",
            command=self._on_delete_duplicates
        )
        self.delete_btn.pack(side="left", padx=(16, 0))

        self.delete_all_btn = ttk.Button(
            btn_row,
            text="Delete All Duplicates (Keep One per Group)",
            state="disabled",
            command=self._on_delete_all_duplicates
        )
        self.delete_all_btn.pack(side="left", padx=(10, 0))

        # Keep state for preview
        self._preview_photo = None
        self._preview_path = None

        # Re-render preview on resize
        self.preview_canvas.bind("<Configure>", self._on_preview_resize)

        viewer = ttk.PanedWindow(self, orient="horizontal")
        viewer.pack(fill="both", expand=True, padx=10, pady=5)

        # Left: group list
        left = ttk.Frame(viewer)
        viewer.add(left, weight=1)

        # Header with title + sort dropdown
        header = ttk.Frame(left)
        header.pack(fill="x")
        ttk.Label(header, text="Duplicate Groups").pack(side="left")

        self.sort_var = tk.StringVar(value="Largest->Smallest")
        self.sort_combo = ttk.Combobox(
            header,
            state="readonly",
            textvariable=self.sort_var,
            values=[
                "Name",
                "Numerical",
                "Largest->Smallest",
                "Smallest->Largest",
                "By Origin Folder",
                "By Duplicate Folder",
            ],
            width=22,
        )
        self.sort_combo.pack(side="right")
        self.sort_combo.bind("<<ComboboxSelected>>", self._on_sort_changed)

        self.group_list = tk.Listbox(left, height=8, exportselection=False)
        self.group_list.pack(fill="both", expand=True)
        self.group_list.bind("<<ListboxSelect>>", self._on_group_select)

        # Right: file list
        right = ttk.Frame(viewer)
        viewer.add(right, weight=2)

        ttk.Label(right, text="Files in Group (Select the one to keep)").pack(anchor="w")

        self.file_list = tk.Listbox(right, exportselection=False)
        self.file_list.pack(fill="both", expand=True)
        self.file_list.bind("<<ListboxSelect>>", self._on_file_select)

        self._on_scan_mode_changed()

    # ===== Expert Mode UI (hidden unless Expert Mode is enabled in main app) =====
    def _build_expert_ui(self):
        # Build but do not pack; notify_mode_change will toggle visibility
        self.expert_frame = ttk.LabelFrame(self, text="Expert Options")
        container = ttk.Frame(self.expert_frame)
        container.pack(fill="x", padx=8, pady=6)

        # Controls row
        ctrl_row = ttk.Frame(container)
        ctrl_row.pack(fill="x", pady=(0, 6))
        ttk.Label(ctrl_row, text="After selecting a parent folder, load subfolders to include or ignore.").pack(side="left")
        ttk.Label(ctrl_row, text="Recursion depth:").pack(side="right", padx=(8, 4))
        self.subfolder_depth_var = tk.IntVar(value=1)
        self.subfolder_depth_spin = ttk.Spinbox(
            ctrl_row,
            from_=1,
            to=20,
            width=4,
            textvariable=self.subfolder_depth_var
        )
        self.subfolder_depth_spin.pack(side="right")
        ttk.Button(ctrl_row, text="Load Subfolders", command=self._load_subfolders).pack(side="right")
        self.subfolder_depth_var.trace_add("write", self._on_subfolder_depth_change)

        # Two-column include/ignore lists with move buttons
        lists = ttk.Frame(container)
        lists.pack(fill="x")
        lists.columnconfigure(0, weight=1)
        lists.columnconfigure(1, weight=0)
        lists.columnconfigure(2, weight=1)

        # Include list
        left_col = ttk.Frame(lists)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(left_col, text="Search Folders (Include)").pack(anchor="w")
        self.include_listbox = tk.Listbox(left_col, height=6, exportselection=False)
        self.include_listbox.pack(fill="both", expand=True)

        # Move buttons
        mid_col = ttk.Frame(lists)
        mid_col.grid(row=0, column=1, sticky="ns")
        ttk.Button(mid_col, text=">> Ignore", command=self._move_to_ignore).pack(pady=4)
        ttk.Button(mid_col, text="<< Include", command=self._move_to_include).pack(pady=4)

        # Ignore list
        right_col = ttk.Frame(lists)
        right_col.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        ttk.Label(right_col, text="Ignore Folders").pack(anchor="w")
        self.ignore_listbox = tk.Listbox(right_col, height=6, exportselection=False)
        self.ignore_listbox.pack(fill="both", expand=True)

    def notify_mode_change(self):
        """Called by main app when a mode is toggled; show/hide Expert panel."""
        enabled = False
        try:
            if getattr(self.app, "expert_mode_enabled", None):
                enabled = bool(self.app.expert_mode_enabled.get())
        except Exception:
            enabled = False

        # Toggle visibility
        try:
            if enabled:
                # Only pack once; refresh lists if path already chosen
                if not self.expert_frame.winfo_ismapped():
                    self.expert_frame.pack(fill="x", padx=10, pady=(0, 8))
            else:
                if self.expert_frame.winfo_ismapped():
                    self.expert_frame.pack_forget()
        except Exception:
            pass

    # -------- Persistence helpers --------
    def _state_file(self) -> Path:
        return Path(__file__).resolve().with_name("exact_deduper_state.json")

    def _load_state(self) -> dict:
        try:
            path = self._state_file()
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception:
            pass
        return {}

    def _save_state(self):
        if self._suspend_state_save:
            return

        include_paths, ignore_paths = self._capture_filter_paths()
        try:
            depth = int(self.subfolder_depth_var.get()) if hasattr(self, "subfolder_depth_var") else 1
        except Exception:
            depth = 1
        payload = {
            "selected_path": self._normalize_path_value(self.selected_path),
            "suspect_path": self._normalize_path_value(self.suspect_path),
            "sort_choice": self.sort_var.get() if hasattr(self, "sort_var") else "Largest->Smallest",
            "scan_mode": self._selected_scan_mode() if hasattr(self, "scan_mode_var") else "exact",
            "min_relative_match_pct": self._safe_relative_threshold(),
            "subfolder_depth": max(1, depth),
            "include_dirs": include_paths,
            "ignore_dirs": ignore_paths,
        }

        try:
            state_path = self._state_file()
            tmp_path = state_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(state_path)
        except Exception:
            # Persistence should never interrupt dedupe operations.
            pass

    def _normalize_path_value(self, value: str | None) -> str:
        text = (value or "").strip()
        if not text:
            return ""
        return os.path.abspath(os.path.expanduser(text))

    def _normalize_existing_dir(self, value: str | None) -> str | None:
        normalized = self._normalize_path_value(value)
        if normalized and os.path.isdir(normalized):
            return normalized
        return None

    def _capture_filter_paths(self) -> tuple[list[str], list[str]]:
        include_labels = list(self.include_listbox.get(0, "end")) if hasattr(self, "include_listbox") else []
        ignore_labels = list(self.ignore_listbox.get(0, "end")) if hasattr(self, "ignore_listbox") else []

        include_paths = []
        ignore_paths = []
        seen_include = set()
        seen_ignore = set()

        for label in include_labels:
            resolved = self._resolve_folder_label_to_path(label)
            if resolved and resolved not in seen_include:
                seen_include.add(resolved)
                include_paths.append(resolved)

        for label in ignore_labels:
            resolved = self._resolve_folder_label_to_path(label)
            if resolved and resolved not in seen_ignore:
                seen_ignore.add(resolved)
                ignore_paths.append(resolved)

        return include_paths, ignore_paths

    def _build_folder_label(self, folder_path: str) -> str:
        abs_path = os.path.abspath(folder_path)
        base = self.selected_path if self.selected_path and os.path.isdir(self.selected_path) else None
        if base:
            try:
                rel = os.path.relpath(abs_path, base)
                if rel != "." and not rel.startswith(".."):
                    depth = rel.count(os.sep) + 1
                    indent = "  " * (depth - 1)
                    return f"{indent}{rel}"
            except Exception:
                pass
        return abs_path

    def _restore_filter_labels_from_paths(self, include_dirs: list[str], ignore_dirs: list[str]):
        include_labels = []
        ignore_labels = []
        new_map = dict(self._folder_label_to_path)
        seen_include = set()
        seen_ignore = set()

        for folder in include_dirs or []:
            normalized = self._normalize_existing_dir(folder)
            if not normalized or normalized in seen_include:
                continue
            seen_include.add(normalized)
            label = self._build_folder_label(normalized)
            include_labels.append(label)
            new_map[label] = normalized

        for folder in ignore_dirs or []:
            normalized = self._normalize_existing_dir(folder)
            if not normalized or normalized in seen_ignore:
                continue
            seen_ignore.add(normalized)
            label = self._build_folder_label(normalized)
            ignore_labels.append(label)
            new_map[label] = normalized

        if ignore_labels:
            ignore_set = set(ignore_labels)
            include_labels = [label for label in include_labels if label not in ignore_set]

        self._folder_label_to_path = new_map
        self._refresh_expert_lists(include_labels, ignore_labels, save_state=False)

    def _show_restore_prompt_on_load(self):
        if not isinstance(self._loaded_state, dict) or not self._loaded_state:
            return

        try:
            saved_depth = int(self._loaded_state.get("subfolder_depth", 1) or 1)
        except Exception:
            saved_depth = 1
        try:
            saved_relative = float(self._loaded_state.get("min_relative_match_pct", 85.0) or 85.0)
        except Exception:
            saved_relative = 85.0

        has_snapshot = any([
            bool((self._loaded_state.get("selected_path") or "").strip()),
            bool((self._loaded_state.get("suspect_path") or "").strip()),
            bool(self._loaded_state.get("include_dirs")),
            bool(self._loaded_state.get("ignore_dirs")),
            (self._loaded_state.get("scan_mode") or "exact") != "exact",
            saved_relative != 85.0,
            saved_depth != 1,
            (self._loaded_state.get("sort_choice") or "Largest->Smallest") != "Largest->Smallest",
        ])
        if not has_snapshot:
            return

        include_count = len(self._loaded_state.get("include_dirs") or [])
        ignore_count = len(self._loaded_state.get("ignore_dirs") or [])
        selected_path = self._loaded_state.get("selected_path") or "(not set)"
        summary = (
            "Restore your previous Exact Deduper setup?\n\n"
            f"Folder: {selected_path}\n"
            f"Expert include folders: {include_count}\n"
            f"Expert ignore folders: {ignore_count}\n\n"
            "If expert filters are restored, Expert Mode will be turned on automatically."
        )
        restore = messagebox.askyesno(
            "Restore Previous Session",
            summary,
            icon="question",
        )
        if restore:
            self._apply_session_state(self._loaded_state)
            self.log("Previous Exact Deduper session restored.")
        else:
            self.log("Restore skipped. Starting with current defaults.")

    def _apply_session_state(self, state: dict):
        self._suspend_state_save = True
        try:
            restored_path = self._normalize_existing_dir(state.get("selected_path"))
            self.selected_path = restored_path
            self.path_label.config(text=restored_path or "No folder selected")

            restored_suspect = self._normalize_existing_dir(state.get("suspect_path"))
            self.suspect_path = restored_suspect
            self.suspect_label.config(text=restored_suspect or "Not set")

            try:
                depth = int(state.get("subfolder_depth", 1) or 1)
            except Exception:
                depth = 1
            self.subfolder_depth_var.set(max(1, depth))

            saved_sort = str(state.get("sort_choice") or "Largest->Smallest")
            sort_values = tuple(self.sort_combo.cget("values"))
            self.sort_var.set(saved_sort if saved_sort in sort_values else "Largest->Smallest")

            saved_mode = str(state.get("scan_mode") or "exact").strip().lower()
            self.scan_mode_var.set("relative" if saved_mode == "relative" else "exact")

            try:
                saved_threshold = float(state.get("min_relative_match_pct", 85.0) or 85.0)
            except Exception:
                saved_threshold = 85.0
            self.relative_threshold_var.set(max(50.0, min(100.0, saved_threshold)))
            self._on_scan_mode_changed()

            include_dirs = state.get("include_dirs") or []
            ignore_dirs = state.get("ignore_dirs") or []
            self._restore_filter_labels_from_paths(include_dirs, ignore_dirs)
        finally:
            self._suspend_state_save = False

        self._update_start_button_state()
        self.notify_mode_change()
        self._auto_enable_expert_mode_if_filters_present()
        self._save_state()

    def _auto_enable_expert_mode_if_filters_present(self):
        include_paths, ignore_paths = self._capture_filter_paths()
        if not include_paths and not ignore_paths:
            return

        try:
            expert_var = getattr(self.app, "expert_mode_enabled", None)
            if expert_var and not bool(expert_var.get()):
                expert_var.set(True)
                self.notify_mode_change()
                self.log("Expert Mode enabled automatically to apply restored include/ignore filters.")
        except Exception:
            pass

    def _on_sort_changed(self, _event=None):
        self._populate_groups()
        self._save_state()

    def _on_scan_mode_changed(self, _event=None):
        mode = self._selected_scan_mode()
        if mode == "relative":
            self.start_btn.config(text="Run Relative Deduper")
            self.relative_threshold_spin.config(state="normal")
        else:
            self.start_btn.config(text="Run Exact Deduper")
            self.relative_threshold_spin.config(state="disabled")
        self._save_state()

    def _on_relative_threshold_change(self, *_args):
        self._save_state()

    def _on_subfolder_depth_change(self, *_args):
        self._save_state()

    def _selected_scan_mode(self) -> str:
        try:
            mode = str(self.scan_mode_var.get() or "exact").strip().lower()
        except Exception:
            mode = "exact"
        return "relative" if mode == "relative" else "exact"

    def _safe_relative_threshold(self) -> float:
        try:
            value = float(self.relative_threshold_var.get())
        except Exception:
            value = 85.0
        return max(50.0, min(100.0, value))

    # -------- Expert helpers --------
    def _refresh_expert_lists(self, include_items: list[str], ignore_items: list[str], save_state: bool = True):
        self.include_listbox.delete(0, "end")
        self.ignore_listbox.delete(0, "end")
        for p in include_items:
            self.include_listbox.insert("end", p)
        for p in ignore_items:
            self.ignore_listbox.insert("end", p)
        if save_state:
            self._save_state()

    def _load_subfolders(self):
        """Populate include list with subfolders up to a configured recursion depth."""
        base = getattr(self, "selected_path", None)
        if not base or not os.path.isdir(base):
            messagebox.showinfo("Expert Options", "Please select a valid parent directory first.")
            return

        try:
            try:
                max_depth = int(self.subfolder_depth_var.get())
            except Exception:
                max_depth = 1
            max_depth = max(1, max_depth)

            labels_with_paths = []
            for root, dirnames, _files in os.walk(base, topdown=True, followlinks=False):
                dirnames.sort(key=lambda s: s.lower())

                rel_root = os.path.relpath(root, base)
                current_depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1

                if current_depth >= max_depth:
                    dirnames[:] = []
                    continue

                for dirname in dirnames:
                    abs_path = os.path.join(root, dirname)
                    rel_path = os.path.relpath(abs_path, base)
                    depth = rel_path.count(os.sep) + 1
                    indent = "  " * (depth - 1)
                    label = f"{indent}{rel_path}"
                    labels_with_paths.append((label, abs_path))

            merged_map = dict(self._folder_label_to_path)
            merged_map.update({label: abs_path for label, abs_path in labels_with_paths})
            self._folder_label_to_path = merged_map
            sub_labels = [label for label, _abs_path in labels_with_paths]
        except Exception as ex:
            self.log(f"Failed to enumerate subfolders: {ex}")
            sub_labels = []

        # Load subfolders into include; keep current ignores intact (intersection removed from include)
        current_ignores = list(self.ignore_listbox.get(0, "end")) if hasattr(self, "ignore_listbox") else []
        include = [label for label in sub_labels if label not in set(current_ignores)]
        self._refresh_expert_lists(include, current_ignores)
        self.log(f"Loaded {len(sub_labels)} subfolder(s) up to depth {max_depth}.")

    def _move_to_ignore(self):
        sel = list(self.include_listbox.curselection())
        if not sel:
            return
        items = [self.include_listbox.get(i) for i in sel]
        existing_ign = set(self.ignore_listbox.get(0, "end"))
        for idx in reversed(sel):
            try:
                self.include_listbox.delete(idx)
            except Exception:
                pass
        for it in items:
            if it not in existing_ign:
                self.ignore_listbox.insert("end", it)
        self._save_state()

    def _move_to_include(self):
        sel = list(self.ignore_listbox.curselection())
        if not sel:
            return
        items = [self.ignore_listbox.get(i) for i in sel]
        existing_inc = set(self.include_listbox.get(0, "end"))
        for idx in reversed(sel):
            try:
                self.ignore_listbox.delete(idx)
            except Exception:
                pass
        for it in items:
            if it not in existing_inc:
                self.include_listbox.insert("end", it)
        self._save_state()

    def _get_expert_filters(self) -> tuple[list[str] | None, list[str] | None]:
        """Return (include_dirs, ignore_dirs) according to Expert Mode UI."""
        try:
            if getattr(self.app, "expert_mode_enabled", None) and self.app.expert_mode_enabled.get():
                include_items = list(self.include_listbox.get(0, "end")) if hasattr(self, "include_listbox") else []
                ignore_items = list(self.ignore_listbox.get(0, "end")) if hasattr(self, "ignore_listbox") else []
                # Resolve labels to absolute paths
                include_items = [self._resolve_folder_label_to_path(lbl) for lbl in include_items]
                ignore_items = [self._resolve_folder_label_to_path(lbl) for lbl in ignore_items]
                include_items = [p for p in include_items if p]
                ignore_items = [p for p in ignore_items if p]
                return (include_items or None, ignore_items or None)
        except Exception:
            pass
        return (None, None)

    def _resolve_folder_label_to_path(self, label: str) -> str | None:
        mapped = self._folder_label_to_path.get(label)
        if mapped and os.path.isdir(mapped):
            return os.path.abspath(mapped)

        # Fallback: strip indentation and resolve relative to selected root.
        cleaned = label.lstrip()
        if os.path.isabs(cleaned):
            return os.path.abspath(cleaned) if os.path.isdir(cleaned) else None

        base = self.selected_path if self.selected_path and os.path.isdir(self.selected_path) else None
        if not base:
            return None
        candidate = os.path.abspath(os.path.join(base, cleaned))
        return candidate if os.path.isdir(candidate) else None

    # -------- Sorting helpers --------
    def _apply_sort(self):
        """Sort self.results in-place based on dropdown choice."""
        if not self.results or not hasattr(self, "sort_var"):
            return
        choice = self.sort_var.get()

        # Precompute totals for size sorts
        def total_size(g: dict) -> int:
            try:
                return sum(int(getattr(entry, "size", 0) or 0) for entry in g.get("files", []))
            except Exception:
                return int(g.get("size", 0)) * max(1, len(g.get("files", [])))

        # Key based on choice
        if choice == "Largest->Smallest":
            self.results.sort(key=total_size, reverse=True)
        elif choice == "Smallest->Largest":
            self.results.sort(key=total_size)
        elif choice == "Name":
            def k(g):
                files = g.get("files", [])
                if not files:
                    return ""
                keep_idx = self._pick_preferred_keeper_index(files)
                return os.path.splitext(os.path.basename(str(files[keep_idx].path)))[0].lower()
            self.results.sort(key=k)
        elif choice == "Numerical":
            num_re = re.compile(r"(\d+)")
            def k(g):
                files = g.get("files", [])
                if not files:
                    return (float("inf"), "")
                keep_idx = self._pick_preferred_keeper_index(files)
                name = os.path.splitext(os.path.basename(str(files[keep_idx].path)))[0]
                m = num_re.search(name)
                num = int(m.group(1)) if m else float("inf")
                return (num, name.lower())
            self.results.sort(key=k)
        elif choice == "By Origin Folder":
            def k(g):
                files = g.get("files", [])
                if not files:
                    return ""
                keep_idx = self._pick_preferred_keeper_index(files)
                return str(os.path.dirname(str(files[keep_idx].path))).lower()
            self.results.sort(key=k)
        elif choice == "By Duplicate Folder":
            def k(g):
                files = g.get("files", [])
                if len(files) < 2:
                    # fallback to keeper parent
                    parent = os.path.dirname(str(files[0].path)) if files else ""
                    return parent.lower()
                keep_idx = self._pick_preferred_keeper_index(files)
                for i, fe in enumerate(files):
                    if i != keep_idx:
                        return str(os.path.dirname(str(fe.path))).lower()
                return str(os.path.dirname(str(files[0].path))).lower()
            self.results.sort(key=k)
        else:
            # Default: leave order as-is
            pass

    def _sort_key_for_group(self, group: dict):
        # Not used directly; placeholder for potential future external sorting needs.
        return 0

    def _on_cancel(self):
        if not self.scanning:
            return

        self.cancel_requested = True
        self.log("Cancel requested...")
        self.log("Waiting for scanner to stop...")
        self.progress["value"] = 0

    def _update_start_button_state(self):
        """Enable Start only when not scanning and a valid folder is selected."""
        has_valid_path = bool(self.selected_path and os.path.isdir(self.selected_path))
        if self.scanning or not has_valid_path:
            self.start_btn.config(state="disabled")
        else:
            self.start_btn.config(state="normal")

    def _scanner_callback(self, event, data):
        self.event_queue.put((event, data))

    def _poll_events(self):
        try:
            while True:
                event, data = self.event_queue.get_nowait()
                self._handle_event(event, data)
        except queue.Empty:
            pass

        if self.scanning:
            self.after(50, self._poll_events)

    def _handle_event(self, event, data):
        if event == "scan_start":
            mode = str(data.get("mode") or "exact").lower()
            self.log(f"Scanning ({mode}): {data['root']}")

        elif event == "file_discovered":
            self.log(f"Files found: {data['count']}")

        elif event == "hash_start":
            self.progress["maximum"] = data["total"]
            self.progress["value"] = 0

        elif event == "hash_progress":
            self.progress["value"] = data["current"]

        elif event == "compare_start":
            self.progress["maximum"] = data["total"]
            self.progress["value"] = 0

        elif event == "compare_progress":
            self.progress["value"] = data["current"]

        elif event == "scan_complete":
            self._populate_groups()
            self.progress["value"] = 0

            mode = str(data.get("mode") or "exact").lower()
            self.log(
                f"{mode.title()} scan complete - {data['groups']} duplicate groups "
                f"({data['files_scanned']} files scanned)"
            )

        elif event == "thread_done":
            self.scanning = False
            self._update_start_button_state()
            self.cancel_btn.config(state="disabled")
            self.log("Scanner thread finished.")

        elif event == "scan_cancelled":
            self.log("Scan cancelled by user.")

    # ========================
    # UI Helper Methods
    # ========================

    def log(self, message: str):
        """Append text to the log box safely."""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_progress(self, value: int, maximum: int | None = None):
        """Update progress bar."""
        if maximum is not None:
            self.progress["maximum"] = maximum
        self.progress["value"] = value
        self.update_idletasks()

    def destroy(self):
        try:
            self._save_state()
        except Exception:
            pass
        super().destroy()

    # ========================
    # Button Handlers
    # ========================

    def _on_start(self):
        if self.scanning:
            return

        if not self.selected_path or not os.path.isdir(self.selected_path):
            self.log("Please select a valid folder first.")
            self._update_start_button_state()
            return

        selected_mode = self._selected_scan_mode()
        relative_threshold = self._safe_relative_threshold()

        self.scanning = True
        if selected_mode == "relative":
            self.log(f"Starting relative dedupe scan (minimum relative match: {relative_threshold:.1f}%)...")
        else:
            self.log("Starting exact dedupe scan...")
        self.cancel_requested = False
        self._update_start_button_state()
        self.cancel_btn.config(state="normal")
        self.progress["value"] = 0
        self.progress["maximum"] = 1
        self._save_state()

        def worker():
            try:
                include_dirs, ignore_dirs = self._get_expert_filters()
                self.results = scan_for_duplicates(
                    self.selected_path,
                    mode=selected_mode,
                    progress_callback=self._scanner_callback,
                    cancel_check=lambda: self.cancel_requested,
                    include_dirs=include_dirs,
                    ignore_dirs=ignore_dirs,
                    min_relative_match_pct=relative_threshold,
                )
            finally:
                self.event_queue.put(("thread_done", {}))

        self.scan_thread = threading.Thread(
            target=worker,
            daemon=True
        )
        self.scan_thread.start()

        self._poll_events()

    def _on_group_select(self, event):
        selection = self.group_list.curselection()
        if not selection:
            return

        index = selection[0]
        group = self.results[index]

        self.file_list.delete(0, "end")
        for entry in group["files"]:
            self.file_list.insert("end", str(entry.path))

        # Enable the delete button only if there are duplicates to delete
        if hasattr(self, "delete_btn"):
            is_exact = str(group.get("mode") or "exact").lower() == "exact"
            self.delete_btn.config(state="normal" if is_exact and len(group["files"]) > 1 else "disabled")

        # Auto-select the first file to ensure a preview is shown (helps video groups)
        if self.file_list.size() > 0:
            try:
                self.file_list.selection_clear(0, "end")
                self.file_list.selection_set(0)
                self._on_file_select(None)
            except Exception:
                pass

        # Ensure the group list retains focus so its selection stays visibly highlighted
        try:
            self.group_list.focus_set()
        except Exception:
            pass

    def _populate_groups(self):
        self.group_list.delete(0, "end")

        if not self.results:
            self.group_list.insert("end", "No duplicate groups found")
            self.file_list.delete(0, "end")
            self._clear_preview()
            if hasattr(self, "delete_btn"):
                self.delete_btn.config(state="disabled")
            if hasattr(self, "delete_all_btn"):
                self.delete_all_btn.config(state="disabled")
            return

        # Sort according to the selected filter
        try:
            self._apply_sort()
        except Exception:
            pass

        for i, group in enumerate(self.results):
            file_count = len(group["files"])
            size_bytes = sum(int(getattr(entry, "size", 0) or 0) for entry in group["files"])

            # Human-readable size
            if size_bytes >= 1024 ** 3:
                size_str = f"{size_bytes / (1024 ** 3):.2f} GB"
            elif size_bytes >= 1024 ** 2:
                size_str = f"{size_bytes / (1024 ** 2):.1f} MB"
            else:
                size_str = f"{size_bytes / 1024:.1f} KB"

            label = f"Group {i + 1} - {file_count} files - {size_str}"
            if str(group.get("mode") or "exact").lower() == "relative":
                try:
                    rel_pct = float(group.get("relative_match_pct", 0.0))
                except Exception:
                    rel_pct = 0.0
                label += f" - Relative match {rel_pct:.1f}%"
            self.group_list.insert("end", label)

        # Enable/disable the global delete button depending on whether duplicates exist
        has_dupes = any(
            len(g.get("files", [])) > 1 and str(g.get("mode") or "exact").lower() == "exact"
            for g in self.results
        )
        if hasattr(self, "delete_all_btn"):
            self.delete_all_btn.config(state="normal" if has_dupes else "disabled")

        # Fallback behavior: always select the first visible group after repopulating.
        # This keeps the workflow one-click after scan/sort changes.
        if self.group_list.size() > 0:
            try:
                self.group_list.selection_clear(0, "end")
                self.group_list.selection_set(0)
                self.group_list.activate(0)
                self.group_list.see(0)
                self._on_group_select(None)
            except Exception:
                pass

    def _on_delete_duplicates(self):
        """Delete all files in the selected group except one keeper.
        If a file is selected in the right-hand list, that file is kept.
        Otherwise, the first file in the group is kept.
        """
        selection = self.group_list.curselection()
        if not selection:
            self.log("Select a duplicate group first.")
            return

        index = selection[0]
        group = self.results[index]
        if str(group.get("mode") or "exact").lower() != "exact":
            self.log("Relative match groups are preview-only in this first implementation. Run Exact mode to enable deletion.")
            return
        files = group["files"]

        if len(files) <= 1:
            self.log("This group no longer has duplicates to delete.")
            if hasattr(self, "delete_btn"):
                self.delete_btn.config(state="disabled")
            return

        # Determine the keeper based on selection in file_list (if any)
        keeper_idx = None
        file_sel = self.file_list.curselection()
        if file_sel:
            keeper_path = self.file_list.get(file_sel[0])
            for i, entry in enumerate(files):
                if str(entry.path) == keeper_path:
                    keeper_idx = i
                    break
        if keeper_idx is None:
            keeper_idx = self._pick_preferred_keeper_index(files, prefer_outside_suspect=True)

        keeper = files[keeper_idx]
        to_delete = [e for i, e in enumerate(files) if i != keeper_idx]

        total_bytes = sum(e.size for e in to_delete)
        pretty_total = self._pretty_size(total_bytes)

        confirm = messagebox.askyesno(
            "Confirm Delete",
            "Delete {n} duplicate file(s) in this group and keep:\n\n{keeper}\n\n"
            "Total to remove: {size}\n\n"
            "This will permanently delete files. Continue?".format(
                n=len(to_delete), keeper=keeper.path, size=pretty_total
            )
        )
        if not confirm:
            return

        deleted = 0
        errors = 0
        for e in to_delete:
            try:
                os.remove(e.path)
                deleted += 1
                self.log(f"Deleted: {e.path}")
            except Exception as ex:
                errors += 1
                self.log(f"Failed to delete {e.path}: {ex}")

        # Update results: keep only the keeper file in this group
        group["files"] = [keeper]

        # If the group is no longer a duplicate set, remove it
        if len(group["files"]) <= 1:
            try:
                del self.results[index]
            except Exception:
                # Fallback in case index changed; filter out singleton groups
                self.results = [g for g in self.results if len(g.get("files", [])) > 1]

        # Refresh UI
        self._populate_groups()
        self.file_list.delete(0, "end")
        if hasattr(self, "delete_btn"):
            self.delete_btn.config(state="disabled")

        self.log(f"Deletion complete. Removed {deleted} file(s){f', {errors} error(s)' if errors else ''}.")

    def _on_delete_all_duplicates(self):
        """Delete all duplicate files across all groups, keeping one per group."""
        if not self.results:
            self.log("No results to process.")
            return

        groups = [
            g for g in self.results
            if len(g.get("files", [])) > 1 and str(g.get("mode") or "exact").lower() == "exact"
        ]
        if not groups:
            self.log("No exact duplicate groups to delete.")
            return

        # Plan deletions: choose the cleanest name per group to keep
        deletion_plan = []
        for g in groups:
            files = g["files"]
            if len(files) > 1:
                k_idx = self._pick_preferred_keeper_index(files, prefer_outside_suspect=True)
                deletion_plan.extend([e for i, e in enumerate(files) if i != k_idx])

        if not deletion_plan:
            self.log("Nothing to delete - already at one per group.")
            return

        total_bytes = sum(e.size for e in deletion_plan)
        pretty_total = self._pretty_size(total_bytes)
        suspect_note = ""
        if self.suspect_path and os.path.isdir(self.suspect_path):
            suspect_note = (
                "Priority mode is active: files inside the suspect folder will be deleted first when possible.\n"
                f"Suspect folder: {self.suspect_path}\n\n"
            )

        confirm = messagebox.askyesno(
            "Confirm Delete All Duplicates",
            "{suspect}Delete {n} duplicate file(s) across {g} group(s), keeping one per group.\n\n"
            "Total to remove: {size}\n\n"
            "This will permanently delete files. Continue?".format(
                suspect=suspect_note, n=len(deletion_plan), g=len(groups), size=pretty_total
            )
        )
        if not confirm:
            return

        deleted = 0
        errors = 0
        for e in deletion_plan:
            try:
                os.remove(e.path)
                deleted += 1
                self.log(f"Deleted: {e.path}")
            except Exception as ex:
                errors += 1
                self.log(f"Failed to delete {e.path}: {ex}")

        # Refresh in-memory results to reflect the on-disk state
        pruned = []
        for g in self.results:
            # Keep only files that still exist
            g["files"] = [fe for fe in g.get("files", []) if os.path.exists(fe.path)]
            if len(g["files"]) > 1:
                pruned.append(g)
        self.results = pruned

        # Refresh UI
        self._populate_groups()
        self.file_list.delete(0, "end")
        if hasattr(self, "delete_btn"):
            self.delete_btn.config(state="disabled")

        self.log(f"Global deletion complete. Removed {deleted} file(s){f', {errors} error(s)' if errors else ''}.")

    def _pretty_size(self, size_bytes: int) -> str:
        if size_bytes >= 1024 ** 3:
            return f"{size_bytes / (1024 ** 3):.2f} GB"
        elif size_bytes >= 1024 ** 2:
            return f"{size_bytes / (1024 ** 2):.1f} MB"
        elif size_bytes >= 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes} bytes"

    def _on_file_select(self, event):
        selection = self.file_list.curselection()
        if not selection:
            self._clear_preview()
            return
        path = self.file_list.get(selection[0])
        self._render_preview_for_path(path)

    def _render_preview_for_path(self, path: str):
        """Attempt to render a preview for images; for videos render a placeholder thumbnail with runtime."""
        self._preview_path = path
        # Current canvas size
        canvas_w = max(50, self.preview_canvas.winfo_width() or self.preview_canvas.winfo_reqwidth())
        canvas_h = max(50, self.preview_canvas.winfo_height() or self.preview_canvas.winfo_reqheight())

        # Try image preview first
        try:
            img = Image.open(path)
            img_copy = img.copy()
            img_copy.thumbnail((canvas_w - 10, canvas_h - 10))
            self._preview_photo = ImageTk.PhotoImage(img_copy)
            self.preview_canvas.delete("all")
            self.preview_canvas.create_image(canvas_w // 2, canvas_h // 2, image=self._preview_photo, anchor="center")

            try:
                size = os.path.getsize(path)
                info = f"{path}\n{self._pretty_size(size)}"
            except Exception:
                info = str(path)
            self.preview_info.config(text=info)
            return
        except Exception:
            # Not an image or failed to open; check for video next
            pass

        # If it's a video, create a placeholder thumbnail and show runtime if available
        if self._is_video(path):
            placeholder = self._create_video_placeholder(path, max(80, canvas_w - 10), max(60, canvas_h - 10))
            self._preview_photo = ImageTk.PhotoImage(placeholder)
            self.preview_canvas.delete("all")
            self.preview_canvas.create_image(canvas_w // 2, canvas_h // 2, image=self._preview_photo, anchor="center")

            try:
                size = os.path.getsize(path)
                size_str = self._pretty_size(size)
            except Exception:
                size_str = "Unknown size"

            dur = self._probe_video_duration(path)
            dur_str = self._format_duration(dur) if dur else "Unknown runtime"
            self.preview_info.config(text=f"{path}\n{size_str}\nRuntime: {dur_str}")
            return

        # Info-only fallback for other file types
        try:
                size = os.path.getsize(path)
                size_str = self._pretty_size(size)
        except Exception:
            size_str = "Unknown size"
            self.preview_canvas.delete("all")
            self._preview_photo = None
            self.preview_info.config(text=f"{path}\n{size_str}\n(No preview available)")

    def _on_preview_resize(self, event):
        """Re-render the current preview when the canvas size changes."""
        if getattr(self, "_preview_path", None):
            self._render_preview_for_path(self._preview_path)

    def _clear_preview(self):
        self.preview_canvas.delete("all")
        self.preview_info.config(text="Select a file to preview")
        self._preview_photo = None
        self._preview_path = None

    def _open_selected_file(self):
        """Open the selected file with the default application."""
        selection = self.file_list.curselection()
        if not selection:
            return
        path = self.file_list.get(selection[0])
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as ex:
            self.log(f"Failed to open file: {ex}")

            # ========================
            # Preview helpers
            # ========================

    def _is_video(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        return ext in {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"}

    def _probe_video_duration(self, path: str) -> float | None:
        """Return duration in seconds if ffprobe is available; otherwise None."""
        # Use ffprobe if present in PATH
        try:
            # If ffprobe not found, this will raise FileNotFoundError
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True,
                text=True,
                check=False
            )
            out = proc.stdout.strip()
            if out:
                try:
                    val = float(out)
                    return val if val > 0 else None
                except ValueError:
                    return None
        except FileNotFoundError:
            return None
        except Exception:
            return None
        return None

    def _format_duration(self, seconds: float | None) -> str:
        if not seconds or seconds <= 0:
            return "0:00"
        total = int(round(seconds))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h > 0:
            return f"{h:d}:{m:02d}:{s:02d}"
        return f"{m:d}:{s:02d}"

    def _create_video_placeholder(self, path: str, max_w: int, max_h: int) -> Image.Image:
        """Create a simple thumbnail-like placeholder with a play icon and file initials."""
        w = max(60, max_w)
        h = max(40, max_h)
        img = Image.new("RGB", (w, h), (24, 24, 24))

        try:
            from PIL import ImageDraw, ImageFont
            draw = ImageDraw.Draw(img)

            # Draw a subtle border
            draw.rectangle([(0, 0), (w - 1, h - 1)], outline=(70, 70, 70))

            # Draw play triangle
            tri_w = max(20, w // 6)
            tri_h = max(20, h // 4)
            cx, cy = w // 2, h // 2
            triangle = [(cx - tri_w // 2, cy - tri_h // 2),
                        (cx - tri_w // 2, cy + tri_h // 2),
                        (cx + tri_w // 2, cy)]
            draw.polygon(triangle, fill=(240, 240, 240))

            # Draw filename initials in corner
            base = os.path.splitext(os.path.basename(path))[0]
            initials = (base[:2] or "V").upper()
            # Font is optional; fallback to default
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
            draw.text((6, 6), initials, fill=(200, 200, 200), font=font)
        except Exception:
            # If PIL drawing fails for any reason, return the plain background
            pass

        return img

            # ========================
            # Keeper selection heuristic
            # ========================

    def _keeper_score(self, filepath: str) -> float:
        """A lower score is better (cleaner name)."""
        name = os.path.splitext(os.path.basename(filepath))[0]
        low = name.lower()
        score = 0.0

        # Penalize common duplicate markers
        if re.search(r"\(\d+\)$", low):  # e.g., "file (1)"
            score += 5.0
        if re.search(r"[ _-]\d+$", low):  # e.g., "file_2" or "file - 2"
            score += 4.0
        if "copy" in low:  # e.g., "file - copy", "file copy"
            score += 6.0 + low.count("copy") * 1.0

        # Slightly penalize extra punctuation (messier names)
        punct = sum(1 for ch in name if not ch.isalnum() and ch not in (" ", "_", "-"))
        score += punct * 0.2

        # Slightly prefer shorter base names
        score += len(name) * 0.01

        return score

    def _is_in_suspect_folder(self, filepath: str) -> bool:
        """True when filepath is inside the selected suspect folder."""
        if not self.suspect_path:
            return False
        try:
            file_abs = os.path.abspath(filepath)
            suspect_abs = os.path.abspath(self.suspect_path)
            common = os.path.commonpath([file_abs, suspect_abs])
            return os.path.normcase(common) == os.path.normcase(suspect_abs)
        except Exception:
            return False

    def _pick_preferred_keeper_index(self, files, prefer_outside_suspect: bool = False) -> int:
        """Return the index of the best keeper file based on configured preference."""
        best_idx = 0
        best_tuple = (float("inf"), float("inf"), float("inf"), 0)  # (suspect_rank, score, name_len, index)
        for i, entry in enumerate(files):
            path = str(entry.path)
            base = os.path.splitext(os.path.basename(path))[0]
            s = self._keeper_score(path)
            suspect_rank = 0
            if prefer_outside_suspect and self._is_in_suspect_folder(path):
                suspect_rank = 1
            tup = (suspect_rank, s, len(base), i)
            if tup < best_tuple:
                best_tuple = tup
                best_idx = i
        return best_idx

