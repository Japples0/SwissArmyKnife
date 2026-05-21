import os
import sqlite3
import time
import hashlib
import shutil
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


DB_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT,
    last_seen_abs_path TEXT,
    last_seen_at REAL NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(root_id, rel_path),
    FOREIGN KEY(root_id) REFERENCES roots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_files_root ON files(root_id);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
"""


def _sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _safe_relpath(abs_path: str, root: str) -> str:
    rel = os.path.relpath(abs_path, root)
    # normalize to avoid surprises across platforms
    return rel.replace("\\", "/")


class FileIndexerFrame(ttk.Frame):
    """
    Database construction tool:
    - Build/Update index from a root directory
    - Restore moved/missing files to their original recorded locations
    """

    def __init__(self, parent, app=None):
        super().__init__(parent)
        self.app = app

        self.db_path_var = tk.StringVar(value="")
        self.root_dir_var = tk.StringVar(value="")
        self.search_dir_var = tk.StringVar(value="")

        self.hash_enabled_var = tk.BooleanVar(value=True)
        self.restore_overwrite_var = tk.BooleanVar(value=False)
        self.restore_rename_var = tk.BooleanVar(value=True)

        self._stop_flag = threading.Event()
        self._worker_thread: threading.Thread | None = None

        self._build_ui()

    def notify_mode_change(self):
        # Optional hook used by the main app; keep it safe/no-op.
        pass

    # ---------------- UI ----------------
    def _build_ui(self):
        header = ttk.Label(self, text="File Indexer (Backup Database + Restore)", font=("Segoe UI", 14, "bold"))
        header.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        # DB selection
        ttk.Label(self, text="Database (SQLite .db):").grid(row=1, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.db_path_var, width=60).grid(row=1, column=1, sticky="ew", padx=(5, 5))
        ttk.Button(self, text="Browse…", command=self._choose_db).grid(row=1, column=2, sticky="ew")
        ttk.Button(self, text="Init DB", command=self._init_db_clicked).grid(row=1, column=3, sticky="ew")

        # Root directory selection
        ttk.Label(self, text="Root directory to index:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(self, textvariable=self.root_dir_var, width=60).grid(row=2, column=1, sticky="ew", padx=(5, 5), pady=(8, 0))
        ttk.Button(self, text="Choose…", command=self._choose_root).grid(row=2, column=2, sticky="ew", pady=(8, 0))
        ttk.Button(self, text="Scan / Update", command=self._scan_clicked).grid(row=2, column=3, sticky="ew", pady=(8, 0))

        # Options
        opts = ttk.LabelFrame(self, text="Options")
        opts.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        opts.columnconfigure(1, weight=1)

        ttk.Checkbutton(opts, text="Compute SHA-256 hashes (slower, more reliable restore)", variable=self.hash_enabled_var).grid(
            row=0, column=0, sticky="w", padx=8, pady=6
        )

        # Restore controls
        restore = ttk.LabelFrame(self, text="Restore")
        restore.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        restore.columnconfigure(1, weight=1)

        ttk.Label(restore, text="Search directory (where to look for moved files):").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 0))
        ttk.Entry(restore, textvariable=self.search_dir_var, width=60).grid(row=0, column=1, sticky="ew", padx=(5, 5), pady=(8, 0))
        ttk.Button(restore, text="Choose…", command=self._choose_search_dir).grid(row=0, column=2, sticky="ew", pady=(8, 0))

        ttk.Checkbutton(restore, text="Allow overwrite on restore (dangerous)", variable=self.restore_overwrite_var).grid(
            row=1, column=0, sticky="w", padx=8, pady=6
        )
        ttk.Checkbutton(restore, text="If destination exists, restore by renaming (e.g., file (1).ext)", variable=self.restore_rename_var).grid(
            row=1, column=1, sticky="w", padx=8, pady=6
        )

        ttk.Button(restore, text="Restore Missing/Moved", command=self._restore_clicked).grid(row=2, column=2, sticky="ew", padx=8, pady=(0, 10))

        # Progress / log
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(12, 0))

        self.log = tk.Text(self, height=14, wrap="word")
        self.log.grid(row=6, column=0, columnspan=4, sticky="nsew", pady=(8, 0))
        self.grid_rowconfigure(6, weight=1)
        self.grid_columnconfigure(1, weight=1)

        btns = ttk.Frame(self)
        btns.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        btns.columnconfigure(0, weight=1)

        ttk.Button(btns, text="Stop", command=self._stop).grid(row=0, column=1, sticky="e")
        ttk.Button(btns, text="Clear Log", command=self._clear_log).grid(row=0, column=2, sticky="e", padx=(8, 0))

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {msg}\n")
        self.log.see("end")

    def _clear_log(self):
        self.log.delete("1.0", "end")

    # ------------- File dialogs -------------
    def _choose_db(self):
        path = filedialog.asksaveasfilename(
            title="Choose SQLite database file",
            defaultextension=".db",
            filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")]
        )
        if path:
            self.db_path_var.set(path)

    def _choose_root(self):
        path = filedialog.askdirectory(title="Choose root directory to index")
        if path:
            self.root_dir_var.set(path)

    def _choose_search_dir(self):
        path = filedialog.askdirectory(title="Choose search directory for restore")
        if path:
            self.search_dir_var.set(path)

    # ------------- DB helpers -------------
    def _connect(self) -> sqlite3.Connection:
        db_path = self.db_path_var.get().strip()
        if not db_path:
            raise ValueError("Please choose a database file.")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection):
        conn.executescript(DB_SCHEMA)
        conn.commit()

    def _get_or_create_root_id(self, conn: sqlite3.Connection, root_path: str) -> int:
        cur = conn.cursor()
        cur.execute("SELECT id FROM roots WHERE root_path = ?", (root_path,))
        row = cur.fetchone()
        if row:
            return int(row[0])
        cur.execute("INSERT INTO roots (root_path, created_at) VALUES (?, ?)", (root_path, time.time()))
        conn.commit()
        return int(cur.lastrowid)

    # ------------- Actions -------------
    def _init_db_clicked(self):
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
            self._log("Initialized database schema.")
        except Exception as e:
            messagebox.showerror("Init DB failed", str(e))

    def _scan_clicked(self):
        if self._worker_thread and self._worker_thread.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return

        root = self.root_dir_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showerror("Invalid root", "Please choose a valid root directory.")
            return

        self._stop_flag.clear()
        self.progress.configure(value=0, maximum=100)
        self._worker_thread = threading.Thread(target=self._scan_worker, args=(root,), daemon=True)
        self._worker_thread.start()

    def _scan_worker(self, root: str):
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                root_id = self._get_or_create_root_id(conn, root)

                # Count files for progress
                all_files = []
                for dirpath, _, filenames in os.walk(root):
                    if self._stop_flag.is_set():
                        self._log("Scan stopped by user.")
                        return
                    for fn in filenames:
                        all_files.append(os.path.join(dirpath, fn))

                total = max(1, len(all_files))
                self._ui_progress_max(total)

                now = time.time()
                updated = 0

                for i, abs_path in enumerate(all_files, start=1):
                    if self._stop_flag.is_set():
                        self._log("Scan stopped by user.")
                        return

                    try:
                        st = os.stat(abs_path)
                        rel = _safe_relpath(abs_path, root)
                        sha = _sha256_file(abs_path) if self.hash_enabled_var.get() else None

                        conn.execute(
                            """
                            INSERT INTO files (root_id, rel_path, size, mtime, sha256, last_seen_abs_path, last_seen_at, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(root_id, rel_path) DO UPDATE SET
                                size=excluded.size,
                                mtime=excluded.mtime,
                                sha256=COALESCE(excluded.sha256, files.sha256),
                                last_seen_abs_path=excluded.last_seen_abs_path,
                                last_seen_at=excluded.last_seen_at,
                                status='present'
                            """,
                            (root_id, rel, int(st.st_size), float(st.st_mtime), sha, abs_path, now, "present")
                        )
                        updated += 1
                    except FileNotFoundError:
                        # file disappeared mid-scan; ignore
                        pass

                    if i % 50 == 0:
                        conn.commit()
                    self._ui_progress_value(i)

                conn.commit()

                # Mark files not seen in this scan as missing
                conn.execute(
                    "UPDATE files SET status='missing' WHERE root_id=? AND last_seen_at < ?",
                    (root_id, now)
                )
                conn.commit()

                self._log(f"Scan complete. Updated/inserted: {updated}. Root: {root}")
        except Exception as e:
            self._log(f"Scan failed: {e}")

    def _restore_clicked(self):
        if self._worker_thread and self._worker_thread.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return

        root = self.root_dir_var.get().strip()
        if not root:
            messagebox.showerror("Root required", "Choose the original root directory (the one you indexed).")
            return

        search_dir = self.search_dir_var.get().strip()
        if not search_dir or not os.path.isdir(search_dir):
            messagebox.showerror("Search directory required", "Choose a valid search directory to locate moved files.")
            return

        self._stop_flag.clear()
        self.progress.configure(value=0, maximum=100)
        self._worker_thread = threading.Thread(target=self._restore_worker, args=(root, search_dir), daemon=True)
        self._worker_thread.start()

    def _restore_worker(self, root: str, search_dir: str):
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)

                cur = conn.execute("SELECT id FROM roots WHERE root_path=?", (root,))
                row = cur.fetchone()
                if not row:
                    self._log("No index found for that root. Run Scan / Update first.")
                    return
                root_id = int(row[0])

                missing = conn.execute(
                    "SELECT id, rel_path, size, mtime, sha256, last_seen_abs_path FROM files WHERE root_id=? AND status='missing'",
                    (root_id,)
                ).fetchall()

                total = max(1, len(missing))
                self._ui_progress_max(total)

                restored = 0
                skipped = 0

                for i, (file_id, rel_path, size, mtime, sha256, last_seen_abs_path) in enumerate(missing, start=1):
                    if self._stop_flag.is_set():
                        self._log("Restore stopped by user.")
                        return

                    original_abs = os.path.join(root, rel_path.replace("/", os.sep))

                    # If it already exists, just mark present
                    if os.path.exists(original_abs):
                        conn.execute("UPDATE files SET status='present', last_seen_abs_path=?, last_seen_at=? WHERE id=?",
                                     (original_abs, time.time(), file_id))
                        skipped += 1
                        self._ui_progress_value(i)
                        continue

                    found = None

                    # 1) check last_seen_abs_path
                    if last_seen_abs_path and os.path.exists(last_seen_abs_path):
                        found = last_seen_abs_path

                    # 2) search by hash (preferred) or by name/size fallback
                    if not found:
                        found = self._find_candidate(search_dir, rel_path, int(size), sha256)

                    if not found:
                        self._log(f"Not found: {rel_path}")
                        self._ui_progress_value(i)
                        continue

                    # Ensure destination folder exists
                    os.makedirs(os.path.dirname(original_abs), exist_ok=True)

                    dest_path = original_abs
                    if os.path.exists(dest_path):
                        if self.restore_overwrite_var.get():
                            pass  # will overwrite via move+replace semantics below
                        elif self.restore_rename_var.get():
                            dest_path = self._next_available_name(dest_path)
                        else:
                            self._log(f"Conflict, skipped (destination exists): {rel_path}")
                            skipped += 1
                            self._ui_progress_value(i)
                            continue

                    # Move back
                    try:
                        if self.restore_overwrite_var.get() and os.path.exists(dest_path):
                            os.remove(dest_path)
                        shutil.move(found, dest_path)

                        conn.execute(
                            "UPDATE files SET status='present', last_seen_abs_path=?, last_seen_at=? WHERE id=?",
                            (dest_path, time.time(), file_id)
                        )
                        conn.commit()
                        restored += 1
                        self._log(f"Restored: {rel_path}")
                    except Exception as e:
                        self._log(f"Failed to restore {rel_path}: {e}")

                    self._ui_progress_value(i)

                self._log(f"Restore complete. Restored: {restored}, Skipped: {skipped}, Missing remaining: {total - restored - skipped}")
        except Exception as e:
            self._log(f"Restore failed: {e}")

    def _find_candidate(self, search_dir: str, rel_path: str, size: int, sha256: str | None) -> str | None:
        target_name = os.path.basename(rel_path.replace("/", os.sep))

        for dirpath, _, filenames in os.walk(search_dir):
            if self._stop_flag.is_set():
                return None

            if target_name in filenames:
                cand = os.path.join(dirpath, target_name)
                try:
                    st = os.stat(cand)
                except FileNotFoundError:
                    continue

                if int(st.st_size) != int(size):
                    continue

                # If we have a hash in DB, verify it
                if sha256:
                    try:
                        if _sha256_file(cand) != sha256:
                            continue
                    except Exception:
                        continue

                return cand

        # If name-based search fails but the hash exists, do a full hash scan (slow) 
        if sha256:
            for dirpath, _, filenames in os.walk(search_dir):
                if self._stop_flag.is_set():
                    return None
                for fn in filenames:
                    cand = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(cand)
                        if int(st.st_size) != int(size):
                            continue
                        if _sha256_file(cand) == sha256:
                            return cand
                    except Exception:
                        continue

        return None

    def _next_available_name(self, path: str) -> str:
        base, ext = os.path.splitext(path)
        n = 1
        while True:
            candidate = f"{base} ({n}){ext}"
            if not os.path.exists(candidate):
                return candidate
            n += 1

    def _stop(self):
        self._stop_flag.set()
        self._log("Stop requested...")

    # UI-thread safe progress updates
    def _ui_progress_max(self, maximum: int):
        def _set():
            self.progress.configure(maximum=maximum, value=0)
        self.after(0, _set)

    def _ui_progress_value(self, value: int):
        def _set():
            self.progress.configure(value=value)
        self.after(0, _set)


def get_module(parent, app=None):
    return FileIndexerFrame(parent, app=app)