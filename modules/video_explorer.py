import io
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog
from PIL import Image, ImageTk
import os
import sys
import json
import subprocess
from pathlib import Path

def get_module(parent, app=None):
    return VideoExplorer(parent, app=app)



SUPPORTED_VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v")
SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff")
SUPPORTED_MEDIA_EXTS = SUPPORTED_VIDEO_EXTS + SUPPORTED_IMAGE_EXTS


class VideoExplorer(ttk.Frame):
    def __init__(self, parent, app=None):
        super().__init__(parent)
        self.app = app  # reference to main window

        self.path_var = tk.StringVar(value=str(Path.home()))
        self.view_mode = tk.StringVar(value="grid")  # 'grid' or 'list'
        self.thumbnail_cache: dict[tuple[str, tuple[int, int]], ImageTk.PhotoImage] = {}

        self.selected_video_path: Path | None = None
        self._current_videos: list[str] = []
        self.search_var = tk.StringVar(value="")

        # ----------------- DIRECTORY BAR -----------------
        dir_frame = ttk.Frame(self, padding=5, relief="groove")
        dir_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 3))
        dir_frame.columnconfigure(0, weight=1)

        path_entry = ttk.Entry(dir_frame, textvariable=self.path_var)
        path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        path_entry.bind("<Return>", lambda _e: self.change_directory())

        browse_button = ttk.Button(dir_frame, text="Browse…", command=self.browse_directory)
        browse_button.grid(row=0, column=1, padx=(0, 5))

        scan_button = ttk.Button(dir_frame, text="Scan Directory…", command=self.scan_directory)
        scan_button.grid(row=0, column=2, padx=(0, 5))

        ttk.Button(dir_frame, text="Grid View", command=lambda: self.switch_view("grid")).grid(row=0, column=3)
        ttk.Button(dir_frame, text="List View", command=lambda: self.switch_view("list")).grid(row=0, column=4)

        # ----------------- BREADCRUMB BAR -----------------
        # Removed breadcrumb/backtrack UI (replaced by "Scan Directory…" button)

        # ----------------- GRID CONFIG -----------------
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=3)
        self.columnconfigure(2, weight=1)

        # ----------------- SIDEBAR -----------------
        sidebar = ttk.Frame(self, padding=5, relief="ridge")
        sidebar.grid(row=2, column=0, sticky="nsew", padx=(0, 5))
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(3, weight=1)

        ttk.Label(sidebar, text="Categories", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")

        ttk.Label(sidebar, text="Search (name or tag):").grid(row=1, column=0, sticky="w", pady=(8, 0))
        search_entry = ttk.Entry(sidebar, textvariable=self.search_var)
        search_entry.grid(row=2, column=0, sticky="ew", pady=(3, 8))
        search_entry.bind("<KeyRelease>", lambda _e: self._refresh_view())

        self.tree = ttk.Treeview(sidebar, show="tree", height=8)
        self.tree.grid(row=3, column=0, sticky="nsew")
        self.populate_tree()

        # ----------------- MAIN VIEW -----------------
        # Use a container so canvas and scrollbar are in separate grid columns (prevents overlay artifacts)
        self.gallery_container = ttk.Frame(self)
        self.gallery_container.grid(row=2, column=1, sticky="nsew")
        self.gallery_container.columnconfigure(0, weight=1)
        self.gallery_container.rowconfigure(0, weight=1)

        self.main_canvas = tk.Canvas(self.gallery_container, borderwidth=0)
        self.main_canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self.gallery_container, orient="vertical", command=self.main_canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.main_canvas.configure(yscrollcommand=self.scrollbar.set)

        self.main_frame = ttk.Frame(self.main_canvas)
        # Keep the inner frame the same width as the canvas to avoid blank gaps
        self._main_frame_window = self.main_canvas.create_window(
            (0, 0), window=self.main_frame, anchor="nw", width=self.main_canvas.winfo_width()
        )
        self.main_frame.bind(
            "<Configure>",
            lambda _e: self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all")),
        )
        self.main_canvas.bind(
            "<Configure>", lambda e: self.main_canvas.itemconfigure(self._main_frame_window, width=e.width)
        )

        # Enable mouse-wheel scrolling when hovering over the gallery and the scrollbar
        self._setup_scrolling()

        # ----------------- DETAILS PANEL -----------------
        details = ttk.Frame(self, padding=5, relief="ridge")
        details.grid(row=2, column=2, sticky="nsew", padx=(5, 0))
        details.columnconfigure(0, weight=1)

        # Embedded preview just above the details section
        ttk.Label(details, text="Preview", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="ew")
        self._viewer_canvas = tk.Canvas(details, height=200, bg="black", highlightthickness=0)
        self._viewer_canvas.grid(row=1, column=0, sticky="ew", pady=(3, 8))
        self._viewer_canvas.bind("<Configure>", self._on_viewer_resize)
        self._viewer_photo = None  # keep a reference for images

        ttk.Label(details, text="Video Details", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, sticky="ew")

        self.detail_label = ttk.Label(details, text="(Select a video)")
        self.detail_label.grid(row=3, column=0, sticky="w", pady=(6, 6))

        btn_row = ttk.Frame(details)
        btn_row.grid(row=4, column=0, sticky="ew")
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)
        btn_row.columnconfigure(2, weight=1)

        # Prefer built-in viewer on the left
        self.view_btn = ttk.Button(btn_row, text="Open (Built-in)", command=self.open_selected_in_viewer, state="disabled")
        self.view_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self.reveal_btn = ttk.Button(btn_row, text="Show in Folder", command=self.reveal_selected, state="disabled")
        self.reveal_btn.grid(row=0, column=1, sticky="ew", padx=(4, 4))

        # External player on the right
        self.play_btn = ttk.Button(btn_row, text="Play (External)", command=self.play_selected, state="disabled")
        self.play_btn.grid(row=0, column=2, sticky="ew", padx=(0, 0))

        ttk.Separator(details).grid(row=5, column=0, sticky="ew", pady=10)

        # Condensed tags section (single line)
        ttk.Label(details, text="Tags:").grid(row=6, column=0, sticky="w")
        self.tags_var = tk.StringVar(value="")
        self.tags_entry = ttk.Entry(details, textvariable=self.tags_var)
        self.tags_entry.grid(row=7, column=0, sticky="ew", pady=(3, 6))

        tag_btns = ttk.Frame(details)
        tag_btns.grid(row=8, column=0, sticky="ew")
        tag_btns.columnconfigure(0, weight=1)
        tag_btns.columnconfigure(1, weight=1)

        self.load_tags_btn = ttk.Button(tag_btns, text="Load Tags", command=self.load_tags, state="disabled")
        self.load_tags_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.save_tags_btn = ttk.Button(tag_btns, text="Save Tags", command=self.save_tags, state="disabled")
        self.save_tags_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # Initial listing
        self.list_directory_contents(self.path_var.get())

    # ---------------------------------------------------
    #                MAIN APP INTEGRATION
    # ---------------------------------------------------
    def notify_mode_change(self):
        """
        Optional hook called by main.py when Quick Compile/Developer mode toggles.
        Keep it lightweight: just update a title tooltip-ish label if desired.
        """
        # You can extend this later (e.g., show debug info in dev mode).
        pass

    # ---------------------------------------------------
    #                DIRECTORY HANDLING
    # ---------------------------------------------------
    def browse_directory(self):
        initialdir = self.path_var.get()
        try:
            if not os.path.isdir(initialdir):
                initialdir = str(Path.home())
        except Exception:
            initialdir = str(Path.home())

        path = filedialog.askdirectory(initialdir=initialdir)
        if path:
            self.path_var.set(path)
            self.list_directory_contents(path)

    def change_directory(self):
        new_path = self.path_var.get()
        if not os.path.exists(new_path):
            self._show_message(f"Path does not exist:\n{new_path}")
            return
        self.list_directory_contents(new_path)

    def scan_directory(self):
        """
        Prompt for a directory, then recursively count:
        - folders (subdirectories, not including root)
        - videos (SUPPORTED_VIDEO_EXTS)
        - photos/images (SUPPORTED_IMAGE_EXTS)
        """
        initialdir = self.path_var.get()
        try:
            if not os.path.isdir(initialdir):
                initialdir = str(Path.home())
        except Exception:
            initialdir = str(Path.home())

        root = filedialog.askdirectory(initialdir=initialdir, title="Select directory to scan")
        if not root:
            return

        root_path = Path(root)

        # Update current directory view to the scanned directory (nice UX)
        self.path_var.set(str(root_path))
        self.list_directory_contents(str(root_path))

        def _worker():
            folders = 0
            videos = 0
            photos = 0

            try:
                # Count folders/videos/photos across all subdirectories
                for dirpath, dirnames, filenames in os.walk(root):
                    # Don't count the root folder itself
                    if os.path.abspath(dirpath) != os.path.abspath(root):
                        folders += 1

                    for fn in filenames:
                        ext = os.path.splitext(fn)[1].lower()
                        if ext in SUPPORTED_VIDEO_EXTS:
                            videos += 1
                        elif ext in SUPPORTED_IMAGE_EXTS:
                            photos += 1
            except Exception as e:
                self.after(0, lambda: self._show_message(f"Scan failed:\n{e}"))
                return

            def _show():
                self._show_message(
                    "Scan complete.\n\n"
                    f"Directory: {root_path}\n"
                    f"Folders (subdirectories): {folders}\n"
                    f"Videos: {videos}\n"
                    f"Photos: {photos}"
                )

            self.after(0, _show)

        threading.Thread(target=_worker, daemon=True).start()

    # ---------------------------------------------------
    #                   VIEW MANAGEMENT
    # ---------------------------------------------------
    def switch_view(self, mode):
        self.view_mode.set(mode)
        self._refresh_view()

    def _refresh_view(self):
        self.list_directory_contents(self.path_var.get())

    def list_directory_contents(self, path):
        for widget in self.main_frame.winfo_children():
            widget.destroy()

        self._clear_selection()

        try:
            items = os.listdir(path)
        except PermissionError:
            ttk.Label(self.main_frame, text="Access denied to this directory.", foreground="red").pack(anchor="w")
            return

        media = [f for f in items if f.lower().endswith(SUPPORTED_MEDIA_EXTS)]
        self._current_videos = sorted(media, key=str.lower)

        filtered = self._apply_search_filter(self._current_videos, Path(path))

        if not filtered:
            ttk.Label(
                self.main_frame,
                text="No matching media files found.",
                font=("Segoe UI", 9, "italic"),
            ).pack(anchor="w")
            return

        if self.view_mode.get() == "grid":
            self._show_grid(filtered, path)
        else:
            self._show_list(filtered, path)

    def _apply_search_filter(self, videos: list[str], dir_path: Path) -> list[str]:
        q = self.search_var.get().strip().lower()
        if not q:
            return videos

        out: list[str] = []
        for name in videos:
            if q in name.lower():
                out.append(name)
                continue

            tags = self._load_tags_for_video(dir_path / name).get("tags", [])
            if any(q in t.lower() for t in tags):
                out.append(name)

        return out

    # ---------------------------------------------------
    #                 DISPLAY MODES
    # ---------------------------------------------------
    def _show_grid(self, videos, path):
        columns = 4
        thumb_size = (120, 80)
        row, col = 0, 0

        for video in videos:
            frame = ttk.Frame(self.main_frame, padding=5)
            frame.grid(row=row, column=col, sticky="nsew")

            file_path = os.path.join(path, video)
            thumbnail = self._get_thumbnail(file_path, thumb_size)
            img_label = ttk.Label(frame, image=thumbnail)
            img_label.image = thumbnail
            img_label.pack()

            name_label = ttk.Label(frame, text=video, wraplength=120)
            name_label.pack(anchor="center")

            for w in (frame, img_label, name_label):
                w.bind("<Button-1>", lambda _e, v=video: self.show_details(v))
                w.bind("<Double-Button-1>", lambda _e, v=video, p=path: self._open_in_viewer(v, p))

            col += 1
            if col >= columns:
                col = 0
                row += 1

    def _show_list(self, videos, path):
        for video in videos:
            frame = ttk.Frame(self.main_frame, padding=3)
            frame.pack(fill="x")

            file_path = os.path.join(path, video)
            thumbnail = self._get_thumbnail(file_path, (80, 60))
            img_label = ttk.Label(frame, image=thumbnail)
            img_label.image = thumbnail
            img_label.pack(side="left")

            name_label = ttk.Label(frame, text=video)
            name_label.pack(side="left", padx=10)

            for w in (frame, img_label, name_label):
                w.bind("<Button-1>", lambda _e, v=video: self.show_details(v))
                w.bind("<Double-Button-1>", lambda _e, v=video, p=path: self._open_in_viewer(v, p))

    # ---------------------------------------------------
    #                 SCROLLING HELPERS
    # ---------------------------------------------------
    def _setup_scrolling(self):
        """Bind mouse-wheel scrolling while hovering over the gallery or its scrollbar."""
        self._wheel_bound = False
        # Bind enter/leave on canvas, inner frame, and scrollbar
        for w in (self.main_canvas, self.main_frame, self.scrollbar):
            try:
                w.bind("<Enter>", lambda _e: self._enable_wheel())
            except Exception:
                pass
            try:
                w.bind("<Leave>", lambda _e: self._disable_wheel())
            except Exception:
                pass

    def _enable_wheel(self):
        if getattr(self, "_wheel_bound", False):
            return
        # Initialize coalesced wheel state
        self._wheel_pending_steps = 0
        self._wheel_job = None
        # Windows/macOS wheel
        try:
            self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        except Exception:
            pass
        # Linux wheel
        try:
            self.bind_all("<Button-4>", self._on_mousewheel_linux, add="+")
            self.bind_all("<Button-5>", self._on_mousewheel_linux, add="+")
        except Exception:
            pass
        self._wheel_bound = True

    def _disable_wheel(self):
        if not getattr(self, "_wheel_bound", False):
            return
        try:
            self.unbind_all("<MouseWheel>")
        except Exception:
            pass
        try:
            self.unbind_all("<Button-4>")
        except Exception:
            pass
        try:
            self.unbind_all("<Button-5>")
        except Exception:
            pass
        self._wheel_bound = False
        # Cancel any pending coalesced scroll
        try:
            if getattr(self, "_wheel_job", None):
                self.after_cancel(self._wheel_job)
        except Exception:
            pass
        self._wheel_job = None
        self._wheel_pending_steps = 0

    def _on_mousewheel(self, event):
        # event.delta is typically +/-120 on Windows; on macOS it may vary
        try:
            steps = int(-event.delta / 120)
        except Exception:
            steps = -1 if getattr(event, "delta", 0) > 0 else 1
        if steps == 0:
            steps = -1 if getattr(event, "delta", 0) > 0 else 1
        self._queue_wheel_steps(steps)

    def _on_mousewheel_linux(self, event):
        if event.num == 4:
            self._queue_wheel_steps(-1)
        elif event.num == 5:
            self._queue_wheel_steps(1)

    def _queue_wheel_steps(self, steps: int):
        """Accumulate wheel steps and scroll in a short burst to prevent smear artifacts."""
        try:
            self._wheel_pending_steps += steps
        except Exception:
            self._wheel_pending_steps = steps
        # Debounce the actual scroll
        try:
            if getattr(self, "_wheel_job", None):
                self.after_cancel(self._wheel_job)
        except Exception:
            pass
        self._wheel_job = self.after(12, self._flush_wheel)

    def _flush_wheel(self):
        steps = getattr(self, "_wheel_pending_steps", 0)
        self._wheel_pending_steps = 0
        self._wheel_job = None
        if steps:
            try:
                self.main_canvas.yview_scroll(steps, "units")
            except Exception:
                pass

    # ---------------------------------------------------
    #                 THUMBNAIL SYSTEM
    # ---------------------------------------------------
    def _get_thumbnail(self, file_path: str, size: tuple[int, int]) -> ImageTk.PhotoImage:
        """
        Generate a real thumbnail:
        - Images: use PIL to scale.
        - Videos: use ffmpeg to grab a frame (fallback to placeholder if ffmpeg unavailable).
        """
        key = (file_path, size)
        if key in self.thumbnail_cache:
            return self.thumbnail_cache[key]

        ext = os.path.splitext(file_path)[1].lower()

        # 1) Try image thumbnails directly via PIL
        if ext in SUPPORTED_IMAGE_EXTS:
            try:
                # Use context manager and copy to avoid issues with closed file handles
                with Image.open(file_path) as im:
                    im = im.convert("RGB")
                    im.thumbnail(size)
                    img = im.copy()
                photo = ImageTk.PhotoImage(img)
                self.thumbnail_cache[key] = photo
                return photo
            except Exception:
                pass

        # 2) Try video frame via ffmpeg if available
        if ext in SUPPORTED_VIDEO_EXTS and self._ffmpeg_available():
            try:
                # Seek to 1s; scale to fit; output single PNG frame to stdout
                cmd = [
                    "ffmpeg",
                    "-ss", "00:00:01.000",
                    "-i", file_path,
                    "-frames:v", "1",
                    "-vf", f"scale={size[0]}:-1:force_original_aspect_ratio=decrease",
                    "-f", "image2pipe",
                    "-vcodec", "png",
                    "pipe:1",
                ]
                proc = subprocess.run(cmd, capture_output=True, check=False)
                if proc.stdout:
                    buf = io.BytesIO(proc.stdout)
                    img = Image.open(buf).convert("RGB")
                    img.thumbnail(size)
                    photo = ImageTk.PhotoImage(img)
                    self.thumbnail_cache[key] = photo
                    return photo
            except Exception:
                pass

        # 3) Fallback placeholder
        img = Image.new("RGB", size, (80, 120, 180))
        try:
            from PIL import ImageDraw
            draw = ImageDraw.Draw(img)
            base = os.path.splitext(os.path.basename(file_path))[0]
            text = (base[:2] or "M").upper()
            draw.text((10, 20), text, fill="white")
        except Exception:
            pass

        photo = ImageTk.PhotoImage(img)
        self.thumbnail_cache[key] = photo
        return photo

    # ---------------------------------------------------
    #                 DETAILS + ACTIONS
    # ---------------------------------------------------
    def show_details(self, video_name: str):
        dir_path = Path(self.path_var.get())
        video_path = (dir_path / video_name).resolve()
        self.selected_video_path = video_path

        size = None
        try:
            size = video_path.stat().st_size
        except OSError:
            size = None

        self.detail_label.config(
            text=f"Selected: {video_name}\nPath: {video_path}\nSize: {size if size is not None else 'Unknown'} bytes"
        )

        self.play_btn.config(state="normal")
        self.reveal_btn.config(state="normal")
        if hasattr(self, "view_btn"):
            self.view_btn.config(state="normal")
        self.load_tags_btn.config(state="normal")
        self.save_tags_btn.config(state="normal")

        self.load_tags()
        # Auto-open in the built-in viewer for immediate preview
        self.open_selected_in_viewer()

    def _open_and_play(self, video_name: str):
        self.show_details(video_name)
        self.play_selected()

    def play_selected(self):
        if not self.selected_video_path:
            return
        self._open_with_default_app(self.selected_video_path)

    def reveal_selected(self):
        if not self.selected_video_path:
            return
        self._reveal_in_file_manager(self.selected_video_path)

    def open_selected_in_viewer(self):
        """Open the currently selected media in the built-in viewer (FFmpeg-backed for videos)."""
        if not self.selected_video_path:
            return
        self._show_viewer(self.selected_video_path)

    def _open_in_viewer(self, media_name: str, dir_path: str):
        """Open from double-click in the list/grid."""
        self.show_details(media_name)
        p = (Path(dir_path) / media_name).resolve()
        self._show_viewer(p)

    def _ffmpeg_available(self) -> bool:
        if hasattr(self, "_ffmpeg_cached"):
            return bool(self._ffmpeg_cached)
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            self._ffmpeg_cached = True
        except Exception:
            self._ffmpeg_cached = False
        return bool(self._ffmpeg_cached)

    def _stop_video_playback(self):
        self._viewer_running = False
        proc = getattr(self, "_viewer_play_proc", None)
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
            self._viewer_play_proc = None
        th = getattr(self, "_viewer_thread", None)
        if th and th.is_alive():
            try:
                th.join(timeout=0.5)
            except Exception:
                pass
        self._viewer_thread = None

    def _video_reader_thread(self, cmd: list[str], frame_w: int, frame_h: int):
        frame_size = frame_w * frame_h * 3
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=frame_size * 2,
            )
        except Exception:
            self._viewer_running = False
            return
        self._viewer_play_proc = proc

        try:
            while self._viewer_running:
                if not proc.stdout:
                    break
                buf = proc.stdout.read(frame_size)
                if not buf or len(buf) < frame_size:
                    break
                try:
                    img = Image.frombytes("RGB", (frame_w, frame_h), buf)
                    photo = ImageTk.PhotoImage(img)
                except Exception:
                    continue

                def _update():
                    if getattr(self, "_viewer_canvas", None) and self._viewer_running:
                        # keep a reference
                        self._viewer_photo = photo
                        self._viewer_canvas.delete("all")
                        self._viewer_canvas.create_image(
                            frame_w // 2, frame_h // 2, image=self._viewer_photo, anchor="center"
                        )

                self.after(0, _update)
                time.sleep(1 / 24.0)
        finally:
            try:
                if proc and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    def _show_viewer(self, media_path: Path):
        """Render the selected media into the embedded preview area (images) or start inline playback (videos)."""
        # Stop any previous playback
        try:
            if getattr(self, "_viewer_running", False):
                self._stop_video_playback()
        except Exception:
            pass

        self._viewer_last_media_path = media_path

        canvas = getattr(self, "_viewer_canvas", None)
        if not canvas or not canvas.winfo_exists():
            return

        ext = media_path.suffix.lower()

        # Image display via PIL (embedded)
        if ext in SUPPORTED_IMAGE_EXTS:
            try:
                canvas.update_idletasks()
                w = max(200, canvas.winfo_width() or 640)
                h = max(150, canvas.winfo_height() or 360)
                with Image.open(media_path) as img:
                    img = img.convert("RGB")
                    img_copy = img.copy()
                    img_copy.thumbnail((w - 10, h - 10))
                photo = ImageTk.PhotoImage(img_copy)
                self._viewer_photo = photo
                canvas.delete("all")
                canvas.create_image(w // 2, h // 2, image=self._viewer_photo, anchor="center")
            except Exception as e:
                self._show_message(f"Could not display image:\n{e}")
            return

        # Video playback via ffmpeg piping (embedded)
        if ext in SUPPORTED_VIDEO_EXTS:
            if not self._ffmpeg_available():
                self._show_message("FFmpeg is not available on PATH. Use external player instead.")
                return

            # Choose a reasonable frame size based on current canvas size
            try:
                canvas.update_idletasks()
            except Exception:
                pass
            frame_w = max(320, canvas.winfo_width() or 640)
            frame_h = max(180, canvas.winfo_height() or 360)

            vf = f"scale={frame_w}:{frame_h}:force_original_aspect_ratio=decrease,pad={frame_w}:{frame_h}:(ow-iw)/2:(oh-ih)/2"
            cmd = [
                "ffmpeg",
                "-loglevel", "error",
                "-i", str(media_path),
                "-vf", vf,
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-r", "24",
                "pipe:1",
            ]

            self._viewer_running = True
            th = threading.Thread(target=self._video_reader_thread, args=(cmd, frame_w, frame_h), daemon=True)
            self._viewer_thread = th
            th.start()
            return

        # Fallback for unknown types
        self._show_message("Unsupported media type for built-in viewer.")

    def _on_viewer_resize(self, _event):
        """Debounce canvas resizes and re-render media to keep it centered and within bounds."""
        media_path = getattr(self, "_viewer_last_media_path", None)
        if not media_path:
            return
        try:
            if getattr(self, "_viewer_resize_job", None):
                self.after_cancel(self._viewer_resize_job)
        except Exception:
            pass

        def _do():
            try:
                self._show_viewer(media_path)
            except Exception:
                pass

        self._viewer_resize_job = self.after(80, _do)

    # ---------------------------------------------------
    #                 TAG / METADATA (SIDECAR JSON)
    # ---------------------------------------------------
    def _tags_sidecar_path(self, video_path: Path) -> Path:
        return video_path.with_suffix(video_path.suffix + ".tags.json")

    def _load_tags_for_video(self, video_path: Path) -> dict:
        sidecar = self._tags_sidecar_path(video_path)
        if not sidecar.exists():
            return {"tags": []}
        try:
            with sidecar.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"tags": []}
            tags = data.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            tags = [str(t) for t in tags if str(t).strip()]
            return {"tags": tags}
        except Exception:
            return {"tags": []}

    def load_tags(self):
        if not self.selected_video_path:
            return
        data = self._load_tags_for_video(self.selected_video_path)
        tags = data.get("tags", [])
        if hasattr(self, "tags_var"):
            self.tags_var.set(", ".join(tags))

    def save_tags(self):
        if not self.selected_video_path:
            return
        raw = (self.tags_var.get() if hasattr(self, "tags_var") else "").strip()
        tags = [t.strip() for t in raw.split(",") if t.strip()]
        # de-dupe preserving order
        seen: set[str] = set()
        tags = [t for t in tags if not (t.lower() in seen or seen.add(t.lower()))]

        sidecar = self._tags_sidecar_path(self.selected_video_path)
        payload = {"tags": tags}
        try:
            with sidecar.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._show_message(f"Failed to save tags:\n{e}")
            return

        # refresh current view so searching-by-tag updates immediately
        self._refresh_view()

    # ---------------------------------------------------
    #                 PLATFORM HELPERS
    # ---------------------------------------------------
    def _open_with_default_app(self, path: Path):
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
                return
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
                return
            subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            self._show_message(f"Could not open media:\n{e}")

    def _reveal_in_file_manager(self, path: Path):
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(path)])
                return
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
                return
            subprocess.Popen(["xdg-open", str(path.parent)])
        except Exception as e:
            self._show_message(f"Could not open folder:\n{e}")

    def _clear_selection(self):
        self.selected_video_path = None
        self.detail_label.config(text="(Select a video)")
        self.play_btn.config(state="disabled")
        self.reveal_btn.config(state="disabled")
        if hasattr(self, "view_btn"):
            self.view_btn.config(state="disabled")
        self.load_tags_btn.config(state="disabled")
        self.save_tags_btn.config(state="disabled")
        if hasattr(self, "tags_var"):
            self.tags_var.set("")
        # Stop any running playback and clear the embedded preview
        try:
            self._stop_video_playback()
            if getattr(self, "_viewer_canvas", None):
                self._viewer_canvas.delete("all")
        except Exception:
            pass

    # ---------------------------------------------------
    #                 HELPER METHODS
    # ---------------------------------------------------
    def populate_tree(self):
        self.tree.insert("", "end", "gaming", text="Gaming")
        self.tree.insert("", "end", "school", text="School")
        self.tree.insert("", "end", "holidays", text="Holidays")

    def _show_message(self, msg):
        popup = tk.Toplevel(self)
        popup.title("Notice")
        ttk.Label(popup, text=msg, padding=10).pack()
        ttk.Button(popup, text="OK", command=popup.destroy).pack(pady=5)
        popup.transient(self)
        popup.grab_set()