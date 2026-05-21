import tkinter as tk
from tkinter import ttk, messagebox
import importlib
import os
import sys
import traceback


class SwissArmyKnife(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Swiss Army Knife")
        self.geometry("1000x600")
        self.minsize(800, 500)

        # Keep track of loaded modules
        self.modules = {}
        self.current_module = None  # module object
        self.current_module_frame = None  # actual Frame instance from module

        # --- Setup UI ---
        self._create_menubar()
        self._create_layout()
        self._load_module_list()

    # -------------------- UI SETUP --------------------
    def _create_menubar(self):
        """Create the top menu bar"""
        menubar = tk.Menu(self)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        # Tools menu
        tools_menu = tk.Menu(menubar, tearoff=0)

        # --- Mode submenu ---
        mode_menu = tk.Menu(tools_menu, tearoff=0)
        self.quick_compile_enabled = tk.BooleanVar(value=False)
        self.dev_mode_enabled = tk.BooleanVar(value=False)
        self.expert_mode_enabled = tk.BooleanVar(value=False)

        mode_menu.add_checkbutton(
            label="Quick Compile Mode",
            variable=self.quick_compile_enabled,
            command=lambda: self._toggle_mode("Quick Compile", self.quick_compile_enabled.get())
        )

        mode_menu.add_checkbutton(
            label="Developer Mode",
            variable=self.dev_mode_enabled,
            command=lambda: self._toggle_mode("Developer", self.dev_mode_enabled.get())
        )

        mode_menu.add_checkbutton(
            label="Expert Mode",
            variable=self.expert_mode_enabled,
            command=lambda: self._toggle_mode("Expert", self.expert_mode_enabled.get())
        )

        tools_menu.add_cascade(label="Mode", menu=mode_menu)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    def _toggle_mode(self, mode_name, state):
        # Prevent both modes from being active together
        if mode_name == "Quick Compile" and state:
            self.dev_mode_enabled.set(False)

        if mode_name == "Developer" and state:
            self.quick_compile_enabled.set(False)

        # Tell module frame to update itself visually (frame, not module object)
        if self.current_module_frame and hasattr(self.current_module_frame, "notify_mode_change"):
            try:
                self.current_module_frame.notify_mode_change()
            except Exception:
                # don't crash the whole app if a module misbehaves
                traceback.print_exc()

        print(f"{mode_name} mode {'enabled' if state else 'disabled'}")

    def _create_layout(self):
        """Create main layout structure"""
        # Root window uses 2 main sections: Sidebar & Main area
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # Sidebar
        sidebar = ttk.Frame(self, padding=10, relief="ridge")
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.columnconfigure(0, weight=1)

        ttk.Label(sidebar, text="Modules", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, pady=(0, 5))

        self.module_list = tk.Listbox(sidebar, height=20, exportselection=False)
        self.module_list.grid(row=1, column=0, sticky="nsew")
        self.module_list.bind("<<ListboxSelect>>", self._on_module_select)

        refresh_button = ttk.Button(sidebar, text="🔄 Refresh", command=self._load_module_list)
        refresh_button.grid(row=2, column=0, pady=(10, 0), sticky="ew")

        # Main content area
        self.main_frame = ttk.Frame(self, padding=10, relief="sunken")
        self.main_frame.grid(row=0, column=1, sticky="nsew")

        self.default_label = ttk.Label(
            self.main_frame,
            text="Select a module from the list on the left.",
            font=("Segoe UI", 12, "italic"),
            anchor="center",
        )
        self.default_label.pack(expand=True)

    # -------------------- MODULE MANAGEMENT --------------------
    def _load_module_list(self):
        """Discover available modules (single .py files OR packages)"""
        self.module_list.delete(0, "end")
        self.modules.clear()

        base_dir = os.path.dirname(__file__)
        module_dir = os.path.join(base_dir, "modules")

        if not os.path.exists(module_dir):
            os.makedirs(module_dir)

        # Make sure project root is importable (IMPORTANT)
        if base_dir not in sys.path:
            sys.path.insert(0, base_dir)

        for entry in os.listdir(module_dir):
            full_path = os.path.join(module_dir, entry)

            # Case 1: standalone .py module
            if entry.endswith(".py") and not entry.startswith("__"):
                name = entry[:-3]
                self.modules[name] = f"modules.{name}"
                self.module_list.insert("end", name)

            # Case 2: folder-based module
            elif os.path.isdir(full_path):
                init_file = os.path.join(full_path, "__init__.py")
                if os.path.exists(init_file):
                    self.modules[entry] = f"modules.{entry}"
                    self.module_list.insert("end", entry)

        if not self.modules:
            self.module_list.insert("end", "(no modules found)")

    def _on_module_select(self, event):
        """Handle selection from module list"""
        selection = self.module_list.curselection()
        if not selection:
            return
        selected = self.module_list.get(selection[0])
        if selected == "(no modules found)":
            return
        self.load_module(selected)

    def load_module(self, module_name):
        """Safely load a module and display its UI"""
        # Destroy old module frame (if present)
        if self.current_module_frame:
            try:
                self.current_module_frame.destroy()
            except Exception:
                traceback.print_exc()
            finally:
                self.current_module_frame = None
                self.current_module = None

        # Clear any leftover widgets (keeps behavior if module left something)
        for widget in self.main_frame.winfo_children():
            widget.destroy()

        # Try to import the selected module (use importlib.reload to pull latest code)
        module_path = self.modules.get(module_name)
        try:
            mod = importlib.import_module(module_path)
            # reload so dev changes are picked up on refresh
            try:
                importlib.reload(mod)
            except Exception:
                pass

            if hasattr(mod, "get_module"):
                # get_module returns a Frame (PDFCompiler)
                frame = mod.get_module(self.main_frame, app=self)
                frame.pack(fill="both", expand=True)

                # Store both the module object and the actual frame instance
                self.current_module = mod
                self.current_module_frame = frame

                # Force the module UI to sync its mode immediately (safe call)
                if hasattr(frame, "notify_mode_change"):
                    try:
                        frame.notify_mode_change()
                    except Exception:
                        traceback.print_exc()

            else:
                raise AttributeError(f"Module '{module_name}' has no 'get_module' function.")

        except Exception as e:
            traceback.print_exc()
            ttk.Label(self.main_frame, text=f"⚠️ Failed to load {module_name}\n{e}", foreground="red").pack(expand=True)

    # -------------------- MISC FUNCTIONS --------------------
    def show_about(self):
        messagebox.showinfo(
            "About",
            "Swiss Army Knife App\nVersion 1.0\n\nA modular toolkit for all-purpose utilities.\nDeveloped by Josh."
        )


if __name__ == "__main__":
    app = SwissArmyKnife()
    app.mainloop()
