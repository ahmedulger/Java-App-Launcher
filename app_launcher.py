import customtkinter as ctk
import subprocess
import threading
import json
import os
import sys
import signal
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime

# --- Constants ---
APP_TITLE = "App Launcher"
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_launcher_settings.json")
DEFAULT_MAVEN_PATH = "mvn"
DEFAULT_JAVA_PATH = "java"
DEFAULT_NODE_PATH = "npm"


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
            try:
                if sys.platform == "win32":
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.process.terminate()
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

        # --- Main Frame ---
        self.frame = ctk.CTkFrame(parent_notebook)

        # --- Split Pane: Logs (left 50%) | Settings (right 50%) ---
        self.frame.grid_columnconfigure(0, weight=1, uniform="pane")
        self.frame.grid_columnconfigure(1, weight=0)
        self.frame.grid_columnconfigure(2, weight=1, uniform="pane")
        self.frame.grid_rowconfigure(0, weight=1)

        self._build_log_panel()
        self._build_separator()
        self._build_settings_panel()

        self._poll_output()

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
            "npm_script": "start",
            "spring_profile": "",
            "server_port": "8080",
        }

    # ========================
    # LOG PANEL (LEFT)
    # ========================
    def _build_log_panel(self):
        log_frame = ctk.CTkFrame(self.frame, corner_radius=10)
        log_frame.grid(row=0, column=0, sticky="nsew", padx=(8, 3), pady=8)
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = ctk.CTkFrame(log_frame, height=44, corner_radius=8, fg_color=("gray86", "gray20"))
        toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 3))

        self.status_label = ctk.CTkLabel(toolbar, text="● STOPPED", text_color="gray",
                                         font=ctk.CTkFont(size=13, weight="bold"))
        self.status_label.pack(side="left", padx=8)

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
                                       font=ctk.CTkFont(family="Consolas", size=12),
                                       state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=6, pady=(3, 6))

    def _build_separator(self):
        sep = ctk.CTkFrame(self.frame, width=2, fg_color=("gray75", "gray35"))
        sep.grid(row=0, column=1, sticky="ns", padx=0, pady=10)

    # ========================
    # SETTINGS PANEL (RIGHT)
    # ========================
    def _build_settings_panel(self):
        settings_outer = ctk.CTkFrame(self.frame, corner_radius=10)
        settings_outer.grid(row=0, column=2, sticky="nsew", padx=(3, 8), pady=8)
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
        row = self._add_entry("server_port", "Server Port", row, self.config["server_port"],
                              placeholder="8080")

        # --- Group: Debug ---
        row = self._add_group_header("Debug", row)
        row = self._add_checkbox("debug_enabled", "Enable Debug", row, self.config["debug_enabled"])
        row = self._add_entry("debug_port", "Debug Port", row, self.config["debug_port"],
                              placeholder="5005")

        # --- Group: Maven Settings ---
        row = self._add_group_header("Maven Settings", row)
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
                                    command=lambda: var.set(default_value))
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
            cfg[key] = var.get()
        return cfg

    def _get_java_cmd(self):
        java_path = self.setting_vars["java_path"].get().strip()
        if java_path and os.path.isdir(java_path):
            candidate = os.path.join(java_path, "bin", "java.exe" if sys.platform == "win32" else "java")
            if os.path.isfile(candidate):
                return candidate
            return os.path.join(java_path, "bin", "java")
        return java_path or DEFAULT_JAVA_PATH

    def _get_maven_cmd(self):
        mvn_path = self.setting_vars["maven_path"].get().strip()
        if mvn_path and os.path.isdir(mvn_path):
            ext = ".cmd" if sys.platform == "win32" else ""
            candidate = os.path.join(mvn_path, "bin", f"mvn{ext}")
            if os.path.isfile(candidate):
                return candidate
            return os.path.join(mvn_path, "bin", "mvn")
        return mvn_path or DEFAULT_MAVEN_PATH

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
            # Check if it's a nodejs installation directory (contains npm.cmd)
            ext = ".cmd" if sys.platform == "win32" else ""
            for candidate_dir in [node_path, os.path.join(node_path, "bin")]:
                candidate = os.path.join(candidate_dir, f"npm{ext}")
                if os.path.isfile(candidate):
                    return candidate
        return node_path or DEFAULT_NODE_PATH

    def _log(self, text):
        self.output_queue.put(text)

    def _on_process_output(self, text):
        self._log(text)

    def _on_process_exit(self, exit_code):
        self._log(f"\n[Process exited with code {exit_code}]\n")
        self.output_queue.put(("__STATUS__", "stopped"))

    def _poll_output(self):
        try:
            while True:
                item = self.output_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__STATUS__":
                    self._update_status(item[1])
                else:
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", item)
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
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
        debug_enabled = self.setting_vars["debug_enabled"].get()
        debug_port = self.setting_vars["debug_port"].get().strip()

        mvn = self._get_maven_cmd()
        cmd_parts = [mvn, "spring-boot:run"]

        jvm_args = []
        if vm_options:
            jvm_args.extend(vm_options.split())
        if debug_enabled and debug_port:
            jvm_args.append(f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:{debug_port}")

        if main_class:
            cmd_parts.append(f"-Dspring-boot.run.mainClass={main_class}")
        if spring_profile:
            cmd_parts.append(f"-Dspring-boot.run.profiles={spring_profile}")

        # Collect run arguments
        run_args = []
        if server_port:
            run_args.append(f"--server.port={server_port}")
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

        self.tabs = {}  # tab_id -> ProjectTab
        self._current_theme = "dark"

        self._load_settings_and_build()

    def _load_settings_and_build(self):
        saved = self._load_settings_file()
        self._current_theme = saved.get("theme", "dark")
        ctk.set_appearance_mode(self._current_theme)
        ctk.set_default_color_theme("blue")

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
            "tabs": tabs_data,
        }

    def _save_settings(self):
        data = self._collect_save_data()
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save settings: {e}")

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
