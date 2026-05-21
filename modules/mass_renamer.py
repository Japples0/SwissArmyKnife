# modules/mass_renamer.py
import os
import errno
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ----------------------
# Helper functions
# ----------------------

def _human_size(path):
    try:
        sz = path.stat().st_size
        for unit in ["B","KB","MB","GB","TB"]:
            if sz < 1024.0:
                return f"{sz:3.1f}{unit}"
            sz /= 1024.0
    except Exception:
        return "n/a"
    return "n/a"

def _parse_size(value):
    try:
        num = float(value[:-2])
        unit = value[-2:].upper()

        mult = {"B":1, "KB":1024, "MB":1024**2, "GB":1024**3, "TB":1024**4}
        if unit in mult:
            return num * mult[unit]
        return num
    except:
        return 0


_WINDOWS_RESERVED_BASENAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM0", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT0", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
_WINDOWS_INVALID_CHARS = set('<>:"/\\|?*')
_RENAME_RETRYABLE_ERRNOS = {errno.EACCES, errno.EPERM, errno.ENOENT}
_RENAME_RETRYABLE_WINERRORS = {5, 32, 33}


def _fs_key(name):
    # Windows and iCloud Drive for Windows are case-insensitive for file names.
    return name.casefold() if os.name == "nt" else name


def _validate_target_name(name):
    if not name or name in {".", ".."}:
        return "Name cannot be empty or '.'/'..'."

    if "/" in name or "\\" in name:
        return "Name cannot include path separators."

    if any(ord(ch) < 32 for ch in name):
        return "Name contains control characters."

    if os.name == "nt":
        if any(ch in _WINDOWS_INVALID_CHARS for ch in name):
            return "Name contains characters not allowed on Windows (< > : \" / \\ | ? *)."

        if name.endswith(" ") or name.endswith("."):
            return "Windows does not allow file names ending with a space or period."

        root = name.split(".", 1)[0].rstrip(" .").upper()
        if root in _WINDOWS_RESERVED_BASENAMES:
            return f"'{root}' is a reserved Windows device name."

    return None


def _is_retryable_rename_error(exc):
    if not isinstance(exc, OSError):
        return False

    if exc.errno in _RENAME_RETRYABLE_ERRNOS:
        return True

    winerror = getattr(exc, "winerror", None)
    if winerror in _RENAME_RETRYABLE_WINERRORS:
        return True

    return False


def _rename_with_retry(source_path, target_path, attempts=10, base_delay=0.08):
    last_error = None
    for attempt in range(attempts):
        try:
            source_path.rename(target_path)
            return
        except OSError as exc:
            last_error = exc
            if attempt == attempts - 1 or not _is_retryable_rename_error(exc):
                raise
            time.sleep(base_delay * (attempt + 1))

    if last_error:
        raise last_error


def ensure_unique_targets(targets):
    """Given list of (oldpath, targetname), ensure targets are unique by adding suffixes."""
    seen = set()
    final = []
    for old, name in targets:
        base, ext = os.path.splitext(name)
        candidate = name
        i = 1
        while _fs_key(candidate) in seen:
            candidate = f"{base} ({i}){ext}"
            i += 1
        seen.add(_fs_key(candidate))
        final.append((old, candidate))
    return final

# ----------------------
# Module Frame
# ----------------------

class MassRenamerFrame(ttk.Frame):
    def __init__(self, parent, app=None):
        super().__init__(parent)
        self.app = app

        # State
        self.folder_path = tk.StringVar(value="")
        self.filter_ext = tk.StringVar(value="*")
        self.sort_method = tk.StringVar(value="alphabetical")
        self.sort_order_asc = tk.BooleanVar(value=True)

        self.rename_mode = tk.StringVar(value="sequential")  # or 'replace'
        # sequential params
        self.base_name = tk.StringVar(value="File")
        self.start_number = tk.IntVar(value=1)
        self.padding = tk.IntVar(value=3)
        self.keep_extension = tk.BooleanVar(value=True)
        # replace params
        self.find_text = tk.StringVar(value="")
        self.replace_text = tk.StringVar(value="")
        # optional sync-safety delay (useful for cloud-sync folders like iCloud Drive)
        self.sync_safe_mode = tk.BooleanVar(value=False)
        self.sync_delay_seconds = tk.DoubleVar(value=0.35)

        self.view_sort_column = None
        self.view_sort_desc = False
        self.removed_files = set()

        # last rename mapping for undo (list of (final_path, original_path))
        self.last_mapping = None

        # preview mapping (list of (Path, newname))
        self._last_preview = []

        # UI
        self._build_ui()

        # current file list (full paths)
        self.current_files = []

    def notify_mode_change(self):
        # Called by main app when modes change (Quick Compile / Developer)
        if self.app:
            dev = getattr(self.app, "dev_mode_enabled", None)
            qc = getattr(self.app, "quick_compile_enabled", None)
            if dev and dev.get():
                self.configure(style="Dev.TFrame")
            elif qc and qc.get():
                self.configure(style="Quick.TFrame")
            else:
                self.configure(style="TFrame")

    # ----------------------
    # UI builders
    # ----------------------
    def _build_ui(self):
        # Top controls: folder, filter, load
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0,8))

        ttk.Label(top, text="Folder:").pack(side="left")
        folder_entry = ttk.Entry(top, textvariable=self.folder_path, width=60)
        folder_entry.pack(side="left", padx=(4,6), fill="x", expand=True)

        ttk.Button(top, text="Browse", command=self._browse_folder).pack(side="left", padx=4)
        ttk.Button(top, text="Refresh", command=self._load_files).pack(side="left")

        # Main area split: left options, right preview
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, width=360)
        left.pack(side="left", fill="y", padx=(0,8))
        left.pack_propagate(False)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        # --- Sorting options ---
        s_frame = ttk.LabelFrame(left, text="Sort / Order")
        s_frame.pack(fill="x", pady=(0,8))

        ttk.Radiobutton(s_frame, text="Alphabetical", value="alphabetical", variable=self.sort_method).pack(anchor="w", padx=6, pady=2)
        ttk.Radiobutton(s_frame, text="By Size", value="size", variable=self.sort_method).pack(anchor="w", padx=6, pady=2)
        ttk.Radiobutton(s_frame, text="Creation Time", value="ctime", variable=self.sort_method).pack(anchor="w", padx=6, pady=2)
        ttk.Radiobutton(s_frame, text="Modification Time", value="mtime", variable=self.sort_method).pack(anchor="w", padx=6, pady=2)

        order_frame = ttk.Frame(s_frame)
        order_frame.pack(fill="x", padx=6, pady=4)
        ttk.Checkbutton(order_frame, text="Ascending", variable=self.sort_order_asc).pack(side="left")

        # --- Rename mode ---
        r_frame = ttk.LabelFrame(left, text="Rename Mode")
        r_frame.pack(fill="x", pady=(0,8))

        ttk.Radiobutton(r_frame, text="Sequential (Base_001.ext)", value="sequential", variable=self.rename_mode, command=self._refresh_mode_controls).pack(anchor="w", padx=6, pady=2)
        seq_frame = ttk.Frame(r_frame)
        seq_frame.pack(fill="x", padx=6, pady=4)

        ttk.Label(seq_frame, text="Base:").grid(row=0, column=0, sticky="w")
        ttk.Entry(seq_frame, textvariable=self.base_name, width=18).grid(row=0, column=1, padx=4, sticky="w")
        ttk.Label(seq_frame, text="Start:").grid(row=1, column=0, sticky="w")
        ttk.Entry(seq_frame, textvariable=self.start_number, width=6).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(seq_frame, text="Padding:").grid(row=2, column=0, sticky="w")
        ttk.Entry(seq_frame, textvariable=self.padding, width=6).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Checkbutton(seq_frame, text="Keep extension", variable=self.keep_extension).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6,0))

        ttk.Radiobutton(r_frame, text="Search & Replace", value="replace", variable=self.rename_mode, command=self._refresh_mode_controls).pack(anchor="w", padx=6, pady=2)
        rep_frame = ttk.Frame(r_frame)
        rep_frame.pack(fill="x", padx=6, pady=4)
        ttk.Label(rep_frame, text="Find:").grid(row=0, column=0, sticky="w")
        ttk.Entry(rep_frame, textvariable=self.find_text, width=18).grid(row=0, column=1, padx=4, sticky="w")
        ttk.Label(rep_frame, text="Replace:").grid(row=1, column=0, sticky="w")
        ttk.Entry(rep_frame, textvariable=self.replace_text, width=18).grid(row=1, column=1, padx=4, sticky="w")

        # Action buttons
        a_frame = ttk.Frame(left)
        a_frame.pack(fill="x", pady=(6,0))
        ttk.Button(a_frame, text="Preview", command=self._preview).pack(side="left", padx=(0,6))
        ttk.Button(a_frame, text="Rename", command=self._apply_rename).pack(side="left", padx=(0,6))
        ttk.Button(a_frame, text="Undo Last", command=self._undo_last).pack(side="left")

        safety_frame = ttk.LabelFrame(left, text="Sync Safety")
        safety_frame.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(
            safety_frame,
            text="Enable delay between renames (safer for cloud sync)",
            variable=self.sync_safe_mode,
        ).pack(anchor="w", padx=6, pady=(4, 2))
        delay_row = ttk.Frame(safety_frame)
        delay_row.pack(anchor="w", padx=6, pady=(0, 6))
        ttk.Label(delay_row, text="Delay (seconds):").pack(side="left")
        ttk.Entry(delay_row, textvariable=self.sync_delay_seconds, width=8).pack(side="left", padx=(6, 0))

        # Status / small hints
        self.status_label = ttk.Label(left, text="Status: idle", anchor="w")
        self.status_label.pack(fill="x", pady=(8,0), padx=4)

        # --- Right: Preview table ---
        preview_frame = ttk.LabelFrame(right, text="Preview (old → new)")
        preview_frame.pack(fill="both", expand=True)

        cols = ("old", "new", "size", "modified")
        self.tree = ttk.Treeview(preview_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("old", text="Old Name", command=lambda: self._on_column_click("old"))
        self.tree.heading("new", text="New Name", command=lambda: self._on_column_click("new"))
        self.tree.heading("size", text="Size", command=lambda: self._on_column_click("size"))
        self.tree.heading("modified", text="Modified", command=lambda: self._on_column_click("modified"))

        self.tree.column("old", width=300, anchor="w")
        self.tree.column("new", width=300, anchor="w")
        self.tree.column("size", width=90, anchor="center")
        self.tree.column("modified", width=140, anchor="center")

        vsb = ttk.Scrollbar(preview_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        # Remove selected button (placed under preview so it's obvious which items are removed)
        remove_btn = ttk.Button(preview_frame, text="Remove Selected", command=self._remove_selected)
        remove_btn.pack(fill="x", pady=6)

        # load files if path already set
        self._refresh_mode_controls()

    # ----------------------
    # File loading + sorting
    # ----------------------
    def _browse_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.folder_path.set(path)
            self._load_files()

    def _load_files(self):
        folder = self.folder_path.get()
        self.current_files = []
        self._last_preview = []
        self.tree.delete(*self.tree.get_children())
        if not folder or not os.path.isdir(folder):
            self.status_label.config(text="Status: select a valid folder")
            return
        try:
            entries = []
            with os.scandir(folder) as it:
                for ent in it:
                    if ent.is_file():
                        p = Path(ent.path).resolve()
                        if p in self.removed_files:
                            continue
                        entries.append(p)
            self.current_files = entries
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", f"Failed to list files:\n{e}")
            self.status_label.config(text="Status: error loading files")

    def _sorted_files(self):
        """Return list of pathlib.Path sorted based on options."""
        files = list(self.current_files)
        if not files:
            return []

        key_name = self.sort_method.get()
        reverse = not self.sort_order_asc.get()

        if key_name == "alphabetical":
            return sorted(files, key=lambda p: p.name.lower(), reverse=reverse)
        elif key_name == "size":
            return sorted(files, key=lambda p: p.stat().st_size, reverse=reverse)
        elif key_name == "ctime":
            return sorted(files, key=lambda p: p.stat().st_ctime, reverse=reverse)
        elif key_name == "mtime":
            return sorted(files, key=lambda p: p.stat().st_mtime, reverse=reverse)
        else:
            return sorted(files, key=lambda p: p.name.lower(), reverse=reverse)

    def _on_column_click(self, col):
        # Toggle descending on repeated click
        if self.view_sort_column == col:
            self.view_sort_desc = not self.view_sort_desc
        else:
            self.view_sort_column = col
            self.view_sort_desc = False

        # Sort self._last_preview (list of (path, newname))
        if not self._last_preview:
            return

        if col == "old":
            key = lambda it: it[0].name.lower()
        elif col == "new":
            key = lambda it: it[1].lower()
        elif col == "size":
            key = lambda it: it[0].stat().st_size
        elif col == "modified":
                key = lambda it: it[0].stat().st_mtime
        else:
                return

        self._last_preview.sort(key=key, reverse=self.view_sort_desc)
        self._rebuild_preview_tree()

    def _rebuild_preview_tree(self):
        self.tree.delete(*self.tree.get_children())
        for old, new in self._last_preview:
            size = _human_size(old)
            mtime = datetime.fromtimestamp(old.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            self.tree.insert("", "end", values=(old.name, new, size, mtime))

    # ----------------------
    # Preview + name generation
    # ----------------------
    def _preview(self):
        # Ensure files loaded
        self._load_files()
        files = self._sorted_files()
        if not files:
            messagebox.showinfo("No files", "No files found to preview.")
            return

        targets = []
        mode = self.rename_mode.get()
        if mode == "sequential":
            base = self.base_name.get().strip()
            start = int(self.start_number.get())
            padding = max(0, int(self.padding.get()))
            keep_ext = self.keep_extension.get()
            num = start
            for p in files:
                if keep_ext:
                    ext = p.suffix
                else:
                    ext = ""
                newname = f"{base}_{str(num).zfill(padding)}{ext}"
                targets.append((p, newname))
                num += 1
        elif mode == "replace":
            find = self.find_text.get()
            replace = self.replace_text.get()
            if find == "":
                messagebox.showwarning("Invalid Replace Rule", "Find text cannot be empty for Search & Replace mode.")
                self.status_label.config(text="Status: replace preview blocked (empty find text)")
                return
            for p in files:
                new = p.name.replace(find, replace)
                targets.append((p, new))
        else:
            # fallback: identical names
            for p in files:
                targets.append((p, p.name))

        # make unique
        targets = ensure_unique_targets(targets)

        # Fill tree preview
        self.tree.delete(*self.tree.get_children())
        for oldp, newname in targets:
            try:
                mtime = datetime.fromtimestamp(oldp.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                size = _human_size(oldp)
            except Exception:
                mtime = "n/a"
                size = "n/a"
            self.tree.insert("", "end", values=(oldp.name, newname, size, mtime))

        # store the preview mapping in memory but not as last_mapping
        self._last_preview = targets
        self.status_label.config(text=f"Status: preview ready ({len(targets)} items)")

    def _validate_mapping(self, folder, mapping):
        source_paths = {oldp for oldp, _ in mapping}
        errors = []

        for oldp, targetname in mapping:
            name_error = _validate_target_name(targetname)
            if name_error:
                errors.append(f"{oldp.name} -> {targetname}: {name_error}")
                continue

            final_path = folder / targetname
            if final_path.exists() and final_path not in source_paths:
                errors.append(f"{oldp.name} -> {targetname}: destination already exists.")

        return errors

    def _get_sync_delay_seconds(self):
        if not self.sync_safe_mode.get():
            return 0.0

        try:
            delay = float(self.sync_delay_seconds.get())
        except Exception:
            raise ValueError("Sync safety delay must be a number.")

        if delay < 0:
            raise ValueError("Sync safety delay cannot be negative.")
        if delay > 10:
            raise ValueError("Sync safety delay is too high (max 10 seconds).")

        return delay

    # ----------------------
    # Renaming + undo
    # ----------------------
    def _apply_rename(self):
        # Ensure preview exists
        if not hasattr(self, "_last_preview") or not self._last_preview:
            self._preview()
            if not hasattr(self, "_last_preview") or not self._last_preview:
                return

        mapping = list(self._last_preview)  # list of (Path, targetname)
        if not mapping:
            messagebox.showinfo("Nothing to rename", "Preview produced no targets.")
            return

        # confirm
        msg = f"Rename {len(mapping)} files in:\n{self.folder_path.get()}\n\nProceed?"
        if not messagebox.askokcancel("Confirm Rename", msg):
            return

        # perform safe rename: two-step using temp names in same folder to avoid collisions
        folder = Path(self.folder_path.get())
        temp_map = []
        moved_to_final = []

        # disable buttons to reduce accidental double clicks
        self._set_ui_enabled(False)
        try:
            # Step 1: generate unique final target names (in case tree modified)
            mapping = ensure_unique_targets(mapping)
            sync_delay = self._get_sync_delay_seconds()
            validation_errors = self._validate_mapping(folder, mapping)
            if validation_errors:
                details = "\n".join(validation_errors[:8])
                if len(validation_errors) > 8:
                    details += f"\n... and {len(validation_errors) - 8} more"
                messagebox.showerror("Invalid Target Name(s)", details)
                self.status_label.config(text="Status: rename blocked by invalid/conflicting names")
                return

            # Step 2: build temp names and rename originals -> temp
            for oldp, targetname in mapping:
                if not oldp.exists():
                    raise FileNotFoundError(f"Source file is missing: {oldp.name}")

                # temp name in same folder to avoid cross-device rename issue
                ts = f".rename_temp_{int(time.time()*1000)}_{os.getpid()}"
                tempname = f"{oldp.name}{ts}"
                temp_path = folder / tempname

                # ensure temp doesn't exist
                i = 0
                while temp_path.exists():
                    temp_path = folder / f"{tempname}_{i}"
                    i += 1

                _rename_with_retry(oldp, temp_path)
                temp_map.append((temp_path, folder / targetname, oldp))

            # Step 3: rename temps -> final
            total = len(temp_map)
            for idx, (temp_path, final_path, oldp) in enumerate(temp_map):
                # If anything appears at destination between validation and now, abort safely.
                if final_path.exists():
                    raise FileExistsError(f"Destination already exists: {final_path.name}")

                _rename_with_retry(temp_path, final_path)
                moved_to_final.append((final_path, oldp))

                if sync_delay > 0 and idx < total - 1:
                    self.status_label.config(
                        text=f"Status: waiting {sync_delay:.2f}s for cloud sync ({idx + 1}/{total})"
                    )
                    self.update_idletasks()
                    time.sleep(sync_delay)

            # store undo mapping (final -> original names)
            undo_map = []
            for oldp, targetname in mapping:
                final_path = folder / targetname
                undo_map.append((final_path, oldp))
            self.last_mapping = undo_map

            messagebox.showinfo("Done", f"Renamed {len(mapping)} files.")
            self.status_label.config(text=f"Status: renamed {len(mapping)} files")
            # reload list and preview
            self._load_files()
            self._preview()

        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", f"Rename failed:\n{e}")

            # Attempt rollback in reverse order.
            try:
                for final_path, oldp in reversed(moved_to_final):
                    if final_path.exists() and not oldp.exists():
                        _rename_with_retry(final_path, oldp)

                for temp_path, _, oldp in reversed(temp_map):
                    if temp_path.exists() and not oldp.exists():
                        _rename_with_retry(temp_path, oldp)

                self.status_label.config(text="Status: error during rename (rollback applied)")
            except Exception as rollback_error:
                traceback.print_exc()
                self.status_label.config(text="Status: error during rename (rollback incomplete)")
                messagebox.showerror("Rollback Warning", f"Rollback did not fully complete:\n{rollback_error}")
        finally:
            self._set_ui_enabled(True)

    def _undo_last(self):
        if not self.last_mapping:
            messagebox.showinfo("Nothing to undo", "No previous rename to undo.")
            return

        if not messagebox.askokcancel("Undo Rename", f"Undo last rename ({len(self.last_mapping)} files)?"):
            return

        self._set_ui_enabled(False)
        folder = Path(self.folder_path.get())
        try:
            # try to move final -> original
            for final_path, original_path in self.last_mapping:
                if final_path.exists():
                    # ensure original parent exists
                    original_parent = original_path.parent
                    if not original_parent.exists():
                        original_parent.mkdir(parents=True, exist_ok=True)
                    # if original_path exists, make unique
                    if original_path.exists():
                        original_backup = original_path.with_name(original_path.name + ".undo_conflict")
                        _rename_with_retry(original_path, original_backup)
                    _rename_with_retry(final_path, original_path)
            messagebox.showinfo("Undo Complete", "Reverted last rename.")
            self.status_label.config(text="Status: undo complete")
            self.last_mapping = None
            self._load_files()
            self._preview()
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Undo Failed", f"Failed to undo rename:\n{e}")
            self.status_label.config(text="Status: undo failed")
        finally:
            self._set_ui_enabled(True)

    def _set_ui_enabled(self, enabled: bool):
        # quick and dirty: traverse children and disable/enable common widgets
        for child in self.winfo_children():
            try:
                child_state = "normal" if enabled else "disabled"
                if isinstance(child, (ttk.Button, ttk.Entry, ttk.Checkbutton, ttk.Radiobutton, ttk.Combobox)):
                    child.configure(state=child_state)
            except Exception:
                pass
        # ensure tree remains visible
        if not enabled:
            self.status_label.config(text="Status: busy...")
        else:
            # update done state is handled by callers
            pass

    def _refresh_mode_controls(self):
        # no fancy enable/disable of internal widgets to keep code short;
        # we rely on the name fields to be read when previewing.
        pass

    def _remove_selected(self):
        selected = self.tree.selection()
        if not selected:
            return

        removed_count = 0
        for item in selected:
            vals = self.tree.item(item, "values")
            old_name = vals[0]

            for old, new in list(self._last_preview):
                if old.name == old_name:
                    self.removed_files.add(old.resolve())
                    try:
                        self._last_preview.remove((old, new))
                    except ValueError:
                        pass
                    removed_count += 1
                    break

            self.tree.delete(item)

        self.status_label.config(text=f"Status: removed {removed_count} file(s) from batch")


# ----------------------
# Module factory
# ----------------------
def get_module(parent, app=None):
    """Returns the module frame instance to be attached to the main app."""
    frame = MassRenamerFrame(parent, app=app)
    return frame
