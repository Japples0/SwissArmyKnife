import json
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
from natsort import natsorted

# Supported image extensions
SUPPORTED_EXTS = [
    "jpg", "jpeg", "png",
    "webp",
    "heic", "heif"
]


def collect_image_paths(directory, filetype="combine"):
    """Return image paths in explicit manifest order or flat natural order."""
    directory = os.path.abspath(directory)
    wanted_type = filetype.lower()
    manifest_path = os.path.join(directory, "manifest.json")

    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            ordered_paths = []
            for chapter in manifest.get("chapters", []):
                for image in chapter.get("images", []):
                    local_path = image.get("local_path")
                    if not local_path:
                        continue
                    parts = local_path.replace("\\", "/").split("/")
                    absolute_path = os.path.join(directory, *parts)
                    extension = os.path.splitext(absolute_path)[1].lower().lstrip(".")
                    if not os.path.isfile(absolute_path):
                        continue
                    if wanted_type != "combine" and extension != wanted_type:
                        continue
                    if extension in SUPPORTED_EXTS:
                        ordered_paths.append(absolute_path)
            if ordered_paths:
                return ordered_paths
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"Could not read sequential manifest; using flat folder scan: {exc}")

    if wanted_type == "combine":
        filenames = [
            filename
            for filename in os.listdir(directory)
            if any(filename.lower().endswith(f".{extension}") for extension in SUPPORTED_EXTS)
        ]
    else:
        filenames = [
            filename
            for filename in os.listdir(directory)
            if filename.lower().endswith(f".{wanted_type}")
        ]
    return [os.path.join(directory, filename) for filename in natsorted(filenames)]


def create_pdf_from_images(image_paths, output_path):
    """Create a PDF from image paths already arranged in the desired order."""
    images = []
    try:
        for image_path in image_paths:
            try:
                with Image.open(image_path) as source:
                    images.append(source.convert("RGB"))
            except Exception as exc:
                print(f"Skipping {image_path}: {exc}")
        if not images:
            raise ValueError("Could not load any images.")
        images[0].save(output_path, save_all=True, append_images=images[1:])
        return len(images)
    finally:
        for image in images:
            image.close()

# HEIC/HEIF support (if available)
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False
    print("Warning: 'pillow-heif' not installed. HEIC/HEIF images will be skipped.")


class PDFCompiler(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app  # store reference to main window

        # --- Track User Interactions ---
        self.user_touched_filename = False

        # Traces we add (so we can clean them up using destroy command)
        # Each item will be (tk_variable, mode, callback_name)
        self._traces = []

        # Variables (use app-level mode toggles; don't duplicate quick_mode here)
        self.mode_var = tk.StringVar(value="Single Strip")
        self.path_var = tk.StringVar()
        self.filetype_var = tk.StringVar(value="jpg")
        self.ppi_var = tk.StringVar(value="Original")
        self.save_location_var = tk.StringVar(value="Same as source")
        self.filename_var = tk.StringVar(value="AlbumName.pdf")

        # Register a trace on the app mode variables so module updates when main toggles occur
        try:
            cb = lambda *a: self.update_mode_state()
            # store (var, mode, handler) so we can remove later
            self._traces.append(
                (self.app.quick_compile_enabled, "write", self.app.quick_compile_enabled.trace_add("write", cb)))
            self._traces.append((self.app.dev_mode_enabled, "write", self.app.dev_mode_enabled.trace_add("write", cb)))
        except Exception:
            # If app doesn't provide these, ignore silently
            pass

        # --- Layout ---
        self.create_widgets()

    def create_widgets(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="PDF Compiler Setup", font=("Segoe UI", 14, "bold")).pack(pady=5)

        # Mode Selection
        self.mode_frame = ttk.LabelFrame(main, text="Mode")
        self.mode_frame.pack(fill="x", pady=5)
        modes = ["Single Strip", "Picture by Picture", "Specific Strip", "5-page Join"]
        ttk.Combobox(self.mode_frame, textvariable=self.mode_var, values=modes, state="readonly").pack(fill="x", padx=5, pady=5)

        # Path Selection
        self.path_frame = ttk.LabelFrame(main, text="Image Source Path")
        self.path_frame.pack(fill="x", pady=5)
        ttk.Entry(self.path_frame, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=5, pady=5)
        ttk.Button(self.path_frame, text="Browse", command=self.select_folder).pack(side="left", padx=5)

        # File Type
        self.filetype_frame = ttk.LabelFrame(main, text="File Type")
        self.filetype_frame.pack(fill="x", pady=5)
        ttk.Combobox(
            self.filetype_frame,
            textvariable=self.filetype_var,
            values=SUPPORTED_EXTS + ["Combine"],
            state="readonly"
        ).pack(fill="x", padx=5, pady=5)

        # Pixel Density
        self.ppi_frame = ttk.LabelFrame(main, text="Pixel Density (PPI)")
        self.ppi_frame.pack(fill="x", pady=5)
        ttk.Combobox(self.ppi_frame, textvariable=self.ppi_var, values=["100", "200", "300", "400", "Original"], state="readonly").pack(fill="x", padx=5, pady=5)

        # Save Options
        self.save_frame = ttk.LabelFrame(main, text="Save Options")
        self.save_frame.pack(fill="x", pady=5)

        ttk.Entry(self.save_frame, textvariable=self.save_location_var).pack(side="left", fill="x", expand=True, padx=5, pady=5)
        ttk.Button(self.save_frame, text="Browse", command=self.select_save_location).pack(side="left", padx=5)

        # Single filename entry (do NOT create two)
        filename_entry = ttk.Entry(self.save_frame, textvariable=self.filename_var)
        filename_entry.pack(fill="x", padx=5, pady=5)

        # Track manual edits so autoset stops once user types
        filename_entry.bind("<Key>", lambda e: self._mark_filename_as_custom())

        # Scan Button
        ttk.Button(main, text="Scan Images", command=self.scan_images).pack(pady=10)

        # Image List Frame
        self.image_list_frame = ttk.LabelFrame(main, text="Detected Images")
        self.image_list_frame.pack(fill="both", expand=True, pady=5)
        self.image_listbox = tk.Listbox(self.image_list_frame, height=8)
        self.image_listbox.pack(side="left", fill="both", expand=True, padx=5, pady=5)

        scrollbar = ttk.Scrollbar(self.image_list_frame, orient="vertical", command=self.image_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.image_listbox.config(yscrollcommand=scrollbar.set)

        # Start Button
        ttk.Button(main, text="Prepare Compilation", command=self.preparation_phase).pack(pady=15)

        self.update_mode_state()

    def _mark_filename_as_custom(self):
        self.user_touched_filename = True

    def select_folder(self):
        folder = filedialog.askdirectory(title="Select Image Directory")

        if folder:
            self.path_var.set(folder)

            # Update only if the user has not touched the input!
            if not self.user_touched_filename:
                folder_name = os.path.basename(folder)
                self.filename_var.set(f"{folder_name}.pdf")

    def get_output_path(self):
        save_loc = self.save_location_var.get().strip()
        filename = self.filename_var.get().strip()

        # Same as source → use image directory
        if save_loc.lower() == "same as source":
            return os.path.join(self.path_var.get().strip(), filename)

        # Else → use user-selected location
        return os.path.join(save_loc, filename)

    def select_save_location(self):
        folder = filedialog.askdirectory(title="Select Save Location")
        if folder:
            self.save_location_var.set(folder)

        if not self.user_touched_filename:
            folder_name = os.path.basename(self.path_var.get().strip())
            if folder_name:
                self.filename_var.set(f"{folder_name}.pdf")

    def preparation_phase(self):
        if self.quick_compile():
            self.quick_compile()
        else:
            self.expert_compile()

    def update_mode_state(self):
        quick = self.app.quick_compile_enabled.get()
        dev = self.app.dev_mode_enabled.get()

        # DEV MODE OVERRIDES EVERYTHING
        if dev:
            for widget in [self.mode_frame, self.ppi_frame]:
                for child in widget.winfo_children():
                    child.configure(state="normal")
            return  # stop here, dev mode wins

        # QUICK MODE
        if quick:
            for widget in [self.mode_frame, self.ppi_frame]:
                for child in widget.winfo_children():
                    child.configure(state="disabled")

            # Autofill save location
            self.save_location_var.set("Same as source")

        else:
            # NORMAL MODE (unlock everything)
            for widget in [self.mode_frame, self.ppi_frame]:
                for child in widget.winfo_children():
                    child.configure(state="normal")

    def notify_mode_change(self):
        self.update_mode_state()
        self.update_idletasks()

    def quick_compile(self):
        path = self.path_var.get().strip()

        if not path or not os.path.isdir(path):
            messagebox.showerror("Invalid Directory", "Please select a valid folder.")
            return

        # Auto filename ONLY if user hasn't touched it
        if self.filename_var.get().strip().lower() in ["albumname.pdf", ""]:
            folder = os.path.basename(path)
            self.filename_var.set(f"{folder}.pdf")

        image_paths = collect_image_paths(path, self.filetype_var.get())
        if not image_paths:
            messagebox.showerror("No Images", f"No matching images found.")
            return

        # Save to same directory
        pdf_path = self.get_output_path()

        try:
            image_count = create_pdf_from_images(image_paths, pdf_path)
        except Exception as exc:
            messagebox.showerror("Error", f"Could not create PDF:\n{exc}")
            return
        messagebox.showinfo("Success", f"PDF created from {image_count} image(s):\n{pdf_path}")

    def expert_compile(self):
        self.preparation_checks()

    def preparation_checks(self):
        errors = []
        mode = self.mode_var.get()
        path = self.path_var.get().strip()
        filetype = self.filetype_var.get().lower()
        ppi = self.ppi_var.get()
        save_location = self.save_location_var.get().strip()
        filename = self.filename_var.get().strip()

        if not path or not os.path.isdir(path):
            errors.append("⚠️ Invalid or missing image directory.")

        matching_paths = collect_image_paths(path, filetype) if path and os.path.isdir(path) else []

        if not filetype:
            errors.append("⚠️ File type must be selected.")
        else:
            if filetype == "combine":
                # Check for ANY supported file
                has_supported = bool(matching_paths)
                if not has_supported:
                    errors.append("⚠️ No supported image files found for Combine mode.")
            else:
                # Normal single-type validation
                has_type = bool(matching_paths)
                if not has_type:
                    errors.append(f"⚠️ No .{filetype} files found in directory.")

        if ppi not in ["100", "200", "300", "400", "Original"]:
            errors.append("⚠️ Invalid PPI selection.")

        if save_location.lower() != "same as source" and not os.path.isdir(save_location):
            errors.append("⚠️ Save location invalid.")

        if not filename:
            errors.append("⚠️ Filename cannot be empty.")

        if errors:
            messagebox.showerror("Input Errors", "\n".join(errors))
        else:
            messagebox.showinfo("Preparation Successful", f"All inputs validated for expert compile.")

    def scan_images(self):
        path = self.path_var.get().strip()
        filetype = self.filetype_var.get().lower()

        if not path or not os.path.isdir(path):
            messagebox.showerror("Invalid Directory", "Please select a valid folder.")
            return
        matching_files = collect_image_paths(path, filetype)

        self.image_listbox.delete(0, tk.END)

        if not matching_files:
            messagebox.showwarning("No Files Found", f"No files found matching selection.")
            return

        for filename in matching_files:
            self.image_listbox.insert(tk.END, os.path.relpath(filename, path))

        messagebox.showinfo("Scan Complete", f"✅ Found {len(matching_files)} file(s).")

    def destroy(self):
        # Remove any traces we registered on app variables
        try:
            for var, mode, handler in getattr(self, "_traces", []):
                try:
                    var.trace_remove(mode, handler)
                except Exception:
                    # fallback: some Python/Tk versions return different handler names; ignore errors
                    pass
        except Exception:
            pass

        # Call parent destroy to actually remove widgets
        try:
            super().destroy()
        except Exception:
            # worst case: ensure no crash
            pass


class ImageGallery(ttk.Frame):
    def __init__(self, parent, image_paths):
        super().__init__(parent)
        self.image_paths = image_paths
        self.image_labels = []
        self.show_thumbnails = True  # start in thumbnail mode
        self.thumbnail_size = (120, 120)
        self.thumb_cache = {}

        # Create scrollable canvas
        self.canvas = tk.Canvas(self, borderwidth=0)
        self.scroll_y = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner_frame = ttk.Frame(self.canvas)

        self.inner_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll_y.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll_y.pack(side="right", fill="y")

        # Populate and bind zoom control
        self._populate_gallery()
        self.canvas.bind_all("<Control-MouseWheel>", self._on_ctrl_scroll)

    def _populate_gallery(self):
        # Clear current items
        for widget in self.inner_frame.winfo_children():
            widget.destroy()
        self.image_labels.clear()

        # Create thumbnails or filenames
        for idx, path in enumerate(self.image_paths):
            if self.show_thumbnails:
                thumb = self._get_thumbnail(path)
                lbl = ttk.Label(self.inner_frame, image=thumb, text=os.path.basename(path), compound="top", padding=5, relief="ridge")
                lbl.image = thumb
            else:
                lbl = ttk.Label(self.inner_frame, text=os.path.basename(path), padding=5, relief="ridge")

            lbl.grid(row=idx, column=0, sticky="ew", pady=3)
            lbl.bind("<Button-1>", self._on_drag_start)
            lbl.bind("<B1-Motion>", self._on_drag_motion)
            lbl.bind("<ButtonRelease-1>", self._on_drag_release)
            self.image_labels.append(lbl)

    def _get_thumbnail(self, path):
        if path not in self.thumb_cache:
            try:
                img = Image.open(path)
                img.thumbnail(self.thumbnail_size)
                self.thumb_cache[path] = ImageTk.PhotoImage(img)
            except Exception:  # fallback placeholder
                ph = Image.new("RGB", self.thumbnail_size, color=(180, 180, 180))
                self.thumb_cache[path] = ImageTk.PhotoImage(ph)
        return self.thumb_cache[path]

    def _on_ctrl_scroll(self, event):
        # Ctrl + scroll up/down toggles mode
        if event.delta > 0:
            self.show_thumbnails = True
        else:
            self.show_thumbnails = False
            self._populate_gallery()

    # --- Drag functionality ---

    def _on_drag_start(self, event):
        widget = event.widget
        self.drag_data = {"widget": widget, "y": event.y_root, "index": self.image_labels.index(widget)}

    def _on_drag_motion(self, event):
        delta_y = event.y_root - self.drag_data["y"]
        idx = self.drag_data["index"]
        new_idx = idx
        if delta_y < -20 and idx > 0:
            new_idx -= 1
        elif delta_y > 20 and idx < len(self.image_labels) - 1:
            new_idx += 1

        if new_idx != idx:
            self._swap_widgets(idx, new_idx)
            self.drag_data["index"] = new_idx
            self.drag_data["y"] = event.y_root

    def _on_drag_release(self, event):
        self.drag_data = {"widget": None, "y": 0, "index": None}

    def _swap_widgets(self, i, j):
        self.image_labels[i], self.image_labels[j] = self.image_labels[j], self.image_labels[i]
        self.image_paths[i], self.image_paths[j] = self.image_paths[j], self.image_paths[i]

        for idx, lbl in enumerate(self.image_labels):
            lbl.grid_configure(row=idx)

def get_module(parent, app=None):
    return PDFCompiler(parent, app)

