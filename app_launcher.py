import customtkinter as ctk
import subprocess
import threading
import json
import os
import sys
import signal
import queue
import time
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime

# --- Constants ---
APP_TITLE = "App Launcher"
# Support PyInstaller bundled mode
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
    _BUNDLE_DIR = getattr(sys, '_MEIPASS', _BASE_DIR)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE_DIR = _BASE_DIR

SETTINGS_FILE = os.path.join(_BASE_DIR, "app_launcher_settings.json")
DATASOURCES_FILE = os.path.join(_BASE_DIR, "datasources.json")
DEFAULT_MAVEN_PATH = "mvn"
DEFAULT_JAVA_PATH = "java"
DEFAULT_NODE_PATH = "npm"
CONSOLE_LOG4J2_CONFIG = os.path.join(_BUNDLE_DIR, "log4j2-console.xml")
DATASOURCE_ENVIRONMENT_COLORS = {
    "local": "#27ae60",
    "dev": "#e67e22",
}
DATASOURCE_CATEGORY_OPTIONS = ["primary", "readonly"]
DATASOURCE_ENVIRONMENT_OPTIONS = ["local", "dev"]


def _new_datasource_id():
    return f"ds_{int(time.time()*1000)}"


def _normalize_datasource_category(value):
    value = (value or "primary").strip().lower()
    if value in ("read-only", "read_only", "readonly"):
        return "readonly"
    return "primary"


def _normalize_datasource_environment(value):
    value = (value or "local").strip().lower()
    return value if value in DATASOURCE_ENVIRONMENT_OPTIONS else "local"


def _datasource_color(environment):
    return DATASOURCE_ENVIRONMENT_COLORS.get(_normalize_datasource_environment(environment), "#7f8c8d")


class ProcessManager:
    """Manages a subprocess with real-time output streaming."""

    def __init__(self, on_output, on_exit):
        self.process = None
        self.on_output = on_output
        self.on_exit = on_exit
        self._stop_event = threading.Event()
        self._threads = []

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def run(self, cmd, cwd=None, env=None):
        if self.is_running():
            return False
        self._stop_event.clear()
        try:
            merged_env = os.environ.copy()
            if env:
                merged_env.update(env)

            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=merged_env,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
            )
            t = threading.Thread(target=self._read_output, daemon=True)
            t.start()
            self._threads.append(t)
            return True
        except Exception as e:
            self.on_output(f"[ERROR] Failed to start process: {e}\n")
            self.on_exit(-1)
            return False

    def _read_output(self):
        try:
            for line in self.process.stdout:
                if self._stop_event.is_set():
                    break
                self.on_output(line)
        except Exception:
            pass
        finally:
            if self.process:
                self.process.stdout.close()
                self.process.wait()
                exit_code = self.process.returncode
            else:
                exit_code = -1
            self.on_exit(exit_code)

    def stop(self):
        self._stop_event.set()
        if self.process and self.is_running():
            pid = self.process.pid
            try:
                if sys.platform == "win32":
                    # Kill entire process tree (maven + java child processes)
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass


class ProjectTab:
    """Represents a single project tab with logs and settings."""

    def __init__(self, parent_notebook, app, tab_id=None, config=None):
        self.app = app
        self.tab_id = tab_id or f"tab_{int(time.time()*1000)}"
        self.process_manager = ProcessManager(
            on_output=self._on_process_output,
            on_exit=self._on_process_exit,
        )
        self.output_queue = queue.Queue()
        self.config = config or self._default_config()
        self._log_font_size = int(self.config.get("log_font_size", 12))
        self._saved_sash_ratio = self.config.get("sash_ratio", 0.5)

        # --- Main Frame ---
        self.frame = ctk.CTkFrame(parent_notebook)
        self.frame.grid_rowconfigure(0, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)

        # --- Draggable PanedWindow ---
        self.paned = tk.PanedWindow(self.frame, orient=tk.HORIZONTAL, sashwidth=6,
                                    sashrelief=tk.FLAT, bg="#3b3b3b",
                                    opaqueresize=True)
        self.paned.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        self._build_log_panel()
        self._build_settings_panel()

        # Add panels to paned window
        self.paned.add(self.log_outer_frame, stretch="always")
        self.paned.add(self.settings_outer_frame, stretch="always")

        # Set initial sash from saved ratio (delayed to ensure widget is mapped)
        self._sash_retries = 0
        self._sash_set = False
        self.frame.after(200, self._set_initial_sash)

        self._poll_output()

    def _set_initial_sash(self):
        self.frame.update_idletasks()
        total_w = self.paned.winfo_width()
        if total_w > 100:
            pos = int(total_w * self._saved_sash_ratio)
            self.paned.sash_place(0, pos, 0)
            self._sash_set = True
        elif self._sash_retries < 10:
            self._sash_retries += 1
            self.frame.after(200, self._set_initial_sash)

    def _get_sash_ratio(self):
        """Get current sash position as a ratio 0-1."""
        try:
            total_w = self.paned.winfo_width()
            if total_w > 100:
                sash_pos = self.paned.sash_coord(0)[0]
                return round(sash_pos / total_w, 3)
        except Exception:
            pass
        return 0.5

    def _default_config(self):
        return {
            "name": "New Project",
            "project_type": "spring-boot",
            "project_path": "",
            "main_class": "",
            "vm_options": "",
            "program_args": "",
            "maven_path": DEFAULT_MAVEN_PATH,
            "java_path": DEFAULT_JAVA_PATH,
            "node_path": DEFAULT_NODE_PATH,
            "maven_profiles": "",
            "maven_extra_args": "",
            "debug_enabled": False,
            "debug_port": "5005",
            "console_logging": True,
            "log_max_lines": "500",
            "skip_jacoco": True,
            "npm_script": "start",
            "spring_profile": "",
            "primary_datasource_id": "",
            "readonly_datasource_id": "",
            "override_port": False,
            "server_port": "",
        }

    # ========================
    # LOG PANEL (LEFT)
    # ========================
    def _build_log_panel(self):
        self.log_outer_frame = tk.Frame(self.paned, bg="#2b2b2b")
        log_frame = ctk.CTkFrame(self.log_outer_frame, corner_radius=10)
        log_frame.pack(fill="both", expand=True, padx=(4, 2), pady=4)
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = ctk.CTkFrame(log_frame, height=44, corner_radius=8, fg_color=("gray86", "gray20"))
        toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 3))

        self.status_label = ctk.CTkLabel(toolbar, text="● STOPPED", text_color="gray",
                                         font=ctk.CTkFont(size=13, weight="bold"))
        self.status_label.pack(side="left", padx=8)

        # Font size buttons
        btn_font_minus = ctk.CTkButton(toolbar, text="A-", width=32, height=28, corner_radius=6,
                                       fg_color=("gray70", "gray35"), hover_color=("gray60", "gray45"),
                                       font=ctk.CTkFont(size=12, weight="bold"),
                                       command=self._log_font_decrease)
        btn_font_minus.pack(side="left", padx=(8, 1))

        self.font_size_label = ctk.CTkLabel(toolbar, text=str(self._log_font_size), width=28,
                                            font=ctk.CTkFont(size=11))
        self.font_size_label.pack(side="left", padx=1)

        btn_font_plus = ctk.CTkButton(toolbar, text="A+", width=32, height=28, corner_radius=6,
                                      fg_color=("gray70", "gray35"), hover_color=("gray60", "gray45"),
                                      font=ctk.CTkFont(size=12, weight="bold"),
                                      command=self._log_font_increase)
        btn_font_plus.pack(side="left", padx=(1, 4))

        btn_clear = ctk.CTkButton(toolbar, text="Clear", width=70, height=30, corner_radius=6,
                                  fg_color=("gray70", "gray35"), hover_color=("gray60", "gray45"),
                                  command=self._clear_logs)
        btn_clear.pack(side="right", padx=3)

        btn_stop = ctk.CTkButton(toolbar, text="⏹ Stop", width=80, height=30, corner_radius=6,
                                 fg_color="#c0392b", hover_color="#e74c3c", command=self.stop_process)
        btn_stop.pack(side="right", padx=3)

        btn_run = ctk.CTkButton(toolbar, text="▶ Run", width=80, height=30, corner_radius=6,
                                fg_color="#27ae60", hover_color="#2ecc71", command=self.run_app)
        btn_run.pack(side="right", padx=3)

        # Log text
        self.log_text = ctk.CTkTextbox(log_frame, wrap="word", corner_radius=8,
                                       font=ctk.CTkFont(family="Consolas", size=self._log_font_size),
                                       state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=6, pady=(3, 6))

        # Configure color tags
        self._setup_log_tags()

    def _setup_log_tags(self):
        """Configure text tags for colored log output."""
        text_widget = self.log_text._textbox
        text_widget.tag_configure("error", foreground="#e74c3c")
        text_widget.tag_configure("warn", foreground="#f39c12")
        text_widget.tag_configure("info", foreground="#2ecc71")
        text_widget.tag_configure("debug", foreground="#3498db")
        text_widget.tag_configure("trace", foreground="#95a5a6")
        text_widget.tag_configure("system", foreground="#1abc9c", font=("Consolas", self._log_font_size, "bold"))
        text_widget.tag_configure("exception", foreground="#e67e22")

    def _get_log_tag(self, line):
        """Determine the color tag for a log line."""
        upper = line.upper()
        if any(k in upper for k in ["[ERROR", "ERROR ", "SEVERE", "FATAL"]):
            return "error"
        if any(k in upper for k in ["[WARN", "WARN ", "WARNING"]):
            return "warn"
        if any(k in upper for k in ["[INFO", "INFO "]):
            return "info"
        if any(k in upper for k in ["[DEBUG", "DEBUG "]):
            return "debug"
        if any(k in upper for k in ["[TRACE", "TRACE "]):
            return "trace"
        if line.startswith(("[RUN]", "[MAVEN]", "[STOP]", "[INFO]")):
            return "system"
        if any(k in upper for k in ["EXCEPTION", "CAUSED BY", "\tAT ", "\tAT\t"]):
            return "exception"
        return None

    def _log_font_increase(self):
        self._log_font_size = min(self._log_font_size + 1, 28)
        self._apply_log_font()

    def _log_font_decrease(self):
        self._log_font_size = max(self._log_font_size - 1, 8)
        self._apply_log_font()

    def _apply_log_font(self):
        self.log_text.configure(font=ctk.CTkFont(family="Consolas", size=self._log_font_size))
        self.font_size_label.configure(text=str(self._log_font_size))
        # Update system tag font too
        self.log_text._textbox.tag_configure("system",
            font=("Consolas", self._log_font_size, "bold"))

    # ========================
    # SETTINGS PANEL (RIGHT)
    # ========================
    def _build_settings_panel(self):
        self.settings_outer_frame = tk.Frame(self.paned, bg="#2b2b2b")
        settings_outer = ctk.CTkFrame(self.settings_outer_frame, corner_radius=10)
        settings_outer.pack(fill="both", expand=True, padx=(2, 4), pady=4)
        settings_outer.grid_rowconfigure(0, weight=1)
        settings_outer.grid_columnconfigure(0, weight=1)

        self.settings_scroll = ctk.CTkScrollableFrame(settings_outer, corner_radius=8)
        self.settings_scroll.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.settings_scroll.grid_columnconfigure(1, weight=1)

        self.setting_vars = {}
        row = 0

        # --- Group: Project ---
        row = self._add_group_header("Project", row)
        row = self._add_entry("name", "Tab Name", row, self.config["name"])
        row = self._add_option_menu("project_type", "Project Type", row,
                                    ["spring-boot", "npm"], self.config["project_type"])
        row = self._add_path_entry("project_path", "Project Path", row, self.config["project_path"], mode="folder")

        # --- Group: Maven Operations ---
        row = self._add_group_header("Maven Operations", row)
        mvn_btn_frame = ctk.CTkFrame(self.settings_scroll, fg_color="transparent")
        mvn_btn_frame.grid(row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=4)
        mvn_btn_frame.grid_columnconfigure((0, 1, 2), weight=1)
        row += 1

        mvn_buttons = [
            ("Reload", "reload", "#2980b9", "#3498db"),
            ("Clean", "clean", "#2980b9", "#3498db"),
            ("Install", "install", "#2980b9", "#3498db"),
            ("Test", "test", "#2980b9", "#3498db"),
            ("Custom...", "custom", "#8e44ad", "#9b59b6"),
        ]
        for i, (label, cmd, fg, hv) in enumerate(mvn_buttons):
            r, c = divmod(i, 3)
            action = self._run_custom_maven if cmd == "custom" else lambda c=cmd: self.run_maven(c)
            b = ctk.CTkButton(mvn_btn_frame, text=label, height=32, corner_radius=6,
                              fg_color=fg, hover_color=hv, command=action)
            b.grid(row=r, column=c, sticky="ew", padx=3, pady=2)

        # --- Group: Java / Spring Boot ---
        row = self._add_group_header("Java / Spring Boot", row)
        row = self._add_entry("main_class", "Main Class", row, self.config["main_class"],
                              placeholder="com.example.Application")
        row = self._add_entry("vm_options", "VM Options", row, self.config["vm_options"],
                              placeholder="-Xmx512m -Xms256m")
        row = self._add_entry("program_args", "Program Args", row, self.config["program_args"])
        row = self._add_entry("spring_profile", "Spring Profile", row, self.config["spring_profile"],
                              placeholder="dev,local")
        row = self._add_datasource_select("primary", row)
        row = self._add_datasource_select("readonly", row)
        row = self._add_checkbox("override_port", "Override Server Port", row,
                                 self.config.get("override_port", False))
        row = self._add_entry("server_port", "Server Port", row, self.config["server_port"],
                              placeholder="e.g. 8080")

        # --- Group: Logging ---
        row = self._add_group_header("Logging", row)
        row = self._add_checkbox("console_logging", "Override logs to Console", row,
                                 self.config.get("console_logging", True))
        row = self._add_entry("log_max_lines", "Max Log Lines", row,
                      self.config.get("log_max_lines", "500"),
                      placeholder="500")

        # --- Group: Debug ---
        row = self._add_group_header("Debug", row)
        row = self._add_checkbox("debug_enabled", "Enable Debug", row, self.config["debug_enabled"])
        row = self._add_entry("debug_port", "Debug Port", row, self.config["debug_port"],
                              placeholder="5005")

        # --- Group: Maven Settings ---
        row = self._add_group_header("Maven Settings", row)
        row = self._add_checkbox("skip_jacoco", "Skip JaCoCo (recommended)", row,
                                 self.config.get("skip_jacoco", True))
        row = self._add_path_entry_with_default("maven_path", "Maven Home", row,
                                                self.config["maven_path"], DEFAULT_MAVEN_PATH)
        row = self._add_entry("maven_profiles", "Maven Profiles", row, self.config["maven_profiles"],
                              placeholder="-P dev")
        row = self._add_entry("maven_extra_args", "Extra Maven Args", row, self.config["maven_extra_args"],
                              placeholder="-DskipTests")

        # --- Group: Paths ---
        row = self._add_group_header("Paths", row)
        row = self._add_path_entry_with_default("java_path", "Java Home", row,
                                                self.config["java_path"], DEFAULT_JAVA_PATH)
        row = self._add_path_entry_with_default("node_path", "Node.js Home", row,
                                                self.config["node_path"], DEFAULT_NODE_PATH)

        # --- Group: NPM ---
        row = self._add_group_header("NPM", row)
        row = self._add_entry("npm_script", "NPM Script", row, self.config["npm_script"],
                              placeholder="start")

    def _add_group_header(self, text, row):
        sep = ctk.CTkFrame(self.settings_scroll, height=1, fg_color=("gray75", "gray35"))
        sep.grid(row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=(14, 0))
        row += 1
        header = ctk.CTkLabel(self.settings_scroll, text=text,
                              font=ctk.CTkFont(size=13, weight="bold"),
                              text_color=("#1a5276", "#85c1e9"))
        header.grid(row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 6))
        return row + 1

    def _add_entry(self, key, label, row, default="", placeholder=None):
        lbl = ctk.CTkLabel(self.settings_scroll, text=label, anchor="w",
                           font=ctk.CTkFont(size=12))
        lbl.grid(row=row, column=0, sticky="w", padx=(12, 5), pady=3)

        var = ctk.StringVar(value=default)
        entry = ctk.CTkEntry(self.settings_scroll, textvariable=var,
                             placeholder_text=placeholder or "", corner_radius=6, height=30)
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(5, 8), pady=3)
        self.setting_vars[key] = var
        return row + 1

    def _add_path_entry(self, key, label, row, default="", mode="folder"):
        lbl = ctk.CTkLabel(self.settings_scroll, text=label, anchor="w",
                           font=ctk.CTkFont(size=12))
        lbl.grid(row=row, column=0, sticky="w", padx=(12, 5), pady=3)

        var = ctk.StringVar(value=default)
        entry = ctk.CTkEntry(self.settings_scroll, textvariable=var, corner_radius=6, height=30)
        entry.grid(row=row, column=1, sticky="ew", padx=5, pady=3)

        btn = ctk.CTkButton(self.settings_scroll, text="📁", width=32, height=30, corner_radius=6,
                            command=lambda: self._browse_path(var, mode))
        btn.grid(row=row, column=2, sticky="e", padx=(0, 8), pady=3)

        self.setting_vars[key] = var
        return row + 1

    def _add_path_entry_with_default(self, key, label, row, default="", default_value=""):
        """Path entry with a 'Default' button to reset to system default."""
        lbl = ctk.CTkLabel(self.settings_scroll, text=label, anchor="w",
                           font=ctk.CTkFont(size=12))
        lbl.grid(row=row, column=0, sticky="w", padx=(12, 5), pady=3)

        inner = ctk.CTkFrame(self.settings_scroll, fg_color="transparent")
        inner.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(5, 8), pady=3)
        inner.grid_columnconfigure(0, weight=1)

        var = ctk.StringVar(value=default)
        entry = ctk.CTkEntry(inner, textvariable=var, corner_radius=6, height=30)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        btn_browse = ctk.CTkButton(inner, text="📁", width=32, height=30, corner_radius=6,
                                   command=lambda: self._browse_path(var, "folder"))
        btn_browse.grid(row=0, column=1, padx=(0, 3))

        btn_default = ctk.CTkButton(inner, text="Default", width=58, height=30, corner_radius=6,
                                    fg_color=("gray70", "gray35"), hover_color=("gray60", "gray45"),
                                    font=ctk.CTkFont(size=11),
                                    command=lambda: var.set(""))
        btn_default.grid(row=0, column=2)

        self.setting_vars[key] = var
        return row + 1

    def _add_option_menu(self, key, label, row, values, default):
        lbl = ctk.CTkLabel(self.settings_scroll, text=label, anchor="w",
                           font=ctk.CTkFont(size=12))
        lbl.grid(row=row, column=0, sticky="w", padx=(12, 5), pady=3)

        var = ctk.StringVar(value=default)
        menu = ctk.CTkOptionMenu(self.settings_scroll, variable=var, values=values,
                                 corner_radius=6, height=30)
        menu.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(5, 8), pady=3)
        self.setting_vars[key] = var
        return row + 1

    def _add_datasource_select(self, category, row):
        key = f"{category}_datasource_id"
        label = "Primary Datasource" if category == "primary" else "Readonly Datasource"
        lbl = ctk.CTkLabel(self.settings_scroll, text=label, anchor="w",
                           font=ctk.CTkFont(size=12))
        lbl.grid(row=row, column=0, sticky="w", padx=(12, 5), pady=3)

        inner = ctk.CTkFrame(self.settings_scroll, fg_color="transparent")
        inner.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(5, 8), pady=3)
        inner.grid_columnconfigure(0, weight=1)

        datasource_id = self.config.get(key, self.config.get("datasource_id", ""))
        datasource = self.app.get_datasource_by_id(datasource_id)
        if datasource and _normalize_datasource_category(datasource.get("category", datasource.get("type"))) != category:
            datasource_id = ""
        default_label = self.app.get_datasource_label(datasource_id, category)
        values = self.app.get_datasource_labels(category)
        var = ctk.StringVar(value=default_label)
        menu = ctk.CTkOptionMenu(inner, variable=var, values=values,
                                 corner_radius=6, height=30,
                                 command=lambda _, c=category: self._on_datasource_changed(c))
        menu.grid(row=0, column=0, sticky="ew", padx=(0, 5))

        badge = ctk.CTkLabel(inner, text="", width=76, height=24,
                             corner_radius=12, fg_color="gray45",
                             text_color="white",
                             font=ctk.CTkFont(size=11, weight="bold"))
        badge.grid(row=0, column=1, sticky="e")

        self.setting_vars[key] = var
        if not hasattr(self, "datasource_menus"):
            self.datasource_menus = {}
            self.datasource_badges = {}
        self.datasource_menus[category] = menu
        self.datasource_badges[category] = badge
        self._on_datasource_changed(category)
        return row + 1

    def _on_datasource_changed(self, category):
        datasource = self.app.get_datasource_by_label(
            self.setting_vars[f"{category}_datasource_id"].get(), category
        )
        badge = self.datasource_badges[category]
        if datasource:
            environment = _normalize_datasource_environment(datasource.get("environment"))
            badge.configure(text=environment.upper(), fg_color=_datasource_color(environment))
        else:
            badge.configure(text="NONE", fg_color="gray45")

    def refresh_datasource_options(self):
        if not hasattr(self, "datasource_menus"):
            return
        for category, menu in self.datasource_menus.items():
            key = f"{category}_datasource_id"
            current = self.setting_vars[key].get()
            current_ds = self.app.get_datasource_by_label(current, category)
            values = self.app.get_datasource_labels(category)
            menu.configure(values=values)
            if current_ds:
                self.setting_vars[key].set(self.app.get_datasource_label(current_ds.get("id"), category))
            elif current not in values:
                self.setting_vars[key].set(values[0])
            self._on_datasource_changed(category)

    def _add_checkbox(self, key, label, row, default=False):
        var = ctk.BooleanVar(value=default)
        cb = ctk.CTkCheckBox(self.settings_scroll, text=label, variable=var,
                             corner_radius=4, font=ctk.CTkFont(size=12))
        cb.grid(row=row, column=0, columnspan=3, sticky="w", padx=12, pady=4)
        self.setting_vars[key] = var
        return row + 1

    def _browse_path(self, var, mode="folder"):
        if mode == "folder":
            path = filedialog.askdirectory()
        elif mode == "file":
            path = filedialog.askopenfilename()
        else:
            path = filedialog.askdirectory()
            if not path:
                path = filedialog.askopenfilename()
        if path:
            var.set(path)

    # ========================
    # ACTIONS
    # ========================
    def get_config(self):
        cfg = {}
        for key, var in self.setting_vars.items():
            if key in ("primary_datasource_id", "readonly_datasource_id"):
                category = key.removesuffix("_datasource_id")
                datasource = self.app.get_datasource_by_label(var.get(), category)
                cfg[key] = datasource.get("id", "") if datasource else ""
            else:
                cfg[key] = var.get()
        cfg["log_font_size"] = self._log_font_size
        cfg["sash_ratio"] = self._get_sash_ratio()
        return cfg

    def _get_java_cmd(self):
        java_path = self.setting_vars["java_path"].get().strip()
        if java_path and os.path.isdir(java_path):
            candidate = os.path.join(java_path, "bin", "java.exe" if sys.platform == "win32" else "java")
            if os.path.isfile(candidate):
                return candidate
            return os.path.join(java_path, "bin", "java")
        # Empty or default: find in system PATH
        found = shutil.which("java")
        return found if found else "java"

    def _get_java_major_version(self, java_cmd):
        """Returns the major Java version (e.g. 8, 11, 17, 21). Falls back to 8 on error."""
        try:
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            result = subprocess.run(
                [java_cmd, "-version"],
                capture_output=True, text=True,
                startupinfo=startupinfo, timeout=5
            )
            output = result.stderr or result.stdout
            # Output examples:
            #   java version "1.8.0_291"   -> Java 8
            #   openjdk version "11.0.2"   -> Java 11
            #   openjdk version "17.0.1"   -> Java 17
            import re
            m = re.search(r'"(\d+)[\._]', output)
            if m:
                major = int(m.group(1))
                # "1.8" style -> major is 1, real version is 8
                if major == 1:
                    m2 = re.search(r'"1\.(\d+)', output)
                    return int(m2.group(1)) if m2 else 8
                return major
        except Exception:
            pass
        return 8  # safe fallback

    def _get_maven_cmd(self):
        mvn_path = self.setting_vars["maven_path"].get().strip()
        if mvn_path and os.path.isdir(mvn_path):
            ext = ".cmd" if sys.platform == "win32" else ""
            candidate = os.path.join(mvn_path, "bin", f"mvn{ext}")
            if os.path.isfile(candidate):
                return candidate
            return os.path.join(mvn_path, "bin", "mvn")
        # Empty or default: find in system PATH
        for name in ("mvn.cmd", "mvn") if sys.platform == "win32" else ("mvn",):
            found = shutil.which(name)
            if found:
                return found
        return "mvn"

    def _get_project_path(self):
        return self.setting_vars["project_path"].get() or None

    def _get_env(self):
        """Build environment with JAVA_HOME and MAVEN_HOME if paths are set."""
        env = {}
        java_path = self.setting_vars["java_path"].get().strip()
        if java_path and os.path.isdir(java_path):
            env["JAVA_HOME"] = java_path
        mvn_path = self.setting_vars["maven_path"].get().strip()
        if mvn_path and os.path.isdir(mvn_path):
            env["MAVEN_HOME"] = mvn_path
            env["M2_HOME"] = mvn_path
        node_path = self.setting_vars["node_path"].get().strip()
        if node_path and os.path.isdir(node_path):
            env["NODE_HOME"] = node_path
        return env if env else None

    def _get_npm_cmd(self):
        node_path = self.setting_vars["node_path"].get().strip()
        if node_path and os.path.isdir(node_path):
            ext = ".cmd" if sys.platform == "win32" else ""
            for candidate_dir in [node_path, os.path.join(node_path, "bin")]:
                candidate = os.path.join(candidate_dir, f"npm{ext}")
                if os.path.isfile(candidate):
                    return candidate
        # Empty or default: find in system PATH
        for name in ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",):
            found = shutil.which(name)
            if found:
                return found
        return "npm"

    def _get_selected_datasource(self, category):
        var = self.setting_vars.get(f"{category}_datasource_id")
        if not var:
            return None
        return self.app.get_datasource_by_label(var.get(), category)

    def _build_datasource_run_args(self):
        mappings = [
            ("jdbc_url", "url"),
            ("username", "username"),
            ("password", "password"),
            ("driver_class_name", "driver-class-name"),
        ]
        args = []
        for category, prefix in (("primary", "database"), ("readonly", "database.readOnly")):
            datasource = self._get_selected_datasource(category)
            if not datasource:
                continue
            for source_key, property_name in mappings:
                value = (datasource.get(source_key) or "").strip()
                if value:
                    args.append(f"--{prefix}.{property_name}={value}")
        return args

    def _log(self, text):
        self.output_queue.put(text)

    def _on_process_output(self, text):
        self._log(text)

    def _on_process_exit(self, exit_code):
        self._log(f"\n[Process exited with code {exit_code}]\n")
        self.output_queue.put(("__STATUS__", "stopped"))

    DEFAULT_MAX_LOG_LINES = 500

    def _get_log_max_lines(self):
        value = self.setting_vars.get("log_max_lines").get().strip()
        try:
            parsed = int(value)
            return max(100, min(parsed, 20000))
        except Exception:
            return self.DEFAULT_MAX_LOG_LINES

    def _poll_output(self):
        items = []
        try:
            while True:
                items.append(self.output_queue.get_nowait())
        except queue.Empty:
            pass

        if items:
            status_items = [i for i in items if isinstance(i, tuple) and i[0] == "__STATUS__"]
            text_items = [i for i in items if not (isinstance(i, tuple) and i[0] == "__STATUS__")]

            if text_items:
                self.log_text.configure(state="normal")
                tw = self.log_text._textbox
                for item in text_items:
                    for line in item.splitlines(True):
                        tag = self._get_log_tag(line)
                        if tag:
                            tw.insert("end", line, tag)
                        else:
                            tw.insert("end", line)

                # Trim to configured max line count
                max_lines = self._get_log_max_lines()
                line_count = int(tw.index("end-1c").split(".")[0])
                if line_count > max_lines:
                    excess = line_count - max_lines
                    tw.delete("1.0", f"{excess + 1}.0")

                self.log_text.see("end")
                self.log_text.configure(state="disabled")

            for s in status_items:
                self._update_status(s[1])

        self.frame.after(100, self._poll_output)

    def _update_status(self, status):
        if status == "running":
            self.status_label.configure(text="● RUNNING", text_color="#27ae60")
        elif status == "stopped":
            self.status_label.configure(text="● STOPPED", text_color="gray")
        elif status == "maven":
            self.status_label.configure(text="● MAVEN", text_color="#2980b9")

    def _clear_logs(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def run_maven(self, goal):
        if self.process_manager.is_running():
            self._log("[WARN] A process is already running. Stop it first.\n")
            return

        mvn = self._get_maven_cmd()
        cwd = self._get_project_path()
        if not cwd:
            self._log("[ERROR] Project path is not set.\n")
            return

        if goal == "reload":
            cmd_parts = [mvn, "dependency:resolve", "-U"]
        else:
            cmd_parts = [mvn, goal]

        profiles = self.setting_vars["maven_profiles"].get().strip()
        if profiles:
            cmd_parts.extend(profiles.split())

        extra = self.setting_vars["maven_extra_args"].get().strip()
        if extra:
            cmd_parts.extend(extra.split())

        self._log(f"[MAVEN] Running: {' '.join(cmd_parts)}\n")
        self._log(f"[MAVEN] Directory: {cwd}\n")
        self._update_status("maven")
        self.process_manager.run(cmd_parts, cwd=cwd, env=self._get_env())

    def _run_custom_maven(self):
        dialog = ctk.CTkInputDialog(text="Enter Maven command (e.g. 'compile package -DskipTests'):",
                                     title="Custom Maven Command")
        result = dialog.get_input()
        if result and result.strip():
            if self.process_manager.is_running():
                self._log("[WARN] A process is already running. Stop it first.\n")
                return

            mvn = self._get_maven_cmd()
            cwd = self._get_project_path()
            if not cwd:
                self._log("[ERROR] Project path is not set.\n")
                return

            cmd_parts = [mvn] + result.strip().split()
            self._log(f"[MAVEN] Running: {' '.join(cmd_parts)}\n")
            self._update_status("maven")
            self.process_manager.run(cmd_parts, cwd=cwd, env=self._get_env())

    def run_app(self):
        if self.process_manager.is_running():
            self._log("[WARN] A process is already running. Stop it first.\n")
            return

        project_type = self.setting_vars["project_type"].get()
        cwd = self._get_project_path()
        if not cwd:
            self._log("[ERROR] Project path is not set.\n")
            return

        if project_type == "spring-boot":
            self._run_spring_boot(cwd)
        elif project_type == "npm":
            self._run_npm(cwd)

    def _run_spring_boot(self, cwd):
        main_class = self.setting_vars["main_class"].get().strip()
        vm_options = self.setting_vars["vm_options"].get().strip()
        program_args = self.setting_vars["program_args"].get().strip()
        spring_profile = self.setting_vars["spring_profile"].get().strip()
        server_port = self.setting_vars["server_port"].get().strip()
        override_port = self.setting_vars["override_port"].get()
        debug_enabled = self.setting_vars["debug_enabled"].get()
        debug_port = self.setting_vars["debug_port"].get().strip()

        mvn = self._get_maven_cmd()
        cmd_parts = [mvn, "spring-boot:run"]

        # Skip JaCoCo to avoid instrumentation errors
        skip_jacoco = self.setting_vars["skip_jacoco"].get()
        if skip_jacoco:
            cmd_parts.append("-Djacoco.skip=true")

        jvm_args = []
        if vm_options:
            jvm_args.extend(vm_options.split())
        if debug_enabled and debug_port:
            # Java 8 uses address=5005, Java 9+ uses address=*:5005
            java_cmd = self._get_java_cmd()
            java_major = self._get_java_major_version(java_cmd)
            if java_major >= 9:
                debug_address = f"*:{debug_port}"
            else:
                debug_address = debug_port
            jvm_args.append(f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address={debug_address}")

        # Console logging override
        console_logging = self.setting_vars["console_logging"].get()
        if console_logging:
            config_path = CONSOLE_LOG4J2_CONFIG.replace("\\", "/")
            jvm_args.append(f"-Dlogging.config=file:{config_path}")
            jvm_args.append(f"-Dlog4j.configurationFile=file:{config_path}")

        if main_class:
            cmd_parts.append(f"-Dspring-boot.run.mainClass={main_class}")
        if spring_profile:
            cmd_parts.append(f"-Dspring-boot.run.profiles={spring_profile}")

        # Collect run arguments
        run_args = []
        if override_port and server_port:
            run_args.append(f"--server.port={server_port}")
        datasource_args = self._build_datasource_run_args()
        if datasource_args:
            run_args.extend(datasource_args)
        if program_args:
            run_args.extend(program_args.split())
        if run_args:
            cmd_parts.append(f"-Dspring-boot.run.arguments={','.join(run_args)}")

        if jvm_args:
            cmd_parts.append(f"-Dspring-boot.run.jvmArguments={' '.join(jvm_args)}")

        profiles = self.setting_vars["maven_profiles"].get().strip()
        if profiles:
            cmd_parts.extend(profiles.split())

        self._log(f"[RUN] Starting Spring Boot application...\n")
        self._log(f"[RUN] Command: {' '.join(cmd_parts)}\n")
        self._log(f"[RUN] Directory: {cwd}\n")
        for category in ("primary", "readonly"):
            datasource = self._get_selected_datasource(category)
            if datasource:
                environment = _normalize_datasource_environment(datasource.get("environment"))
                self._log(f"[RUN] {category.title()} datasource: {datasource.get('name', '')} ({environment})\n")
        if debug_enabled:
            self._log(f"[RUN] Debug port: {debug_port} (attach IntelliJ remote debugger to this port)\n")
        self._update_status("running")
        self.process_manager.run(cmd_parts, cwd=cwd, env=self._get_env())

    def _run_npm(self, cwd):
        npm = self._get_npm_cmd()
        script = self.setting_vars["npm_script"].get().strip() or "start"

        cmd_parts = [npm, "run", script]
        self._log(f"[RUN] Starting NPM application...\n")
        self._log(f"[RUN] Command: {' '.join(cmd_parts)}\n")
        self._log(f"[RUN] Directory: {cwd}\n")
        self._update_status("running")
        self.process_manager.run(cmd_parts, cwd=cwd, env=self._get_env())

    def stop_process(self):
        if self.process_manager.is_running():
            self._log("[STOP] Stopping process...\n")
            self.process_manager.stop()
        else:
            self._log("[INFO] No running process.\n")

    def destroy(self):
        self.stop_process()
        self.frame.destroy()


class AppLauncher(ctk.CTk):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1400x800")
        self.minsize(900, 500)

        # Set window icon
        icon_path = os.path.join(_BASE_DIR, "app_icon.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(_BUNDLE_DIR, "app_icon.ico")
        if os.path.exists(icon_path):
            self.iconbitmap(icon_path)

        self.tabs = {}  # tab_id -> ProjectTab
        self._current_theme = "dark"
        self.datasources = []

        self._load_settings_and_build()

    def _load_settings_and_build(self):
        saved = self._load_settings_file()
        self._current_theme = saved.get("theme", "dark")
        self.datasources = self._load_datasources_file()
        if not self.datasources and saved.get("datasources"):
            self.datasources = self._normalize_datasources(saved.get("datasources", []))
        ctk.set_appearance_mode(self._current_theme)
        ctk.set_default_color_theme("blue")

        # Restore window geometry
        geometry = saved.get("geometry")
        if geometry:
            try:
                self.geometry(geometry)
            except Exception:
                pass

        self._build_ui()

        # Restore tabs
        tab_configs = saved.get("tabs", [])
        if tab_configs:
            for tc in tab_configs:
                self._add_tab(config=tc.get("config"), tab_id=tc.get("tab_id"))
        else:
            self._add_tab()

    def _build_ui(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Top bar
        topbar = ctk.CTkFrame(self, height=46, corner_radius=10, fg_color=("gray88", "gray18"))
        topbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 3))

        btn_add = ctk.CTkButton(topbar, text="+ New Tab", width=110, height=32, corner_radius=6,
                                fg_color="#27ae60", hover_color="#2ecc71",
                                font=ctk.CTkFont(size=13, weight="bold"),
                                command=lambda: self._add_tab())
        btn_add.pack(side="left", padx=6, pady=7)

        btn_close = ctk.CTkButton(topbar, text="✕ Close Tab", width=100, height=32, corner_radius=6,
                                  fg_color="#c0392b", hover_color="#e74c3c",
                                  font=ctk.CTkFont(size=12),
                                  command=self._close_current_tab)
        btn_close.pack(side="left", padx=4, pady=7)

        btn_rename = ctk.CTkButton(topbar, text="✎ Rename", width=100, height=32, corner_radius=6,
                                   fg_color=("gray65", "gray35"), hover_color=("gray55", "gray45"),
                                   font=ctk.CTkFont(size=12),
                                   command=self._rename_current_tab)
        btn_rename.pack(side="left", padx=4, pady=7)

        btn_datasources = ctk.CTkButton(topbar, text="Datasources", width=120, height=32, corner_radius=6,
                                        fg_color="#16a085", hover_color="#1abc9c",
                                        font=ctk.CTkFont(size=12, weight="bold"),
                                        command=self._open_datasource_manager)
        btn_datasources.pack(side="left", padx=4, pady=7)

        # Right side of topbar
        self.theme_btn = ctk.CTkButton(topbar,
                                       text="🌙 Dark" if self._current_theme == "dark" else "☀ Light",
                                       width=90, height=32, corner_radius=6,
                                       fg_color=("gray65", "gray35"), hover_color=("gray55", "gray45"),
                                       font=ctk.CTkFont(size=12),
                                       command=self._toggle_theme)
        self.theme_btn.pack(side="right", padx=6, pady=7)

        btn_import = ctk.CTkButton(topbar, text="⬇ Import", width=85, height=32, corner_radius=6,
                                   fg_color=("gray65", "gray35"), hover_color=("gray55", "gray45"),
                                   font=ctk.CTkFont(size=12),
                                   command=self._import_settings)
        btn_import.pack(side="right", padx=3, pady=7)

        btn_export = ctk.CTkButton(topbar, text="⬆ Export", width=85, height=32, corner_radius=6,
                                   fg_color=("gray65", "gray35"), hover_color=("gray55", "gray45"),
                                   font=ctk.CTkFont(size=12),
                                   command=self._export_settings)
        btn_export.pack(side="right", padx=3, pady=7)

        # Tab view with larger segmented button
        self.tabview = ctk.CTkTabview(self, corner_radius=10,
                                      segmented_button_fg_color=("gray80", "gray25"),
                                      segmented_button_selected_color=("#2980b9", "#2471a3"),
                                      segmented_button_unselected_color=("gray75", "gray30"),
                                      segmented_button_selected_hover_color=("#3498db", "#2e86c1"))
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=8, pady=(3, 8))
        # Make tab buttons larger
        try:
            self.tabview._segmented_button.configure(
                font=ctk.CTkFont(size=14, weight="bold"),
                height=36
            )
        except Exception:
            pass

        # When a tab is selected, restore its sash if not yet set
        try:
            self.tabview._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        except Exception:
            pass

    def _on_tab_changed(self, event=None):
        """Restore sash for newly selected tab if it hasn't been set yet."""
        try:
            current_name = self.tabview.get()
            for tab_data in self.tabs.values():
                if tab_data["tab_name"] == current_name:
                    proj = tab_data["project"]
                    if not proj._sash_set:
                        proj._sash_retries = 0
                        proj.frame.after(100, proj._set_initial_sash)
                    break
        except Exception:
            pass

    def _add_tab(self, config=None, tab_id=None):
        name = (config or {}).get("name", f"Project {len(self.tabs) + 1}")
        tab_name = self._unique_tab_name(name)

        self.tabview.add(tab_name)
        tab_frame = self.tabview.tab(tab_name)
        tab_frame.grid_rowconfigure(0, weight=1)
        tab_frame.grid_columnconfigure(0, weight=1)

        if config and config.get("name") != tab_name:
            config["name"] = tab_name

        project = ProjectTab(tab_frame, self, tab_id=tab_id, config=config)
        project.frame.grid(row=0, column=0, sticky="nsew")

        self.tabs[project.tab_id] = {"project": project, "tab_name": tab_name}
        self.tabview.set(tab_name)

    def _unique_tab_name(self, base):
        existing = {v["tab_name"] for v in self.tabs.values()}
        if base not in existing:
            return base
        i = 2
        while f"{base} ({i})" in existing:
            i += 1
        return f"{base} ({i})"

    def _get_current_tab_id(self):
        current_name = self.tabview.get()
        for tid, info in self.tabs.items():
            if info["tab_name"] == current_name:
                return tid
        return None

    def _close_current_tab(self):
        tid = self._get_current_tab_id()
        if tid is None:
            return
        if len(self.tabs) <= 1:
            messagebox.showwarning("Warning", "Cannot close the last tab.")
            return

        info = self.tabs[tid]
        info["project"].destroy()
        self.tabview.delete(info["tab_name"])
        del self.tabs[tid]

    def _rename_current_tab(self):
        tid = self._get_current_tab_id()
        if tid is None:
            return

        dialog = ctk.CTkInputDialog(text="Enter new tab name:", title="Rename Tab")
        new_name = dialog.get_input()
        if new_name and new_name.strip():
            new_name = new_name.strip()
            old_name = self.tabs[tid]["tab_name"]

            # CTkTabview doesn't support rename directly, so we update the name var
            self.tabs[tid]["project"].setting_vars["name"].set(new_name)
            self.tabs[tid]["tab_name"] = new_name

            # Recreate the tab (CTkTabview limitation)
            # We'll just update the config name; actual visual rename requires re-adding
            # For simplicity, the name updates on next restart
            messagebox.showinfo("Info", f"Tab renamed to '{new_name}'. Visual update on next app restart or tab re-creation.")

    def _toggle_theme(self):
        if self._current_theme == "dark":
            self._current_theme = "light"
            ctk.set_appearance_mode("light")
            self.theme_btn.configure(text="☀ Light")
        else:
            self._current_theme = "dark"
            ctk.set_appearance_mode("dark")
            self.theme_btn.configure(text="🌙 Dark")

    # ========================
    # DATASOURCES
    # ========================
    def _normalize_datasources(self, datasources):
        normalized = []
        if not isinstance(datasources, list):
            return normalized
        for item in datasources:
            if not isinstance(item, dict):
                continue
            normalized.append({
                "id": item.get("id") or _new_datasource_id(),
                "name": item.get("name") or "Datasource",
                "category": _normalize_datasource_category(item.get("category", item.get("type"))),
                "environment": _normalize_datasource_environment(item.get("environment")),
                "jdbc_url": item.get("jdbc_url") or item.get("url") or "",
                "username": item.get("username") or "",
                "password": item.get("password") or "",
                "driver_class_name": item.get("driver_class_name") or item.get("driver-class-name") or "org.postgresql.Driver",
            })
        return normalized

    def get_datasource_labels(self, category):
        return ["None"] + [
            self.get_datasource_label(ds.get("id"), category)
            for ds in self.datasources
            if ds.get("category") == category
        ]

    def get_datasource_label(self, datasource_id, category=None):
        datasource = self.get_datasource_by_id(datasource_id)
        if not datasource or (category and datasource.get("category") != category):
            return "None"
        environment = _normalize_datasource_environment(datasource.get("environment"))
        return f"{datasource.get('name', 'Datasource')} [{environment}]"

    def get_datasource_by_id(self, datasource_id):
        if not datasource_id:
            return None
        for datasource in self.datasources:
            if datasource.get("id") == datasource_id:
                return datasource
        return None

    def get_datasource_by_label(self, label, category=None):
        if not label or label == "None":
            return None
        for datasource in self.datasources:
            if self.get_datasource_label(datasource.get("id"), category) == label:
                return datasource
        return None

    def _refresh_datasource_menus(self):
        for info in self.tabs.values():
            info["project"].refresh_datasource_options()

    def _open_datasource_manager(self):
        window = ctk.CTkToplevel(self)
        window.title("Datasources")
        window.geometry("760x480")
        window.transient(self)
        window.grab_set()
        window.grid_rowconfigure(0, weight=1)
        window.grid_columnconfigure(1, weight=1)

        list_frame = ctk.CTkFrame(window, corner_radius=8)
        list_frame.grid(row=0, column=0, sticky="ns", padx=(10, 5), pady=10)
        list_frame.grid_rowconfigure(0, weight=1)

        detail_frame = ctk.CTkFrame(window, corner_radius=8)
        detail_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        detail_frame.grid_columnconfigure(1, weight=1)

        list_scroll = ctk.CTkScrollableFrame(list_frame, width=230, corner_radius=6)
        list_scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))

        selected_id = ctk.StringVar(value=self.datasources[0]["id"] if self.datasources else "")
        form_vars = {
            "name": ctk.StringVar(),
            "category": ctk.StringVar(value="primary"),
            "environment": ctk.StringVar(value="local"),
            "jdbc_url": ctk.StringVar(),
            "username": ctk.StringVar(),
            "password": ctk.StringVar(),
            "driver_class_name": ctk.StringVar(value="org.postgresql.Driver"),
        }

        fields = [
            ("name", "Name", None),
            ("category", "Category", DATASOURCE_CATEGORY_OPTIONS),
            ("environment", "Environment", DATASOURCE_ENVIRONMENT_OPTIONS),
            ("jdbc_url", "JDBC URL", None),
            ("username", "Username", None),
            ("password", "Password", None),
            ("driver_class_name", "Driver", None),
        ]
        for row, (key, label, values) in enumerate(fields):
            ctk.CTkLabel(detail_frame, text=label, anchor="w").grid(
                row=row, column=0, sticky="w", padx=(12, 6), pady=6
            )
            if values:
                widget = ctk.CTkOptionMenu(detail_frame, variable=form_vars[key], values=values,
                                           height=30, corner_radius=6)
            else:
                widget = ctk.CTkEntry(detail_frame, textvariable=form_vars[key],
                                      show="*" if key == "password" else "",
                                      height=30, corner_radius=6)
            widget.grid(row=row, column=1, sticky="ew", padx=(6, 12), pady=6)

        help_label = ctk.CTkLabel(
            detail_frame,
            text="Category maps to Spring args. Environment only controls the datasource color.",
            anchor="w",
            text_color=("gray35", "gray70"),
            font=ctk.CTkFont(size=11),
        )
        help_label.grid(row=len(fields), column=0, columnspan=2, sticky="ew", padx=12, pady=(4, 8))

        def load_selected():
            datasource = self.get_datasource_by_id(selected_id.get())
            defaults = {
                "category": "primary",
                "environment": "local",
                "driver_class_name": "org.postgresql.Driver",
            }
            for key, var in form_vars.items():
                var.set(datasource.get(key, "") if datasource else defaults.get(key, ""))

        def rebuild_list():
            for child in list_scroll.winfo_children():
                child.destroy()
            for datasource in self.datasources:
                category = _normalize_datasource_category(datasource.get("category"))
                environment = _normalize_datasource_environment(datasource.get("environment"))
                button = ctk.CTkButton(
                    list_scroll,
                    text=f"{datasource.get('name', 'Datasource')}  [{category}] [{environment}]",
                    height=32,
                    corner_radius=6,
                    fg_color=_datasource_color(environment),
                    hover_color=_datasource_color(environment),
                    command=lambda ds_id=datasource.get("id"): (selected_id.set(ds_id), load_selected()),
                )
                button.pack(fill="x", padx=4, pady=3)

        def add_datasource():
            datasource = {
                "id": _new_datasource_id(),
                "name": "New Datasource",
                "category": "primary",
                "environment": "local",
                "jdbc_url": "",
                "username": "",
                "password": "",
                "driver_class_name": "org.postgresql.Driver",
            }
            self.datasources.append(datasource)
            selected_id.set(datasource["id"])
            rebuild_list()
            load_selected()
            self._refresh_datasource_menus()

        def save_datasource():
            datasource = self.get_datasource_by_id(selected_id.get())
            if not datasource:
                add_datasource()
                datasource = self.get_datasource_by_id(selected_id.get())
            datasource["name"] = form_vars["name"].get().strip() or "Datasource"
            datasource["category"] = _normalize_datasource_category(form_vars["category"].get())
            datasource["environment"] = _normalize_datasource_environment(form_vars["environment"].get())
            datasource["jdbc_url"] = form_vars["jdbc_url"].get().strip()
            datasource["username"] = form_vars["username"].get().strip()
            datasource["password"] = form_vars["password"].get()
            datasource["driver_class_name"] = form_vars["driver_class_name"].get().strip()
            rebuild_list()
            self._refresh_datasource_menus()
            self._save_settings()

        def delete_datasource():
            datasource_id = selected_id.get()
            if not datasource_id:
                return
            datasource = self.get_datasource_by_id(datasource_id)
            if datasource and not messagebox.askyesno("Delete Datasource", f"Delete '{datasource.get('name')}'?"):
                return
            self.datasources = [ds for ds in self.datasources if ds.get("id") != datasource_id]
            selected_id.set(self.datasources[0]["id"] if self.datasources else "")
            rebuild_list()
            load_selected()
            self._refresh_datasource_menus()
            self._save_settings()

        actions = ctk.CTkFrame(list_frame, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 8))
        actions.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(actions, text="Add", height=30, corner_radius=6,
                      fg_color="#27ae60", hover_color="#2ecc71",
                      command=add_datasource).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ctk.CTkButton(actions, text="Delete", height=30, corner_radius=6,
                      fg_color="#c0392b", hover_color="#e74c3c",
                      command=delete_datasource).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        bottom = ctk.CTkFrame(detail_frame, fg_color="transparent")
        bottom.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 12))
        bottom.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(bottom, text="Save", height=32, corner_radius=6,
                      fg_color="#2980b9", hover_color="#3498db",
                      command=save_datasource).grid(row=0, column=1, padx=(6, 0))
        ctk.CTkButton(bottom, text="Close", height=32, corner_radius=6,
                      fg_color=("gray65", "gray35"), hover_color=("gray55", "gray45"),
                      command=window.destroy).grid(row=0, column=2, padx=(6, 0))

        rebuild_list()
        load_selected()

    # ========================
    # PERSISTENCE
    # ========================
    def _collect_save_data(self):
        tabs_data = []
        for tid, info in self.tabs.items():
            tabs_data.append({
                "tab_id": tid,
                "config": info["project"].get_config(),
            })
        return {
            "theme": self._current_theme,
            "geometry": self.geometry(),
            "datasources": self.datasources,
            "tabs": tabs_data,
        }

    def _save_settings(self):
        data = self._collect_save_data()
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._save_datasources_file()
        except Exception as e:
            print(f"Failed to save settings: {e}")

    def _save_datasources_file(self):
        try:
            with open(DATASOURCES_FILE, "w", encoding="utf-8") as f:
                json.dump({"datasources": self.datasources}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save datasources: {e}")

    def _load_datasources_file(self):
        if os.path.exists(DATASOURCES_FILE):
            try:
                with open(DATASOURCES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._normalize_datasources(data.get("datasources", []))
            except Exception:
                pass
        return []

    def _load_settings_file(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _export_settings(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            title="Export Settings",
        )
        if path:
            data = self._collect_save_data()
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                messagebox.showinfo("Export", f"Settings exported to:\n{path}")
            except Exception as e:
                messagebox.showerror("Error", f"Export failed: {e}")

    def _import_settings(self):
        path = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json")],
            title="Import Settings",
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Close all tabs
                for tid in list(self.tabs.keys()):
                    self.tabs[tid]["project"].destroy()
                    self.tabview.delete(self.tabs[tid]["tab_name"])
                self.tabs.clear()

                # Load new tabs
                self._current_theme = data.get("theme", "dark")
                self.datasources = self._normalize_datasources(data.get("datasources", []))
                ctk.set_appearance_mode(self._current_theme)
                self.theme_btn.configure(text="🌙 Dark" if self._current_theme == "dark" else "☀ Light")

                for tc in data.get("tabs", []):
                    self._add_tab(config=tc.get("config"), tab_id=tc.get("tab_id"))

                if not self.tabs:
                    self._add_tab()

                messagebox.showinfo("Import", "Settings imported successfully.")
            except Exception as e:
                messagebox.showerror("Error", f"Import failed: {e}")

    def on_closing(self):
        # Check for running processes
        running = []
        for tid, info in self.tabs.items():
            if info["project"].process_manager.is_running():
                name = info["project"].setting_vars.get("name")
                running.append(name.get() if name else info["tab_name"])

        if running:
            msg = f"The following applications are still running:\n\n"
            msg += "\n".join(f"  • {name}" for name in running)
            msg += "\n\nThey will be stopped. Do you want to exit?"
            if not messagebox.askyesno("Confirm Exit", msg, icon="warning"):
                return

        # Stop all processes before exiting
        for tid, info in self.tabs.items():
            if info["project"].process_manager.is_running():
                info["project"].stop_process()

        # Wait briefly for processes to terminate
        for tid, info in self.tabs.items():
            proc = info["project"].process_manager.process
            if proc:
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        self._save_settings()
        self.destroy()

        self._save_settings()
        self.destroy()


def main():
    app = AppLauncher()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
