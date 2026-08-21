from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import re
import tempfile
import threading
import tkinter as tk
import urllib.parse
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import requests
from bs4 import BeautifulSoup


class SequenceCancelled(Exception):
    """Raised internally when the user stops a sequential operation."""


class WebScraperModule(ttk.Frame):
    """Extract and download media from a single web page.

    The generic scan keeps the original broad media discovery behaviour. The
    structured mode is deliberately narrower: it selects one container, walks
    its matching items in DOM order, and extracts one image from each item.
    """

    GENERIC_MODE = "Generic scan"
    STRUCTURED_MODE = "Structured images"
    SEQUENCE_MODE = "Sequential chapters"
    MAX_ITEM_DIAGNOSTICS = 50

    def __init__(self, parent, app=None):
        super().__init__(parent, padding=10)
        self.app = app

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.9",
            }
        )

        self._items: list[dict] = []
        self._worker: threading.Thread | None = None
        self._ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._destroyed = False
        self._last_raw_response: bytes | None = None
        self._last_response_url = ""
        self._sequence_chapters: list[dict] = []
        self._sequence_signature: tuple | None = None
        self._cancel_event = threading.Event()

        self._build_ui()
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.after(100, self._process_ui_queue)

    # -------------------- UI construction --------------------

    def _build_ui(self):
        controls = ttk.LabelFrame(self, text="Web Media Extractor", padding=10)
        controls.pack(fill="x")
        controls.columnconfigure(1, weight=1)

        self.mode_var = tk.StringVar(value=self.GENERIC_MODE)
        self.url_var = tk.StringVar()
        self.regex_var = tk.StringVar(value=r".*")
        self.container_var = tk.StringVar(value="#chapter_boxImages")
        self.item_var = tk.StringVar(value=".image_story.imageChap")
        self.image_var = tk.StringVar(value="img")
        self.next_selector_var = tk.StringVar(value="a.next-chapter, a[rel='next']")
        self.next_script_var = tk.StringVar(value="next_chapter")
        self.max_chapters_var = tk.StringVar(value="50")
        self.out_dir_var = tk.StringVar(value=os.path.join(os.getcwd(), "downloads"))
        self.timeout_var = tk.StringVar(value="20")
        self.zip_var = tk.BooleanVar(value=True)
        self.zip_name_var = tk.StringVar(value="media.zip")

        ttk.Label(controls, text="Mode:").grid(row=0, column=0, sticky="w")
        self.mode_combo = ttk.Combobox(
            controls,
            textvariable=self.mode_var,
            values=(self.GENERIC_MODE, self.STRUCTURED_MODE, self.SEQUENCE_MODE),
            state="readonly",
            width=22,
        )
        self.mode_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)

        ttk.Label(controls, text="Page URL:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.url_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0)
        )

        self.mode_options = ttk.Frame(controls)
        self.mode_options.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.mode_options.columnconfigure(0, weight=1)

        self.generic_options = ttk.Frame(self.mode_options)
        self.generic_options.grid(row=0, column=0, sticky="ew")
        self.generic_options.columnconfigure(1, weight=1)
        ttk.Label(self.generic_options, text="URL filter (regex):").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.generic_options, textvariable=self.regex_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )

        self.structured_options = ttk.Frame(self.mode_options)
        self.structured_options.columnconfigure(1, weight=1)
        selector_rows = (
            ("Container selector:", self.container_var),
            ("Item selector:", self.item_var),
            ("Image selector:", self.image_var),
        )
        for row, (label, variable) in enumerate(selector_rows):
            ttk.Label(self.structured_options, text=label).grid(
                row=row, column=0, sticky="w", pady=(4 if row else 0, 0)
            )
            ttk.Entry(self.structured_options, textvariable=variable).grid(
                row=row,
                column=1,
                sticky="ew",
                padx=(8, 0),
                pady=(4 if row else 0, 0),
            )

        self.sequence_options = ttk.Frame(self.mode_options)
        self.sequence_options.columnconfigure(1, weight=1)
        sequence_rows = (
            ("Container selector:", self.container_var),
            ("Item selector:", self.item_var),
            ("Image selector:", self.image_var),
            ("Next chapter selector:", self.next_selector_var),
            ("Next URL variable (optional):", self.next_script_var),
            ("Maximum chapters:", self.max_chapters_var),
        )
        for row, (label, variable) in enumerate(sequence_rows):
            ttk.Label(self.sequence_options, text=label).grid(
                row=row, column=0, sticky="w", pady=(4 if row else 0, 0)
            )
            ttk.Entry(self.sequence_options, textvariable=variable).grid(
                row=row,
                column=1,
                sticky="ew",
                padx=(8, 0),
                pady=(4 if row else 0, 0),
            )

        ttk.Label(controls, text="Output folder:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(controls, textvariable=self.out_dir_var).grid(
            row=3, column=1, sticky="ew", padx=(8, 0), pady=(8, 0)
        )
        ttk.Button(controls, text="Browse...", command=self._browse_out_dir).grid(
            row=3, column=2, padx=(8, 0), pady=(8, 0)
        )

        options_row = ttk.Frame(controls)
        options_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(options_row, text="Timeout (s):").pack(side="left")
        ttk.Entry(options_row, textvariable=self.timeout_var, width=8).pack(side="left", padx=(8, 16))
        self.zip_options = ttk.Frame(options_row)
        self.zip_options.pack(side="left")
        ttk.Checkbutton(self.zip_options, text="Zip downloads", variable=self.zip_var).pack(side="left")
        ttk.Label(self.zip_options, text="Zip name:").pack(side="left", padx=(12, 0))
        ttk.Entry(self.zip_options, textvariable=self.zip_name_var, width=22).pack(
            side="left", padx=(8, 0)
        )

        buttons = ttk.Frame(controls)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.single_buttons = ttk.Frame(buttons)
        self.single_buttons.grid(row=0, column=0, sticky="w")
        self.preview_btn = ttk.Button(
            self.single_buttons, text="Preview / Analyse", command=self.preview_media
        )
        self.preview_btn.pack(side="left")
        self.save_html_btn = ttk.Button(
            self.single_buttons, text="Save Raw HTML", command=self.save_raw_html, state="disabled"
        )
        self.save_html_btn.pack(side="left", padx=(8, 0))
        self.download_btn = ttk.Button(
            self.single_buttons, text="Download Selected", command=self.download_selected
        )
        self.download_btn.pack(side="left", padx=(8, 0))
        self.download_all_btn = ttk.Button(
            self.single_buttons, text="Download All", command=self.download_all
        )
        self.download_all_btn.pack(side="left", padx=(8, 0))
        self.clear_btn = ttk.Button(self.single_buttons, text="Clear", command=self._clear_results)
        self.clear_btn.pack(side="left", padx=(8, 0))

        self.sequence_buttons = ttk.Frame(buttons)
        self.sequence_analyse_btn = ttk.Button(
            self.sequence_buttons, text="Analyse Sequence", command=self.analyse_sequence
        )
        self.sequence_analyse_btn.pack(side="left")
        self.sequence_download_btn = ttk.Button(
            self.sequence_buttons,
            text="Start Sequential Download",
            command=self.start_sequential_download,
        )
        self.sequence_download_btn.pack(side="left", padx=(8, 0))
        self.sequence_stop_btn = ttk.Button(
            self.sequence_buttons, text="Stop", command=self.stop_sequence, state="disabled"
        )
        self.sequence_stop_btn.pack(side="left", padx=(8, 0))

        results = ttk.LabelFrame(self, text="Results", padding=10)
        results.pack(fill="both", expand=True, pady=(10, 0))
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)

        columns = ("index", "filename", "type", "url", "status")
        self.tree = ttk.Treeview(results, columns=columns, show="headings", selectmode="extended")
        headings = {
            "index": "Index",
            "filename": "Filename",
            "type": "Type",
            "url": "URL",
            "status": "Status",
        }
        widths = {"index": 60, "filename": 210, "type": 80, "url": 460, "status": 130}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=50, stretch=column == "url")
        self.tree.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(results, orient="vertical", command=self.tree.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(results, orient="horizontal", command=self.tree.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        log_frame = ttk.LabelFrame(self, text="Log", padding=10)
        log_frame.pack(fill="x", pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=7, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def notify_mode_change(self):
        """Compatibility hook used by main.py's application mode menu."""
        return

    def _on_mode_changed(self, _event=None):
        mode = self.mode_var.get()
        if mode == self.SEQUENCE_MODE:
            self.generic_options.grid_remove()
            self.structured_options.grid_remove()
            self.sequence_options.grid(row=0, column=0, sticky="ew")
            self.single_buttons.grid_remove()
            self.sequence_buttons.grid(row=0, column=0, sticky="w")
            self.zip_options.pack_forget()
        elif mode == self.STRUCTURED_MODE:
            self.generic_options.grid_remove()
            self.sequence_options.grid_remove()
            self.structured_options.grid(row=0, column=0, sticky="ew")
            self.sequence_buttons.grid_remove()
            self.single_buttons.grid(row=0, column=0, sticky="w")
            if not self.zip_options.winfo_manager():
                self.zip_options.pack(side="left")
        else:
            self.structured_options.grid_remove()
            self.sequence_options.grid_remove()
            self.generic_options.grid(row=0, column=0, sticky="ew")
            self.sequence_buttons.grid_remove()
            self.single_buttons.grid(row=0, column=0, sticky="w")
            if not self.zip_options.winfo_manager():
                self.zip_options.pack(side="left")
        self._clear_results(log=False)
        self._log(f"Mode changed to: {mode}")

    def _browse_out_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.out_dir_var.get() or os.getcwd())
        if chosen:
            self.out_dir_var.set(chosen)

    def _store_raw_response(self, raw_bytes: bytes, final_url: str):
        self._last_raw_response = raw_bytes
        self._last_response_url = final_url

    def _clear_raw_response(self):
        self._last_raw_response = None
        self._last_response_url = ""
        self.save_html_btn.configure(state="disabled")

    def _log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for button in (self.preview_btn, self.download_btn, self.download_all_btn, self.clear_btn):
            button.configure(state=state)
        self.sequence_analyse_btn.configure(state=state)
        self.sequence_download_btn.configure(state=state)
        stop_state = "normal" if busy and self.mode_var.get() == self.SEQUENCE_MODE else "disabled"
        self.sequence_stop_btn.configure(state=stop_state)
        raw_state = "normal" if not busy and self._last_raw_response is not None else "disabled"
        self.save_html_btn.configure(state=raw_state)
        self.mode_combo.configure(state="disabled" if busy else "readonly")

    def _clear_results(self, log: bool = True):
        self._items = []
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        if log:
            self._log("Cleared results.")

    # -------------------- Thread-to-UI communication --------------------

    def _on_destroy(self, event):
        if event.widget is self:
            self._destroyed = True
            self._cancel_event.set()

    def _post_ui(self, action: str, payload: object = None):
        """Workers enqueue data; only the Tk thread consumes and touches widgets."""
        if not self._destroyed:
            self._ui_queue.put((action, payload))

    def _process_ui_queue(self):
        if self._destroyed:
            return
        try:
            while True:
                action, payload = self._ui_queue.get_nowait()
                if action == "log":
                    self._log(str(payload))
                elif action == "render":
                    self._render_items(payload)  # type: ignore[arg-type]
                elif action == "busy":
                    self._set_busy(bool(payload))
                elif action == "status":
                    row_id, status = payload  # type: ignore[misc]
                    self._set_item_status(str(row_id), str(status))
                elif action == "raw_response":
                    raw_bytes, final_url = payload  # type: ignore[misc]
                    self._store_raw_response(raw_bytes, str(final_url))
                elif action == "sequence_result":
                    chapters, signature = payload  # type: ignore[misc]
                    self._sequence_chapters = chapters
                    self._sequence_signature = signature
                    self._render_sequence_results(chapters)
        except queue.Empty:
            pass
        self.after(100, self._process_ui_queue)

    def _start_worker(self, target: Callable[[], None]):
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=target, daemon=True)
        self._worker.start()

    # -------------------- UI actions --------------------

    def preview_media(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a page URL.")
            return

        timeout = self._parse_timeout()
        if timeout is None:
            return

        mode = self.mode_var.get()
        pattern = None
        selectors: tuple[str, str, str] | None = None
        if mode == self.STRUCTURED_MODE:
            selectors = (
                self.container_var.get().strip(),
                self.item_var.get().strip(),
                self.image_var.get().strip(),
            )
            if not all(selectors):
                messagebox.showwarning("Missing selector", "All three CSS selectors are required.")
                return
        else:
            pattern = self._compile_regex()
            if pattern is None:
                return

        self._set_busy(True)
        self._clear_results(log=False)
        self._clear_raw_response()
        if mode == self.STRUCTURED_MODE:
            self._log(f"[Structured] Fetching: {url}")
        else:
            self._log(f"Fetching HTML: {url}")

        def work():
            try:
                html, final_url = self._fetch_page(
                    url,
                    timeout,
                    log=lambda text: self._post_ui("log", text),
                    structured=mode == self.STRUCTURED_MODE,
                    capture_raw=lambda raw, response_url: self._post_ui(
                        "raw_response", (raw, response_url)
                    ),
                )
                if mode == self.STRUCTURED_MODE and selectors is not None:
                    self._log_raw_presence_checks(html)
                    items = self._extract_structured_images(
                        html,
                        base_url=final_url,
                        container_selector=selectors[0],
                        item_selector=selectors[1],
                        image_selector=selectors[2],
                        log=lambda text: self._post_ui("log", text),
                    )
                    if not items:
                        self._post_ui(
                            "log",
                            "[Structured] DOM scan returned no images; checking inline scripts "
                            "for explicitly embedded image URLs...",
                        )
                        items = self._extract_embedded_script_images(
                            html,
                            log=lambda text: self._post_ui("log", text),
                        )
                else:
                    items = self._extract_media_items(html, base_url=final_url, pattern=pattern)
                    items = self._prepare_generic_items(items)
                self._post_ui("render", items)
            except Exception as exc:
                self._post_ui("log", f"Preview failed: {exc}")
            finally:
                self._post_ui("busy", False)

        self._start_worker(work)

    def save_raw_html(self):
        if self._last_raw_response is None:
            messagebox.showinfo("No response available", "Run Preview / Analyse before saving HTML.")
            return

        parsed = urllib.parse.urlparse(self._last_response_url)
        host = self._sanitize_filename(parsed.netloc, "page")
        suggested_name = f"raw_response_{host}.html"
        destination = filedialog.asksaveasfilename(
            title="Save exact HTTP response",
            defaultextension=".html",
            initialfile=suggested_name,
            filetypes=(("HTML files", "*.html *.htm"), ("All files", "*.*")),
        )
        if not destination:
            return

        raw_snapshot = self._last_raw_response
        self._set_busy(True)

        def work():
            try:
                with open(destination, "wb") as output:
                    output.write(raw_snapshot)
                self._post_ui(
                    "log", f"Saved exact raw HTTP response ({len(raw_snapshot):,} bytes): {destination}"
                )
            except Exception as exc:
                self._post_ui("log", f"Could not save raw HTML: {exc}")
            finally:
                self._post_ui("busy", False)

        self._start_worker(work)

    # -------------------- Sequential chapters --------------------

    def analyse_sequence(self):
        configuration = self._sequence_configuration(require_output=False)
        if configuration is not None:
            self._begin_sequence(configuration, download=False)

    def start_sequential_download(self):
        configuration = self._sequence_configuration(require_output=True)
        if configuration is not None:
            self._begin_sequence(configuration, download=True)

    def stop_sequence(self):
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
            self._log("[Sequence] Stop requested; finishing the current safe operation...")

    def _sequence_configuration(self, require_output: bool) -> dict | None:
        start_url = self.url_var.get().strip()
        selectors = (
            self.container_var.get().strip(),
            self.item_var.get().strip(),
            self.image_var.get().strip(),
        )
        next_selector = self.next_selector_var.get().strip()
        next_script_variable = self.next_script_var.get().strip()
        output_directory = self.out_dir_var.get().strip()

        if not start_url:
            messagebox.showwarning("Missing URL", "Please enter the first chapter URL.")
            return None
        if not all(selectors):
            messagebox.showwarning("Missing selector", "All structured image selectors are required.")
            return None
        if not next_selector and not next_script_variable:
            messagebox.showwarning(
                "Missing traversal rule",
                "Enter a next-chapter selector or an inline next-URL variable name.",
            )
            return None
        if require_output and not output_directory:
            messagebox.showwarning("Missing output folder", "Please choose an output folder.")
            return None
        try:
            maximum_chapters = int(self.max_chapters_var.get().strip())
            if maximum_chapters <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid maximum", "Maximum chapters must be a positive integer.")
            return None
        timeout = self._parse_timeout()
        if timeout is None:
            return None

        return {
            "start_url": start_url,
            "container_selector": selectors[0],
            "item_selector": selectors[1],
            "image_selector": selectors[2],
            "next_selector": next_selector,
            "next_script_variable": next_script_variable,
            "maximum_chapters": maximum_chapters,
            "output_directory": output_directory,
            "timeout": timeout,
        }

    def _sequence_signature_for(self, configuration: dict) -> tuple:
        return (
            self._normalise_visit_url(configuration["start_url"]),
            configuration["container_selector"],
            configuration["item_selector"],
            configuration["image_selector"],
            configuration["next_selector"],
            configuration["next_script_variable"],
            configuration["maximum_chapters"],
        )

    def _begin_sequence(self, configuration: dict, download: bool):
        signature = self._sequence_signature_for(configuration)
        use_cached_analysis = (
            download
            and bool(self._sequence_chapters)
            and self._sequence_signature == signature
        )

        self._cancel_event.clear()
        self._set_busy(True)
        self._clear_results(log=False)
        operation = "download" if download else "analysis"
        self._log(f"[Sequence] Starting sequence {operation}.")

        def work():
            chapters: list[dict] = []
            try:
                if use_cached_analysis:
                    self._sequence_log(
                        f"Using cached analysis for {len(self._sequence_chapters)} chapter(s); "
                        "chapter pages will not be fetched again."
                    )
                    chapters = [
                        {
                            **chapter,
                            "images": [dict(image) for image in chapter["images"]],
                        }
                        for chapter in self._sequence_chapters
                    ]
                    self._download_cached_sequence(chapters, configuration)
                else:
                    chapters = self._crawl_sequence(configuration, download=download)
                self._post_ui("sequence_result", (chapters, signature))
            except Exception as exc:
                self._sequence_log(f"Sequence stopped by an unexpected error: {exc}")
            finally:
                self._post_ui("busy", False)

        self._start_worker(work)

    def _sequence_log(self, message: str):
        self._post_ui("log", f"[Sequence] {message}")

    def _crawl_sequence(self, configuration: dict, download: bool) -> list[dict]:
        chapters: list[dict] = []
        visited_urls: set[str] = set()
        current_url = configuration["start_url"]
        manifest = None
        final_status = "complete"

        if download:
            os.makedirs(configuration["output_directory"], exist_ok=True)
            manifest = {
                "source": configuration["start_url"],
                "status": "in_progress",
                "chapters": chapters,
            }
            self._write_manifest(configuration["output_directory"], manifest)

        try:
            for chapter_index in range(1, configuration["maximum_chapters"] + 1):
                self._raise_if_cancelled()
                requested_key = self._normalise_visit_url(current_url)
                if requested_key in visited_urls:
                    final_status = "loop_detected"
                    self._sequence_log(f"Already visited {current_url}; stopping to prevent a loop.")
                    break

                self._sequence_log(f"Chapter {chapter_index}")
                self._sequence_log(f"URL: {current_url}")
                try:
                    html, final_url = self._fetch_page(
                        current_url,
                        configuration["timeout"],
                        log=lambda text: self._sequence_log(text),
                    )
                except Exception as exc:
                    final_status = "fetch_error"
                    self._sequence_log(f"Chapter fetch failed: {exc}")
                    break

                final_key = self._normalise_visit_url(final_url)
                if final_key in visited_urls:
                    final_status = "loop_detected"
                    self._sequence_log(
                        f"Final URL was already visited ({final_url}); stopping to prevent a loop."
                    )
                    break
                visited_urls.add(final_key)

                try:
                    chapter = self._scrape_sequence_chapter(
                        html, final_url, chapter_index, configuration
                    )
                except Exception as exc:
                    final_status = "extraction_error"
                    self._sequence_log(f"Chapter extraction failed: {exc}")
                    break

                chapters.append(chapter)
                self._sequence_log(f"Found {len(chapter['images'])} images")
                if manifest is not None:
                    self._write_manifest(configuration["output_directory"], manifest)
                    if not self._download_sequence_chapter(
                        chapter, configuration, manifest
                    ):
                        final_status = "download_error"
                        break

                self._raise_if_cancelled()
                next_url, stop_reason = self._discover_next_chapter(
                    html,
                    final_url,
                    configuration["next_selector"],
                    configuration["next_script_variable"],
                )
                if not next_url:
                    self._sequence_log(stop_reason)
                    break
                if self._normalise_visit_url(next_url) in visited_urls:
                    final_status = "loop_detected"
                    self._sequence_log(
                        f"Next chapter URL was already visited ({next_url}); stopping to prevent a loop."
                    )
                    break
                current_url = next_url
            else:
                final_status = "maximum_reached"
                self._sequence_log(
                    f"Configured maximum of {configuration['maximum_chapters']} chapters reached."
                )
        except SequenceCancelled:
            final_status = "cancelled"
            self._sequence_log("Cancelled by user.")
        finally:
            if manifest is not None:
                manifest["status"] = final_status
                manifest["completed_chapters"] = sum(
                    1 for chapter in chapters if chapter.get("status") == "complete"
                )
                self._write_manifest(configuration["output_directory"], manifest)

        self._sequence_log(f"Sequence finished after {len(chapters)} chapter(s): {final_status}.")
        return chapters

    def _scrape_sequence_chapter(
        self, html: str, final_url: str, chapter_index: int, configuration: dict
    ) -> dict:
        items = self._extract_structured_images(
            html,
            base_url=final_url,
            container_selector=configuration["container_selector"],
            item_selector=configuration["item_selector"],
            image_selector=configuration["image_selector"],
            log=lambda text: self._post_ui("log", text),
        )
        if not items:
            self._sequence_log("DOM extraction was empty; checking embedded script data.")
            items = self._extract_embedded_script_images(
                html, log=lambda text: self._post_ui("log", text)
            )
        if not items:
            raise ValueError("no chapter images were found")

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else f"Chapter {chapter_index}"
        images = [
            {
                "page_index": page_index,
                "source_url": item["url"],
                "status": "ready",
                "local_path": None,
            }
            for page_index, item in enumerate(items, start=1)
        ]
        return {
            "chapter_index": chapter_index,
            "title": title,
            "url": final_url,
            "status": "ready",
            "images": images,
        }

    def _discover_next_chapter(
        self,
        html: str,
        current_url: str,
        next_selector: str,
        next_script_variable: str,
    ) -> tuple[str | None, str]:
        soup = BeautifulSoup(html, "html.parser")
        if next_selector:
            try:
                next_element = soup.select_one(next_selector)
            except Exception as exc:
                return None, f"Next-chapter selector error: {exc}"
            if next_element is not None:
                for attribute in ("href", "data-href", "data-url", "value"):
                    candidate = next_element.get(attribute)
                    if not isinstance(candidate, str) or not candidate.strip():
                        continue
                    next_url = urllib.parse.urljoin(current_url, candidate.strip())
                    if urllib.parse.urlparse(next_url).scheme.lower() in ("http", "https"):
                        return next_url, f"Next chapter URL found in '{attribute}'."

        if next_script_variable:
            variable_pattern = re.compile(
                rf"(?:var|let|const)?\s*{re.escape(next_script_variable)}\s*=\s*"
                r"(?P<quote>['\"])(?P<url>.*?)(?P=quote)",
                re.DOTALL,
            )
            for script in soup.find_all("script"):
                script_text = script.string or script.get_text()
                match = variable_pattern.search(script_text or "")
                if not match:
                    continue
                candidate = self._decode_simple_javascript_string(match.group("url"))
                if not candidate.strip():
                    continue
                next_url = urllib.parse.urljoin(current_url, candidate)
                if urllib.parse.urlparse(next_url).scheme.lower() in ("http", "https"):
                    self._sequence_log(
                        f"Next chapter URL read from inline variable '{next_script_variable}'."
                    )
                    return next_url, "Next chapter URL found in inline script data."

        if next_selector:
            return None, f"No usable next chapter link found for selector '{next_selector}'."
        return None, "No next chapter URL was found."

    def _decode_simple_javascript_string(self, value: str) -> str:
        value = value.replace(r"\/", "/")
        value = re.sub(
            r"\\x([0-9a-fA-F]{2})",
            lambda match: chr(int(match.group(1), 16)),
            value,
        )
        return re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            value,
        )

    def _normalise_visit_url(self, url: str) -> str:
        clean_url, _fragment = urllib.parse.urldefrag(url.strip())
        parsed = urllib.parse.urlsplit(clean_url)
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
        )

    def _raise_if_cancelled(self):
        if self._cancel_event.is_set():
            raise SequenceCancelled

    def _download_cached_sequence(self, chapters: list[dict], configuration: dict):
        output_directory = configuration["output_directory"]
        os.makedirs(output_directory, exist_ok=True)
        manifest = {
            "source": configuration["start_url"],
            "status": "in_progress",
            "chapters": chapters,
        }
        self._write_manifest(output_directory, manifest)
        final_status = "complete"
        try:
            for chapter in chapters:
                self._raise_if_cancelled()
                self._sequence_log(f"Chapter {chapter['chapter_index']}: cached analysis")
                if not self._download_sequence_chapter(chapter, configuration, manifest):
                    final_status = "download_error"
                    break
        except SequenceCancelled:
            final_status = "cancelled"
            self._sequence_log("Cancelled by user.")
        finally:
            manifest["status"] = final_status
            manifest["completed_chapters"] = sum(
                1 for chapter in chapters if chapter.get("status") == "complete"
            )
            self._write_manifest(output_directory, manifest)
        self._sequence_log(f"Sequential download finished: {final_status}.")

    def _download_sequence_chapter(
        self, chapter: dict, configuration: dict, manifest: dict
    ) -> bool:
        chapter_width = max(3, len(str(configuration["maximum_chapters"])))
        chapter_directory = os.path.join(
            configuration["output_directory"],
            f"chapter_{chapter['chapter_index']:0{chapter_width}d}",
        )
        os.makedirs(chapter_directory, exist_ok=True)
        page_width = max(3, len(str(len(chapter["images"]))))

        for image in chapter["images"]:
            self._raise_if_cancelled()
            page_index = image["page_index"]
            self._sequence_log(
                f"Downloading page {page_index} / {len(chapter['images'])}"
            )
            existing_path = self._find_existing_sequence_page(
                chapter_directory, page_index, page_width
            )
            if existing_path:
                image["local_path"] = self._manifest_relative_path(
                    configuration["output_directory"], existing_path
                )
                image["status"] = "skipped_existing"
                self._sequence_log(f"Skipped existing file: {existing_path}")
                self._write_manifest(configuration["output_directory"], manifest)
                continue

            part_path = None
            try:
                with self.session.get(
                    image["source_url"], stream=True, timeout=configuration["timeout"]
                ) as response:
                    response.raise_for_status()
                    extension = self._sequence_image_extension(
                        image["source_url"], response.headers.get("Content-Type", "")
                    )
                    final_path = os.path.join(
                        chapter_directory, f"page_{page_index:0{page_width}d}{extension}"
                    )
                    part_path = final_path + ".part"
                    with open(part_path, "wb") as output:
                        for chunk in response.iter_content(chunk_size=1024 * 256):
                            self._raise_if_cancelled()
                            if chunk:
                                output.write(chunk)
                    os.replace(part_path, final_path)
                image["local_path"] = self._manifest_relative_path(
                    configuration["output_directory"], final_path
                )
                image["status"] = "downloaded"
                self._write_manifest(configuration["output_directory"], manifest)
            except SequenceCancelled:
                if part_path and os.path.exists(part_path):
                    os.remove(part_path)
                raise
            except Exception as exc:
                if part_path and os.path.exists(part_path):
                    os.remove(part_path)
                image["status"] = "error"
                image["error"] = str(exc)
                chapter["status"] = "error"
                self._write_manifest(configuration["output_directory"], manifest)
                self._sequence_log(f"Page {page_index} failed: {exc}")
                return False

        chapter["status"] = "complete"
        self._write_manifest(configuration["output_directory"], manifest)
        self._sequence_log(f"Completed chapter {chapter['chapter_index']}.")
        return True

    def _find_existing_sequence_page(
        self, chapter_directory: str, page_index: int, page_width: int
    ) -> str | None:
        prefix = f"page_{page_index:0{page_width}d}."
        for filename in sorted(os.listdir(chapter_directory)):
            if not filename.startswith(prefix) or filename.endswith(".part"):
                continue
            candidate = os.path.join(chapter_directory, filename)
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate
        return None

    def _sequence_image_extension(self, url: str, content_type: str) -> str:
        supported = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
        extension = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
        if extension in supported:
            return extension
        mime_type = content_type.split(";", 1)[0].strip().lower()
        return {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/tiff": ".tiff",
            "image/avif": ".avif",
        }.get(mime_type, ".bin")

    def _manifest_relative_path(self, output_directory: str, path: str) -> str:
        return Path(path).relative_to(Path(output_directory)).as_posix()

    def _write_manifest(self, output_directory: str, manifest: dict):
        os.makedirs(output_directory, exist_ok=True)
        manifest_path = os.path.join(output_directory, "manifest.json")
        temporary_path = manifest_path + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as output:
            json.dump(manifest, output, indent=2, ensure_ascii=False)
            output.write("\n")
        os.replace(temporary_path, manifest_path)

    def _render_sequence_results(self, chapters: list[dict]):
        flattened = []
        for chapter in chapters:
            chapter_index = chapter["chapter_index"]
            for image in chapter["images"]:
                page_index = image["page_index"]
                extension = self._sequence_image_extension(image["source_url"], "")
                local_path = image.get("local_path") or (
                    f"chapter_{chapter_index:03d}/page_{page_index:03d}{extension}"
                )
                flattened.append(
                    {
                        "index": f"{chapter_index:03d}.{page_index:03d}",
                        "filename": local_path,
                        "kind": "image",
                        "url": image["source_url"],
                        "status": image.get("status", "ready").replace("_", " ").title(),
                    }
                )
        self._render_items(flattened)
        self._log(
            f"[Sequence] Results contain {len(chapters)} chapter(s) and "
            f"{len(flattened)} image(s)."
        )

    def download_selected(self):
        if not self._items:
            messagebox.showinfo("Nothing to download", "No results. Click 'Preview / Analyse' first.")
            return

        selected_ids = set(self.tree.selection())
        if not selected_ids:
            messagebox.showinfo("No selection", "Select one or more rows in the results table.")
            return

        selected = [item for item in self._items if item["row_id"] in selected_ids]
        self._begin_download(selected)

    def download_all(self):
        if not self._items:
            messagebox.showinfo("Nothing to download", "No results. Click 'Preview / Analyse' first.")
            return
        self._begin_download(list(self._items))

    def _begin_download(self, items: list[dict]):
        out_dir = self.out_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("Missing output folder", "Please choose an output folder.")
            return
        timeout = self._parse_timeout()
        if timeout is None:
            return

        zip_enabled = bool(self.zip_var.get())
        zip_name = self.zip_name_var.get().strip() or "media.zip"
        item_snapshots = [dict(item) for item in items]

        self._set_busy(True)
        self._log(f"Downloading {len(items)} item(s) to: {out_dir}")

        def work():
            try:
                os.makedirs(out_dir, exist_ok=True)
                if zip_enabled:
                    path, succeeded = self._download_items_and_zip(
                        item_snapshots, out_dir, zip_name, timeout
                    )
                    self._post_ui("log", f"Saved ZIP: {path}")
                else:
                    succeeded = self._download_items(item_snapshots, out_dir, timeout)
                self._post_ui("log", f"Download complete: {succeeded}/{len(item_snapshots)} successful.")
            except Exception as exc:
                self._post_ui("log", f"Download failed: {exc}")
            finally:
                self._post_ui("busy", False)

        self._start_worker(work)

    # -------------------- Parsing / validation --------------------

    def _parse_timeout(self) -> float | None:
        try:
            timeout = float(self.timeout_var.get().strip())
            if timeout <= 0:
                raise ValueError
            return timeout
        except ValueError:
            messagebox.showwarning("Invalid timeout", "Timeout must be a positive number of seconds.")
            return None

    def _compile_regex(self) -> re.Pattern | None:
        regex_text = self.regex_var.get().strip() or r".*"
        try:
            return re.compile(regex_text)
        except re.error as exc:
            messagebox.showwarning("Invalid regex", f"Regex error: {exc}")
            return None

    # -------------------- Network helpers --------------------

    def _fetch_page(
        self,
        url: str,
        timeout: float,
        log: Callable[[str], None] | None = None,
        structured: bool = False,
        capture_raw: Callable[[bytes, str], None] | None = None,
    ) -> tuple[str, str]:
        response = self.session.get(url, timeout=timeout)
        html = response.text
        final_url = response.url
        if capture_raw:
            capture_raw(response.content, final_url)

        if log:
            if structured:
                content_type = response.headers.get("Content-Type", "(not provided)")
                log(f"[Structured] HTTP {response.status_code}")
                log(f"[Structured] Final URL: {final_url}")
                log(f"[Structured] Content-Type: {content_type}")
                log(f"[Structured] Received {len(html):,} characters ({len(response.content):,} bytes)")
            else:
                log(f"HTTP {response.status_code} {response.reason} - {final_url}")
        response.raise_for_status()
        return html, final_url

    def _fetch_html(self, url: str, timeout: float) -> str:
        """Retain the original helper contract for callers outside the UI."""
        html, _final_url = self._fetch_page(url, timeout)
        return html

    def _log_raw_presence_checks(self, html: str):
        for marker in ("chapter_boxImages", "image_story", "imageChap"):
            present = "YES" if marker in html else "NO"
            self._post_ui("log", f"[Structured] Raw HTML contains '{marker}': {present}")

    # -------------------- Media extraction --------------------

    def _extract_media_items(
        self, html: str, base_url: str, pattern: re.Pattern | None
    ) -> list[dict]:
        """Run the original broad scan and de-duplicate URLs in discovery order."""
        if pattern is None:
            pattern = re.compile(r".*")
        soup = BeautifulSoup(html, "html.parser")
        raw_urls: list[tuple[str, str]] = []

        for img in soup.select("img[src]"):
            src = (img.get("src") or "").strip()
            if src:
                raw_urls.append((urllib.parse.urljoin(base_url, src), "image"))

        for source in soup.select("source[srcset]"):
            srcset = (source.get("srcset") or "").strip()
            for part in srcset.split(",") if srcset else ():
                candidate = part.strip().split()[0] if part.strip() else ""
                if candidate:
                    raw_urls.append((urllib.parse.urljoin(base_url, candidate), "image"))

        for video in soup.select("video[src]"):
            src = (video.get("src") or "").strip()
            if src:
                raw_urls.append((urllib.parse.urljoin(base_url, src), "video"))

        for source in soup.select("video source[src]"):
            src = (source.get("src") or "").strip()
            if src:
                raw_urls.append((urllib.parse.urljoin(base_url, src), "video"))

        media_ext = r"(?:jpg|jpeg|png|webp|gif|mp4|webm|mkv|mov|m4v)"
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            absolute_url = urllib.parse.urljoin(base_url, href)
            if re.search(rf"\.{media_ext}(?:\?.*)?$", absolute_url, flags=re.IGNORECASE):
                raw_urls.append((absolute_url, "media"))

        raw_url_pattern = rf"https?://[^\s\"'<>]+\.{media_ext}(?:\?[^\s\"'<>]+)?"
        for match in re.finditer(raw_url_pattern, html, flags=re.IGNORECASE):
            raw_urls.append((match.group(0), "media"))

        items: list[dict] = []
        seen: set[str] = set()
        for url, kind in raw_urls:
            if not url.startswith(("http://", "https://")):
                continue
            if not pattern.search(url) or url in seen:
                continue
            seen.add(url)
            items.append({"url": url, "kind": kind, "label": f"[{kind}] {url}"})
        return items

    def _extract_structured_images(
        self,
        html: str,
        base_url: str,
        container_selector: str,
        item_selector: str,
        image_selector: str,
        log: Callable[[str], None] | None = None,
    ) -> list[dict]:
        def diagnostic(message: str):
            if log:
                log(f"[Structured] {message}")

        diagnostic("Parsing HTML with BeautifulSoup (html.parser)...")
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            diagnostic(f"BeautifulSoup parse FAILED: {exc}")
            raise
        diagnostic("BeautifulSoup parse complete.")

        try:
            container = soup.select_one(container_selector)
        except Exception as exc:
            diagnostic(f"Container selector '{container_selector}': ERROR - {exc}")
            return []

        if container is None:
            diagnostic(f"Container '{container_selector}': NOT FOUND")
            diagnostic(
                "Diagnostic outcome A: the requested container is absent from the raw HTTP response."
            )
            return []
        diagnostic(f"Container '{container_selector}': FOUND")

        container_id = container.get("id") or "(none)"
        class_list = container.get("class") or []
        classes = " ".join(class_list) if class_list else "(none)"
        descendant_count = len(container.find_all(True))
        diagnostic(
            f"Container metadata: tag={container.name}, id={container_id}, "
            f"classes={classes}, descendants={descendant_count}"
        )

        try:
            elements = container.select(item_selector)
        except Exception as exc:
            diagnostic(f"Item selector '{item_selector}': ERROR - {exc}")
            return []
        diagnostic(f"Item selector '{item_selector}': {len(elements)} matches")
        if not elements:
            diagnostic(
                "Diagnostic outcome B: the container exists, but no expected item elements were found."
            )
            return []

        extracted: list[dict] = []
        skip_counts = {"no_image": 0, "no_url": 0, "url_error": 0, "unsupported_url": 0}
        matched_images = 0
        for dom_index, element in enumerate(elements, start=1):
            try:
                image = element.select_one(image_selector)
            except Exception as exc:
                diagnostic(f"Image selector '{image_selector}': ERROR - {exc}")
                return []

            if image is None:
                skip_counts["no_image"] += 1
                if dom_index <= self.MAX_ITEM_DIAGNOSTICS:
                    diagnostic(f"Item {dom_index}: no element matching '{image_selector}' - skipped")
                continue
            matched_images += 1
            if dom_index <= self.MAX_ITEM_DIAGNOSTICS:
                diagnostic(f"Item {dom_index}: {image_selector} FOUND")

            raw_url, attribute = self._image_url_from_element(image)
            if not raw_url:
                skip_counts["no_url"] += 1
                if dom_index <= self.MAX_ITEM_DIAGNOSTICS:
                    diagnostic(f"Item {dom_index}: no supported image URL attribute - skipped")
                continue
            if dom_index <= self.MAX_ITEM_DIAGNOSTICS:
                diagnostic(f"Item {dom_index}: URL from '{attribute}'")

            try:
                absolute_url = urllib.parse.urljoin(base_url, raw_url)
            except Exception as exc:
                skip_counts["url_error"] += 1
                if dom_index <= self.MAX_ITEM_DIAGNOSTICS:
                    diagnostic(f"Item {dom_index}: URL normalisation failed ({exc}) - skipped")
                continue
            scheme = urllib.parse.urlparse(absolute_url).scheme.lower()
            if scheme not in ("http", "https"):
                skip_counts["unsupported_url"] += 1
                if dom_index <= self.MAX_ITEM_DIAGNOSTICS:
                    diagnostic(
                        f"Item {dom_index}: unsupported URL scheme in '{raw_url}' - skipped"
                    )
                continue

            extracted.append(
                {
                    "index": dom_index,
                    "url": absolute_url,
                    "kind": "image",
                    "source_attribute": attribute,
                }
            )

        if len(elements) > self.MAX_ITEM_DIAGNOSTICS:
            diagnostic(
                f"Per-item diagnostics limited to the first {self.MAX_ITEM_DIAGNOSTICS} of "
                f"{len(elements)} items."
            )

        width = max(4, len(str(len(elements))))
        for item in extracted:
            fallback = f"image_{item['index']:0{width}d}.bin"
            original_name = self._pick_filename_from_url(item["url"], fallback)
            original_name = self._sanitize_filename(original_name, fallback)
            item["filename"] = f"{item['index']:0{width}d}_{original_name}"
            item["status"] = "Ready"

        skipped = sum(skip_counts.values())
        if skipped:
            diagnostic(
                "Skipped images by reason: "
                f"no image element={skip_counts['no_image']}, "
                f"no URL attribute={skip_counts['no_url']}, "
                f"URL normalisation error={skip_counts['url_error']}, "
                f"unsupported URL={skip_counts['unsupported_url']}"
            )
        diagnostic(f"Extracted {len(extracted)} structured image(s) in DOM order.")
        if not extracted:
            if matched_images == 0:
                diagnostic(
                    "Diagnostic outcome B: item elements exist, but the expected image children are absent."
                )
            else:
                diagnostic(
                    "Diagnostic outcome C: image elements exist, but URL extraction or processing rejected all of them."
                )
        return extracted

    def _image_url_from_element(self, image) -> tuple[str | None, str | None]:
        attribute_names = (
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-url",
            "data-image",
            "data-cfsrc",
        )
        for attribute in attribute_names:
            value = image.get(attribute)
            if isinstance(value, str):
                value = value.strip()
                if self._is_usable_image_candidate(value):
                    return value, attribute

        for attribute in ("srcset", "data-srcset"):
            value = image.get(attribute)
            if not isinstance(value, str):
                continue
            for part in value.split(","):
                candidate = part.strip().split()[0] if part.strip() else ""
                if self._is_usable_image_candidate(candidate):
                    return candidate, attribute
        return None, None

    def _extract_embedded_script_images(
        self, html: str, log: Callable[[str], None] | None = None
    ) -> list[dict]:
        """Find Base64-encoded absolute image URLs explicitly stored in scripts.

        Some pages ship the ordered image list as data and let JavaScript build
        the container later. Decoding those URL strings is deterministic and
        does not require executing page code or guessing sequential filenames.
        """
        soup = BeautifulSoup(html, "html.parser")
        quoted_base64 = re.compile(
            r"(?P<quote>['\"])(?P<value>[A-Za-z0-9+/]{20,}={0,2})(?P=quote)"
        )
        image_extension = re.compile(
            r"\.(?:jpe?g|png|webp|gif|bmp|tiff?|avif)$", re.IGNORECASE
        )

        urls: list[str] = []
        seen: set[str] = set()
        for script in soup.find_all("script"):
            script_text = script.string or script.get_text()
            if not script_text:
                continue
            for match in quoted_base64.finditer(script_text):
                encoded = match.group("value")
                try:
                    decoded = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
                except (binascii.Error, UnicodeDecodeError, ValueError):
                    continue

                parsed = urllib.parse.urlparse(decoded)
                if parsed.scheme.lower() not in ("http", "https"):
                    continue
                if not image_extension.search(parsed.path):
                    continue
                if decoded in seen:
                    continue
                seen.add(decoded)
                urls.append(decoded)

        if not urls:
            if log:
                log("[Structured] Embedded-script fallback found no encoded image URLs.")
            return []

        width = max(4, len(str(len(urls))))
        items: list[dict] = []
        for index, url in enumerate(urls, start=1):
            fallback = f"image_{index:0{width}d}.bin"
            original_name = self._pick_filename_from_url(url, fallback)
            original_name = self._sanitize_filename(original_name, fallback)
            items.append(
                {
                    "index": index,
                    "url": url,
                    "kind": "image",
                    "source_attribute": "Base64 string in inline script",
                    "filename": f"{index:0{width}d}_{original_name}",
                    "status": "Ready",
                }
            )

        if log:
            log(
                f"[Structured] Embedded-script fallback recovered {len(items)} "
                "image URL(s) in source order."
            )
        return items

    def _is_usable_image_candidate(self, value: str) -> bool:
        if not value or value == "#":
            return False
        return not value.lower().startswith(("data:", "javascript:", "about:"))

    def _prepare_generic_items(self, items: list[dict]) -> list[dict]:
        prepared: list[dict] = []
        for index, source in enumerate(items, start=1):
            item = dict(source)
            fallback = f"download_{index:04d}.bin"
            name = self._pick_filename_from_url(item["url"], fallback)
            item.update(
                index=index,
                filename=self._sanitize_filename(name, fallback),
                status="Ready",
            )
            prepared.append(item)
        return prepared

    def _render_items(self, items: list[dict]):
        self._clear_results(log=False)
        self._items = items
        for position, item in enumerate(items):
            row_id = f"result-{position}"
            item["row_id"] = row_id
            self.tree.insert(
                "",
                "end",
                iid=row_id,
                values=(
                    item["index"],
                    item["filename"],
                    item["kind"],
                    item["url"],
                    item["status"],
                ),
            )
        self._log(f"Preview contains {len(items)} media item(s).")

    def _set_item_status(self, row_id: str, status: str):
        if self.tree.exists(row_id):
            values = list(self.tree.item(row_id, "values"))
            values[4] = status
            self.tree.item(row_id, values=values)
        for item in self._items:
            if item.get("row_id") == row_id:
                item["status"] = status
                break

    # -------------------- Downloading --------------------

    def _sanitize_filename(self, name: str, default: str) -> str:
        name = name.strip() or default
        name = re.sub(r"[^\w.\-() ]+", "_", name).strip()
        if len(name) > 180:
            stem, extension = os.path.splitext(name)
            name = stem[: max(1, 180 - len(extension))] + extension[:10]
        return name or default

    def _pick_filename_from_url(self, url: str, fallback: str) -> str:
        parsed = urllib.parse.urlparse(url)
        basename = urllib.parse.unquote(os.path.basename(parsed.path))
        return basename or fallback

    def _download_items(self, items: list[dict], out_dir: str, timeout: float) -> int:
        used_names: set[str] = set()
        succeeded = 0
        for progress, item in enumerate(items, start=1):
            filename = self._unique_filename(item["filename"], used_names)
            row_id = item.get("row_id")
            if row_id:
                self._post_ui("status", (row_id, "Downloading"))
            self._post_ui("log", f"Downloading {progress}/{len(items)}: {item['url']}")
            try:
                path = self._download_url(
                    item["url"], out_dir=out_dir, timeout=timeout, filename=filename
                )
                succeeded += 1
                if row_id:
                    self._post_ui("status", (row_id, "Saved"))
                self._post_ui("log", f"Saved: {path}")
            except Exception as exc:
                if row_id:
                    self._post_ui("status", (row_id, "Error"))
                self._post_ui("log", f"Failed: {item['url']} ({exc})")
        return succeeded

    def _download_url(
        self, url: str, out_dir: str, timeout: float, filename: str | None = None
    ) -> str:
        os.makedirs(out_dir, exist_ok=True)
        fallback = "download.bin"
        chosen_name = filename or self._pick_filename_from_url(url, fallback)
        chosen_name = self._sanitize_filename(chosen_name, fallback)
        output_path = os.path.join(out_dir, chosen_name)

        with self.session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with open(output_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        output.write(chunk)
        return output_path

    def _download_items_and_zip(
        self, items: list[dict], out_dir: str, zip_name: str, timeout: float
    ) -> tuple[str, int]:
        zip_name = self._sanitize_filename(zip_name, "media.zip")
        if not zip_name.lower().endswith(".zip"):
            zip_name += ".zip"
        zip_path = os.path.join(out_dir, zip_name)

        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_items = []
            for position, item in enumerate(items, start=1):
                archive_item = dict(item)
                if not re.match(r"^\d+_", archive_item["filename"]):
                    archive_item["filename"] = f"{position:04d}_{archive_item['filename']}"
                archive_items.append(archive_item)
            succeeded = self._download_items(archive_items, temporary_directory, timeout)
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(Path(temporary_directory).iterdir()):
                    archive.write(path, arcname=path.name)
        return zip_path, succeeded

    def _download_urls_and_zip(
        self, urls: list[str], out_dir: str, zip_name: str, timeout: float
    ) -> str:
        """Compatibility wrapper for the original URL-list helper."""
        items = []
        for index, url in enumerate(urls, start=1):
            extension = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".bin"
            items.append(
                {
                    "url": url,
                    "filename": f"{index:04d}{extension[:10]}",
                    "row_id": "",
                }
            )
        path, _succeeded = self._download_items_and_zip(items, out_dir, zip_name, timeout)
        return path

    def _unique_filename(self, filename: str, used_names: set[str]) -> str:
        candidate = filename
        stem, extension = os.path.splitext(filename)
        suffix = 2
        while candidate.casefold() in used_names:
            candidate = f"{stem}_{suffix}{extension}"
            suffix += 1
        used_names.add(candidate.casefold())
        return candidate


def get_module(parent, app=None):
    """Tkinter module entry point expected by main.py."""
    return WebScraperModule(parent, app=app)
