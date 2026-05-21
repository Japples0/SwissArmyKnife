import os
import re
import threading
import urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tempfile
import zipfile
from pathlib import Path

import requests
from bs4 import BeautifulSoup


class WebScraperModule(ttk.Frame):
    """
    Web Scraper / Media Downloader (framework-first):

    - Fetch a page (HTML)
    - Extract candidate media URLs shown/embedded by the page (images/videos)
      * <img src>, <source srcset>
      * <video src>, <source src> inside <video>
      * <a href> direct media links
      * raw media URLs found in HTML text (commonly inside inline scripts)
    - Download selected URLs
    - Optional: zip selected URLs after download
    """

    def __init__(self, parent, app=None):
        super().__init__(parent, padding=10)
        self.app = app

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "SwissArmyKnifeWebScraper/1.0 (+local tool)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        self._build_ui()

        # state
        self._items: list[dict] = []  # {url, kind, label}
        self._worker: threading.Thread | None = None

    def _build_ui(self):
        controls = ttk.LabelFrame(self, text="Media Scraper", padding=10)
        controls.pack(fill="x")

        self.url_var = tk.StringVar()
        self.regex_var = tk.StringVar(value=r".*")
        self.out_dir_var = tk.StringVar(value=os.path.join(os.getcwd(), "downloads"))
        self.timeout_var = tk.StringVar(value="20")
        self.zip_var = tk.BooleanVar(value=True)
        self.zip_name_var = tk.StringVar(value="media.zip")

        ttk.Label(controls, text="Page URL:").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.url_var).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="URL filter (regex):").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.regex_var).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Label(controls, text="Output folder:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.out_dir_var).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Button(controls, text="Browse…", command=self._browse_out_dir).grid(row=2, column=2, padx=(8, 0), pady=(8, 0))

        ttk.Label(controls, text="Timeout (s):").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.timeout_var, width=10).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        zip_row = ttk.Frame(controls)
        zip_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(zip_row, text="Zip downloads", variable=self.zip_var).pack(side="left")
        ttk.Label(zip_row, text="Zip name:").pack(side="left", padx=(12, 0))
        ttk.Entry(zip_row, textvariable=self.zip_name_var, width=24).pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(controls)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        self.preview_btn = ttk.Button(buttons, text="Preview Media", command=self.preview_media)
        self.preview_btn.grid(row=0, column=0, sticky="w")

        self.download_btn = ttk.Button(buttons, text="Download Selected", command=self.download_selected)
        self.download_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.clear_btn = ttk.Button(buttons, text="Clear", command=self._clear_results)
        self.clear_btn.grid(row=0, column=2, sticky="w", padx=(8, 0))

        results = ttk.LabelFrame(self, text="Detected media URLs", padding=10)
        results.pack(fill="both", expand=True, pady=(10, 0))

        self.listbox = tk.Listbox(results, selectmode="extended")
        self.listbox.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=scrollbar.set)

        log_frame = ttk.LabelFrame(self, text="Log", padding=10)
        log_frame.pack(fill="both", expand=False, pady=(10, 0))

        self.log_text = tk.Text(log_frame, height=8, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def notify_mode_change(self):
        return

    def _browse_out_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.out_dir_var.get() or os.getcwd())
        if chosen:
            self.out_dir_var.set(chosen)

    def _log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.preview_btn.configure(state=state)
        self.download_btn.configure(state=state)
        self.clear_btn.configure(state=state)

    def _clear_results(self):
        self._items = []
        self.listbox.delete(0, "end")
        self._log("Cleared results.")

    # -------------------- UI actions --------------------

    def preview_media(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a page URL.")
            return

        timeout = self._parse_timeout()
        if timeout is None:
            return

        pattern = self._compile_regex()
        if pattern is None:
            return

        self._set_busy(True)
        self._log(f"Fetching HTML: {url}")

        def work():
            try:
                html = self._fetch_html(url, timeout=timeout)
                items = self._extract_media_items(html, base_url=url, pattern=pattern)
                self.after(0, lambda: self._render_items(items))
            except Exception as e:
                self.after(0, lambda: self._log(f"Error: {e}"))
            finally:
                self.after(0, lambda: self._set_busy(False))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def download_selected(self):
        if not self._items:
            messagebox.showinfo("Nothing to download", "No results. Click 'Preview Media' first.")
            return

        selection = list(self.listbox.curselection())
        if not selection:
            messagebox.showinfo("No selection", "Select one or more items from the list.")
            return

        out_dir = self.out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("Missing output folder", "Please choose an output folder.")
            return

        timeout = self._parse_timeout()
        if timeout is None:
            return

        urls = [self._items[i]["url"] for i in selection]
        zip_enabled = bool(self.zip_var.get())
        zip_name = self.zip_name_var.get().strip() or "media.zip"

        self._set_busy(True)
        self._log(f"Downloading {len(urls)} item(s) to: {out_dir}")

        def work():
            try:
                os.makedirs(out_dir, exist_ok=True)

                if zip_enabled:
                    zip_path = self._download_urls_and_zip(urls, out_dir=out_dir, zip_name=zip_name, timeout=timeout)
                    self.after(0, lambda: self._log(f"Saved ZIP: {zip_path}"))
                else:
                    ok = 0
                    for u in urls:
                        try:
                            path = self._download_url(u, out_dir=out_dir, timeout=timeout)
                            ok += 1
                            self.after(0, lambda p=path: self._log(f"Saved: {p}"))
                        except Exception as e:
                            self.after(0, lambda uu=u, ee=e: self._log(f"Failed: {uu} ({ee})"))
                    self.after(0, lambda: self._log(f"Done. Successful: {ok}/{len(urls)}"))

            except Exception as e:
                self.after(0, lambda: self._log(f"Error: {e}"))
            finally:
                self.after(0, lambda: self._set_busy(False))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    # -------------------- Parsing / validation --------------------

    def _parse_timeout(self) -> float | None:
        try:
            return float(self.timeout_var.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid timeout", "Timeout must be a number (seconds).")
            return None

    def _compile_regex(self) -> re.Pattern | None:
        regex_text = self.regex_var.get().strip() or r".*"
        try:
            return re.compile(regex_text)
        except re.error as e:
            messagebox.showwarning("Invalid regex", f"Regex error: {e}")
            return None

    # -------------------- Core network helpers --------------------

    def _fetch_html(self, url: str, timeout: float) -> str:
        resp = self.session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text

    # -------------------- Media extraction --------------------

    def _extract_media_items(self, html: str, base_url: str, pattern: re.Pattern) -> list[dict]:
        """
        Returns items: {url, kind, label}
        kind in {"image","video","media"}
        """
        soup = BeautifulSoup(html, "html.parser")
        raw_urls: list[tuple[str, str]] = []  # (url, kind)

        # Images: <img src>
        for img in soup.select("img[src]"):
            src = (img.get("src") or "").strip()
            if src:
                raw_urls.append((urllib.parse.urljoin(base_url, src), "image"))

        # Images: <source srcset>
        for src in soup.select("source[srcset]"):
            srcset = (src.get("srcset") or "").strip()
            if not srcset:
                continue
            for part in srcset.split(","):
                candidate = part.strip().split(" ")[0]
                if candidate:
                    raw_urls.append((urllib.parse.urljoin(base_url, candidate), "image"))

        # Videos: <video src>
        for vid in soup.select("video[src]"):
            src = (vid.get("src") or "").strip()
            if src:
                raw_urls.append((urllib.parse.urljoin(base_url, src), "video"))

        # Videos: <video><source src>
        for src in soup.select("video source[src]"):
            s = (src.get("src") or "").strip()
            if s:
                raw_urls.append((urllib.parse.urljoin(base_url, s), "video"))

        # Direct media links in anchors
        media_ext = r"(?:jpg|jpeg|png|webp|gif|mp4|webm|mkv|mov|m4v)"
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            abs_url = urllib.parse.urljoin(base_url, href)
            if re.search(rf"\.{media_ext}(?:\?.*)?$", abs_url, flags=re.IGNORECASE):
                raw_urls.append((abs_url, "media"))

        # Raw media URLs embedded in the HTML (often in inline scripts)
        for m in re.finditer(rf"https?://[^\s\"'<>]+\.{media_ext}(?:\?[^\s\"'<>]+)?", html, flags=re.IGNORECASE):
            raw_urls.append((m.group(0), "media"))

        # Normalize, filter, de-duplicate preserving order
        items: list[dict] = []
        seen: set[str] = set()

        for u, kind in raw_urls:
            if not u.startswith(("http://", "https://")):
                continue
            if not pattern.search(u):
                continue
            if u in seen:
                continue
            seen.add(u)

            label = f"[{kind}] {u}"
            items.append({"url": u, "kind": kind, "label": label})

        return items

    def _render_items(self, items: list[dict]):
        self._items = items
        self.listbox.delete(0, "end")
        for it in items:
            self.listbox.insert("end", it["label"])
        self._log(f"Found {len(items)} media URL(s).")

    # -------------------- Downloading --------------------

    def _sanitize_filename(self, name: str, default: str) -> str:
        name = name.strip() or default
        name = re.sub(r"[^\w.\-() ]+", "_", name).strip()
        name = name[:180] if len(name) > 180 else name
        return name or default

    def _pick_filename_from_url(self, url: str, fallback: str) -> str:
        parsed = urllib.parse.urlparse(url)
        base = os.path.basename(parsed.path)
        if not base:
            return fallback
        # Drop very long query-derived names; keep path basename
        return base

    def _download_url(self, url: str, out_dir: str, timeout: float) -> str:
        os.makedirs(out_dir, exist_ok=True)

        # Prefer filename from URL; otherwise fallback
        fallback = "download.bin"
        name = self._pick_filename_from_url(url, fallback=fallback)
        name = self._sanitize_filename(name, default=fallback)

        out_path = os.path.join(out_dir, name)

        with self.session.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)

        return out_path

    def _download_urls_and_zip(self, urls: list[str], out_dir: str, zip_name: str, timeout: float) -> str:
        os.makedirs(out_dir, exist_ok=True)

        zip_name = self._sanitize_filename(zip_name, default="media.zip")
        if not zip_name.lower().endswith(".zip"):
            zip_name += ".zip"
        zip_path = os.path.join(out_dir, zip_name)

        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)

            # Download each URL to temp with stable numbering to preserve order
            for idx, url in enumerate(urls, start=1):
                parsed = urllib.parse.urlparse(url)
                ext = os.path.splitext(parsed.path)[1] or ".bin"
                ext = ext[:10]  # avoid pathological extensions
                filename = f"{idx:04d}{ext}"
                dest = temp_dir / filename

                with self.session.get(url, stream=True, timeout=timeout) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                f.write(chunk)

            # Zip
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(temp_dir.iterdir()):
                    zf.write(p, arcname=p.name)

        return zip_path


def get_module(parent, app=None):
    return WebScraperModule(parent, app=app)