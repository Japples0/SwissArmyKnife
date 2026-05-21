import os
import shutil
import tempfile
import zipfile
from tkinter import filedialog, messagebox
from PIL import Image
from natsort import natsorted
import tkinter as tk
from tkinter import ttk
import traceback
import time
import threading


SUPPORTED_IMAGE_EXTS = ["jpg", "jpeg", "png", "webp", "heic", "heif"]


def extract_zip_to_temp(zip_path: str) -> str:
    """Extract ZIP into a temporary directory and return the directory path."""
    temp_dir = tempfile.mkdtemp(prefix="zip_extract_")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(temp_dir)
    return temp_dir


def collect_images(temp_dir: str) -> list:
    """Walk the extracted ZIP folder and return sorted image paths."""
    images = []

    for root, dirs, files in os.walk(temp_dir):
        for f in files:
            ext = f.split(".")[-1].lower()
            if ext in SUPPORTED_IMAGE_EXTS:
                images.append(os.path.join(root, f))

    return natsorted(images)


def compile_pdf_from_images(image_paths: list, output_path: str):
    """Compile PDF from a list of images."""
    if not image_paths:
        raise ValueError("No images were found inside the ZIP file.")

    pil_images = []

    for img_path in image_paths:
        img = Image.open(img_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        pil_images.append(img)

    first = pil_images[0]
    rest = pil_images[1:]
    first.save(output_path, save_all=True, append_images=rest)


def run_zip_to_pdf_with_ui(frame):
    """Batch-process all ZIPs with live UI updates (fixed)."""
    folder = filedialog.askdirectory(title="Select folder containing ZIP files")
    if not folder:
        return

    completed_zips = os.path.join(folder, "COMPLETED ZIPS")
    completed_pdfs = os.path.join(folder, "COMPLETED PDFS")
    os.makedirs(completed_zips, exist_ok=True)
    os.makedirs(completed_pdfs, exist_ok=True)

    # Get list of full-path zip files
    zips = [
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(".zip")
    ]

    if not zips:
        messagebox.showerror("No ZIPs Found", "The selected folder has no ZIP files.")
        return

    total = len(zips)
    frame.progress["maximum"] = total
    frame.progress["value"] = 0

    zip_stats = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "corrupt": [],
        "time_per_zip": {}
    }

    for idx, zip_path in enumerate(zips, start=1):
        start_time = time.time()
        zip_name_only = os.path.splitext(os.path.basename(zip_path))[0]

        # UI update
        frame.status_label.config(text=f"Processing {os.path.basename(zip_path)} ({idx}/{total})")
        frame.progress["value"] = idx - 1
        frame.update_idletasks()

        temp_dir = None
        try:
            # Extract to a temp dir (uses your helper)
            temp_dir = extract_zip_to_temp(zip_path)

            # Collect images from extracted folder (uses your helper)
            images = collect_images(temp_dir)
            if not images:
                raise Exception("No images found in zip — nothing to process.")

            # Compile into PDF named after zip
            pdf_temp_path = os.path.join(temp_dir, f"{zip_name_only}.pdf")
            compile_pdf_from_images(images, pdf_temp_path)

            # Move outputs
            final_pdf_path = os.path.join(completed_pdfs, f"{zip_name_only}.pdf")
            shutil.move(pdf_temp_path, final_pdf_path)
            shutil.move(zip_path, os.path.join(completed_zips, os.path.basename(zip_path)))

            # Stats update
            elapsed = round(time.time() - start_time, 2)
            zip_stats["processed"] += 1
            zip_stats["success"] += 1
            zip_stats["time_per_zip"][zip_name_only] = elapsed

            print(f"✓ Finished {zip_name_only} in {elapsed}s")

        except Exception as e:
            # Record failure, but continue
            print(f"✗ ERROR processing {zip_name_only}: {str(e)}")
            zip_stats["processed"] += 1
            zip_stats["failed"] += 1
            zip_stats["corrupt"].append(zip_name_only)
            traceback.print_exc()

        finally:
            # Clean temp folder safely
            if temp_dir and os.path.isdir(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass

    # Final UI update
    frame.progress["value"] = total
    frame.status_label.config(text="✔ All ZIPs processed successfully!")
    messagebox.showinfo("Completed", f"All ZIPs processed.\nProcessed: {zip_stats['processed']}  Failed: {zip_stats['failed']}")


    def ui_log(frame, message):
        frame.log_box.config(state="normal")
        frame.log_box.insert("end", message + "\n")
        frame.log_box.see("end")
        frame.log_box.config(state="disabled")

    ui_log(frame, "\n=== BATCH COMPLETE ===")
    ui_log(frame, f"Total ZIPs processed: {zip_stats['processed']}")
    ui_log(frame, f"Successful:          {zip_stats['success']}")
    ui_log(frame, f"Failed/Corrupt:      {zip_stats['failed']}")

    if zip_stats["failed"] > 0:
        print("\nCorrupt/Failed ZIPs:")
        for z in zip_stats["corrupt"]:
            print(" -", z)

    print("\nPer-ZIP timing:")
    for name, t in zip_stats["time_per_zip"].items():
        print(f" {name}: {t}s")

class ZipToPdfFrame(ttk.Frame):
    """The actual UI frame for your module."""
    def __init__(self, parent, app=None):
        super().__init__(parent)

        ttk.Label(
            self,
            text="ZIP → PDF Compiler",
            font=("Segoe UI", 14, "bold")
        ).pack(pady=10)

        ttk.Button(
            self,
            text="Batch: Process All ZIPs in Folder",
            command=lambda: threading.Thread(
                target=run_zip_to_pdf_with_ui,
                args=(self,),
                daemon=True
            ).start()
        ).pack(pady=20)

        self.status_label = ttk.Label(self, text="Idle", font=("Segoe UI", 10))
        self.status_label.pack(pady=(10, 0))

        self.log_box = tk.Text(self, height=10, width=60, state="disabled")
        self.log_box.pack(pady=(10, 0))

        self.progress = ttk.Progressbar(self, length=300, mode="determinate")
        self.progress.pack(pady=5)


    def notify_mode_change(self):
        """Called whenever Quick Compile / Dev Mode changes.
        (You can modify behaviour later if needed)
        """
        pass

def get_module(parent, app=None):
    """Required entry point for the SwissArmyKnife framework."""
    return ZipToPdfFrame(parent, app=app)