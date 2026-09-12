"""ByteSqueeze Windows Worker desktop host.

This is a small native control surface around the existing headless worker.
Encoding, pairing, transfer, retry, and queue behavior remain in the shared
worker modules; this file owns Windows paths, lifecycle, tray, and diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


APP_NAME = "ByteSqueeze Worker"
CONFIG_VERSION = 1
FAMILY_LABELS = {
    "auto": "Auto (best available)",
    "nvenc": "NVIDIA NVENC",
    "vce": "AMD VCN/VCE",
    "qsv": "Intel Quick Sync",
    "software": "CPU / software",
}


def application_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ByteSqueeze Worker"


def resource_root() -> Path:
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen) if frozen else Path(__file__).resolve().parents[1]


def default_config() -> dict:
    root = application_root()
    return {
        "version": CONFIG_VERSION,
        "worker_name": f"{socket.gethostname()} ByteSqueeze Worker",
        "port": 8082,
        "work_dir": str(root / "jobs"),
        "hardware_slots": 1,
        "preferred_encoder_family": "auto",
        "start_minimized": False,
        "run_at_login": False,
    }


def config_path() -> Path:
    return application_root() / "windows-worker.json"


def load_config() -> dict:
    config = default_config()
    try:
        loaded = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config.update({key: loaded[key] for key in config if key in loaded})
    except (OSError, ValueError):
        pass
    try:
        config["port"] = max(1024, min(65535, int(config["port"])))
        config["hardware_slots"] = max(1, min(8, int(config["hardware_slots"])))
    except (TypeError, ValueError):
        config.update({"port": 8082, "hardware_slots": 1})
    if config.get("preferred_encoder_family") not in FAMILY_LABELS:
        config["preferred_encoder_family"] = "auto"
    return config


def save_config(config: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.replace(temp, path)


def configure_environment(config: dict) -> None:
    root = application_root()
    state_dir = root / "state"
    work_dir = Path(str(config["work_dir"])).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    resources = resource_root()
    presets = resources / "presets"
    tools_dir = resources / "tools"
    os.environ.update({
        "TSD_WORKER_MODE": "1",
        "TSD_WINDOWS_WORKER": "1",
        "TSD_WORKER_NAME": str(config["worker_name"]),
        "TSD_WORKER_TEMP_DIR": str(work_dir),
        "TSD_PREFERRED_ENCODER_FAMILY": str(config["preferred_encoder_family"]),
        "TSD_ENCODER_FAMILIES": "nvenc,vce,qsv,software",
        "HB_DATA_DIR": str(state_dir),
        "HB_MEDIA_BASE": str(work_dir),
        "HB_ROOTS_JSON": json.dumps([[str(work_dir), "Worker jobs"]]),
        "HB_PRESET_DIR": str(presets),
    })
    if tools_dir.is_dir():
        os.environ["PATH"] = str(tools_dir) + os.pathsep + os.environ.get("PATH", "")


def configure_runtime_tools() -> None:
    """Expose bundled executables for the worker's private encode child."""
    tools_dir = resource_root() / "tools"
    if tools_dir.is_dir():
        current = os.environ.get("PATH", "")
        if str(tools_dir).casefold() not in current.casefold():
            os.environ["PATH"] = str(tools_dir) + os.pathsep + current


def local_addresses(port: int) -> list[str]:
    hosts = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(item[4][0])
            if not address.startswith("127."):
                hosts.add(address)
    except OSError:
        pass
    return [f"http://{host}:{port}" for host in sorted(hosts)] or [f"http://127.0.0.1:{port}"]


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def set_run_at_login(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            command = f'"{sys.executable}" --minimized' if getattr(sys, "frozen", False) else ""
            if command:
                winreg.SetValueEx(key, "ByteSqueezeWorker", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "ByteSqueezeWorker")
            except FileNotFoundError:
                pass


class WorkerRuntime:
    def __init__(self, config: dict, log_callback=None):
        self.config = config
        self.log_callback = log_callback or (lambda _line: None)
        self.server = None
        self.thread = None
        self.error = ""

    def log(self, message: str) -> None:
        self.log_callback(f"{time.strftime('%H:%M:%S')}  {message}")

    def start(self) -> None:
        try:
            configure_environment(self.config)
            from werkzeug.serving import make_server
            from webui.app.settings import save_settings
            from worker.app import create_worker_app

            save_settings({"hardware_transcode_concurrency": int(self.config["hardware_slots"])})
            app = create_worker_app(announce_pairing=False)
            self.server = make_server("0.0.0.0", int(self.config["port"]), app, threaded=True)
            self.thread = threading.Thread(target=self.server.serve_forever, name="windows-worker-api", daemon=True)
            self.thread.start()
            self.log(f"Worker listening on {', '.join(local_addresses(int(self.config['port'])))}")
        except (Exception, SystemExit) as exc:
            self.error = str(exc)
            self.log(f"Startup failed: {exc}")

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:
                pass

    def new_pairing_code(self) -> dict:
        from webui.app.node_linking import create_pairing_code

        pairing = create_pairing_code(ttl_seconds=3600)
        self.log(f"New pairing code generated: {pairing.get('code')}")
        return pairing

    def apply_live_settings(self, config: dict) -> dict:
        self.config = config
        os.environ["TSD_WORKER_NAME"] = str(config["worker_name"])
        os.environ["TSD_PREFERRED_ENCODER_FAMILY"] = str(config["preferred_encoder_family"])
        from webui.app.node_linking import encoder_hardware_profile, set_local_node_name
        from webui.app.settings import save_settings

        set_local_node_name(str(config["worker_name"]))
        save_settings({"hardware_transcode_concurrency": int(config["hardware_slots"])})
        profile = encoder_hardware_profile(force=True)
        self.log(
            f"Settings saved: {config['hardware_slots']} encode slot(s), "
            f"{FAMILY_LABELS[config['preferred_encoder_family']]}"
        )
        return profile

    def snapshot(self, *, refresh_hardware: bool = False) -> dict:
        try:
            from webui.app.jobs import get_job_summary, list_jobs_for_api
            from webui.app.node_linking import (
                encoder_hardware_profile,
                list_trusted_controllers_public,
                local_node_overview,
            )

            work = Path(str(self.config["work_dir"]))
            usage = shutil.disk_usage(work)
            return {
                "ok": not self.error,
                "error": self.error,
                "local": local_node_overview(),
                "controllers": list_trusted_controllers_public(),
                "hardware": encoder_hardware_profile(force=refresh_hardware),
                "summary": get_job_summary(),
                "jobs": list_jobs_for_api(include_log_tail=False),
                "free_bytes": usage.free,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "controllers": [], "hardware": {}, "summary": {}, "jobs": []}


class WorkerWindow:
    def __init__(self, config: dict):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.config = config
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("860x660")
        self.root.minsize(720, 560)
        self.root.configure(bg="#090d12")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.log_lines: list[str] = []
        self.runtime = WorkerRuntime(config, self.append_log)
        self.pairing = {}
        self.tray = None
        self._make_styles()
        self._build()
        self.runtime.start()
        if not self.runtime.error:
            self.pairing = self.runtime.new_pairing_code()
            self._show_pairing()
        self._start_tray()
        self.refresh(force=True)
        self.root.after(2500, self._poll)

    def _make_styles(self) -> None:
        style = self.ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#090d12")
        style.configure("Card.TFrame", background="#111821", relief="flat")
        style.configure("TLabel", background="#090d12", foreground="#edf6f8", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", foreground="#91a0aa", font=("Segoe UI", 9))
        style.configure("Card.TLabel", background="#111821", foreground="#edf6f8", font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background="#111821", foreground="#edf6f8", font=("Segoe UI Semibold", 12))
        style.configure("Hero.TLabel", foreground="#ffffff", font=("Segoe UI Semibold", 19))
        style.configure("Status.TLabel", foreground="#55d9e8", font=("Segoe UI Semibold", 10))
        style.configure("TButton", background="#1a2730", foreground="#e9f7f9", borderwidth=0, padding=(12, 8), font=("Segoe UI Semibold", 9))
        style.map("TButton", background=[("active", "#263844")])
        style.configure("Accent.TButton", background="#46cede", foreground="#061014", padding=(14, 9))
        style.map("Accent.TButton", background=[("active", "#70e0eb")])
        style.configure("TEntry", fieldbackground="#0b1118", foreground="#edf6f8", insertcolor="#edf6f8", bordercolor="#26323c", padding=8)
        style.configure("TCombobox", fieldbackground="#0b1118", background="#0b1118", foreground="#edf6f8", arrowcolor="#9fadb5", padding=7)
        style.configure("Horizontal.TProgressbar", background="#46cede", troughcolor="#1a232b", bordercolor="#1a232b", lightcolor="#46cede", darkcolor="#46cede")

    def _card(self, parent, column: int, row: int, title: str, *, columnspan: int = 1):
        frame = self.ttk.Frame(parent, style="Card.TFrame", padding=16)
        frame.grid(column=column, row=row, columnspan=columnspan, sticky="nsew", padx=5, pady=5)
        self.ttk.Label(frame, text=title, style="CardTitle.TLabel").grid(column=0, row=0, sticky="w", columnspan=4, pady=(0, 10))
        return frame

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="ByteSqueeze Worker", style="Hero.TLabel").pack(side="left")
        self.status_label = ttk.Label(header, text="● STARTING", style="Status.TLabel")
        self.status_label.pack(side="right")

        grid = ttk.Frame(outer)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.rowconfigure(2, weight=1)

        pairing = self._card(grid, 0, 0, "Pair this worker")
        self.pairing_var = tk.StringVar(value="Starting…")
        ttk.Label(pairing, textvariable=self.pairing_var, style="Card.TLabel", font=("Consolas", 23, "bold")).grid(column=0, row=1, sticky="w")
        self.pair_expiry = tk.StringVar(value="")
        ttk.Label(pairing, textvariable=self.pair_expiry, style="Card.TLabel", foreground="#91a0aa").grid(column=0, row=2, sticky="w", pady=(2, 10))
        ttk.Button(pairing, text="Copy code", command=self.copy_pairing).grid(column=0, row=3, sticky="w")
        ttk.Button(pairing, text="New code", command=self.new_pairing).grid(column=1, row=3, sticky="w", padx=(8, 0))
        self.controller_var = tk.StringVar(value="Not paired yet")
        ttk.Label(pairing, textvariable=self.controller_var, style="Card.TLabel", foreground="#91a0aa", wraplength=340).grid(column=0, row=4, columnspan=3, sticky="w", pady=(12, 0))

        hardware = self._card(grid, 1, 0, "Compute detected")
        self.hardware_var = tk.StringVar(value="Detecting GPUs and encoders…")
        ttk.Label(hardware, textvariable=self.hardware_var, style="Card.TLabel", justify="left", wraplength=350).grid(column=0, row=1, columnspan=4, sticky="nw")
        ttk.Button(hardware, text="Scan again", command=lambda: self.refresh(force=True)).grid(column=0, row=2, sticky="w", pady=(12, 0))

        settings = self._card(grid, 0, 1, "Worker settings", columnspan=2)
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Name", style="Card.TLabel").grid(column=0, row=1, sticky="w", padx=(0, 10), pady=4)
        self.name_var = tk.StringVar(value=str(self.config["worker_name"]))
        ttk.Entry(settings, textvariable=self.name_var).grid(column=1, row=1, sticky="ew", pady=4)
        ttk.Label(settings, text="Working folder", style="Card.TLabel").grid(column=0, row=2, sticky="w", padx=(0, 10), pady=4)
        self.work_var = tk.StringVar(value=str(self.config["work_dir"]))
        ttk.Entry(settings, textvariable=self.work_var).grid(column=1, row=2, sticky="ew", pady=4)
        ttk.Button(settings, text="Browse", command=self.choose_work_dir).grid(column=2, row=2, padx=(8, 0))
        ttk.Button(settings, text="Open", command=self.open_work_dir).grid(column=3, row=2, padx=(8, 0))
        ttk.Label(settings, text="Preferred compute", style="Card.TLabel").grid(column=0, row=3, sticky="w", padx=(0, 10), pady=4)
        self.family_var = tk.StringVar(value=FAMILY_LABELS[str(self.config["preferred_encoder_family"])])
        self.family_combo = ttk.Combobox(settings, textvariable=self.family_var, state="readonly", values=list(FAMILY_LABELS.values()))
        self.family_combo.grid(column=1, row=3, sticky="ew", pady=4)
        ttk.Label(settings, text="Parallel GPU jobs", style="Card.TLabel").grid(column=2, row=3, sticky="e", padx=(16, 8))
        self.slots_var = tk.StringVar(value=str(self.config["hardware_slots"]))
        ttk.Combobox(settings, textvariable=self.slots_var, state="readonly", width=4, values=[str(value) for value in range(1, 9)]).grid(column=3, row=3, sticky="e")
        self.login_var = tk.BooleanVar(value=bool(self.config["run_at_login"]))
        ttk.Checkbutton(settings, text="Start with Windows", variable=self.login_var).grid(column=1, row=4, sticky="w", pady=(8, 0))
        ttk.Button(settings, text="Save settings", style="Accent.TButton", command=self.save_settings).grid(column=3, row=4, sticky="e", pady=(8, 0))
        self.settings_note = tk.StringVar(value="Smart jobs use this preference; explicitly locked presets stay unchanged.")
        ttk.Label(settings, textvariable=self.settings_note, style="Card.TLabel", foreground="#91a0aa").grid(column=0, row=5, columnspan=4, sticky="w", pady=(8, 0))

        activity = self._card(grid, 0, 2, "Activity", columnspan=2)
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(3, weight=1)
        self.queue_var = tk.StringVar(value="No jobs running")
        ttk.Label(activity, textvariable=self.queue_var, style="Card.TLabel").grid(column=0, row=1, sticky="w")
        self.progress = ttk.Progressbar(activity, maximum=100)
        self.progress.grid(column=0, row=2, sticky="ew", pady=(8, 10))
        self.log_widget = tk.Text(activity, height=6, bg="#0b1118", fg="#aebbc3", insertbackground="#fff", relief="flat", font=("Consolas", 9), padx=9, pady=7, state="disabled")
        self.log_widget.grid(column=0, row=3, sticky="nsew")
        controls = ttk.Frame(activity, style="Card.TFrame")
        controls.grid(column=0, row=4, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text="Open controller", command=self.open_controller).pack(side="left")
        ttk.Button(controls, text="Hide to tray", command=self.hide).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Exit worker", command=self.exit).pack(side="right")

    def append_log(self, line: str) -> None:
        self.log_lines = (self.log_lines + [line])[-100:]
        if hasattr(self, "root"):
            self.root.after(0, self._render_log)

    def _render_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.insert("end", "\n".join(self.log_lines[-20:]))
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _show_pairing(self) -> None:
        self.pairing_var.set(str(self.pairing.get("code") or "Unavailable"))
        expires = float(self.pairing.get("expires_at") or 0)
        self.pair_expiry.set("Valid until " + time.strftime("%I:%M %p", time.localtime(expires)) if expires else "")

    def new_pairing(self) -> None:
        self.pairing = self.runtime.new_pairing_code()
        self._show_pairing()

    def copy_pairing(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(str(self.pairing.get("code") or ""))
        self.settings_note.set("Pairing code copied to the clipboard.")

    def choose_work_dir(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(initialdir=self.work_var.get(), title="Choose ByteSqueeze working folder")
        if selected:
            self.work_var.set(selected)
            self.settings_note.set("Save and restart the worker to use the new working folder.")

    def open_work_dir(self) -> None:
        path = Path(self.work_var.get())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def save_settings(self) -> None:
        reverse = {label: key for key, label in FAMILY_LABELS.items()}
        old_work = str(self.config["work_dir"])
        self.config.update({
            "worker_name": self.name_var.get().strip() or default_config()["worker_name"],
            "work_dir": self.work_var.get().strip() or default_config()["work_dir"],
            "preferred_encoder_family": reverse.get(self.family_var.get(), "auto"),
            "hardware_slots": int(self.slots_var.get()),
            "run_at_login": bool(self.login_var.get()),
        })
        Path(str(self.config["work_dir"])).expanduser().mkdir(parents=True, exist_ok=True)
        save_config(self.config)
        try:
            set_run_at_login(bool(self.config["run_at_login"]))
        except OSError as exc:
            self.append_log(f"Could not update Windows startup: {exc}")
        self.runtime.apply_live_settings(self.config)
        if old_work != str(self.config["work_dir"]):
            self.settings_note.set("Saved. Restart ByteSqueeze Worker to change the working folder.")
        else:
            self.settings_note.set("Settings saved and advertised to the controller.")
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> None:
        snapshot = self.runtime.snapshot(refresh_hardware=force)
        if not snapshot.get("ok"):
            self.status_label.configure(text="● ERROR", foreground="#ff7b7b")
            self.queue_var.set(snapshot.get("error") or "Worker failed to start")
            return
        controllers = snapshot.get("controllers") or []
        self.status_label.configure(text="● ONLINE", foreground="#55d9e8")
        self.controller_var.set(
            "Paired with " + ", ".join(str(row.get("name") or "Controller") for row in controllers)
            if controllers else "Not paired yet · enter this worker URL and code on the controller"
        )
        hardware = snapshot.get("hardware") or {}
        gpu_lines = []
        for gpu in hardware.get("gpus") or []:
            gpu_lines.append(f"{gpu.get('name')}  ·  {gpu.get('driver_version') or 'driver detected'}")
        if not gpu_lines:
            vendors = hardware.get("gpu_vendors") or []
            gpu_lines.append("GPU: " + (", ".join(vendors) if vendors else "No supported GPU found"))
        families = [FAMILY_LABELS.get(value, value) for value in hardware.get("encoder_families") or []]
        selectable = [FAMILY_LABELS["auto"]]
        selectable.extend(
            FAMILY_LABELS[value]
            for value in hardware.get("encoder_families") or []
            if value in FAMILY_LABELS and FAMILY_LABELS[value] not in selectable
        )
        self.family_combo.configure(values=selectable)
        cpu = hardware.get("cpu") or {}
        gpu_lines.append("Encoders: " + (", ".join(families) if families else "CPU / software"))
        gpu_lines.append(f"CPU: {cpu.get('name') or 'Detected'} · {cpu.get('logical_cores') or 1} threads")
        gpu_lines.append(f"Work space free: {_format_bytes(int(snapshot.get('free_bytes') or 0))}")
        self.hardware_var.set("\n".join(gpu_lines))

        summary = snapshot.get("summary") or {}
        running = [row for row in snapshot.get("jobs") or [] if row.get("status") == "running"]
        if running:
            current = running[0]
            title = os.path.basename(str(current.get("src") or "Encoding"))
            progress = float(current.get("progress") or 0)
            self.queue_var.set(f"{title}  ·  {progress:.1f}%  ·  {len(running)} running")
            self.progress["value"] = progress
        else:
            queued = int(summary.get("queued") or 0)
            self.queue_var.set(f"Ready · {queued} queued · {int(summary.get('done') or 0)} completed")
            self.progress["value"] = 0

    def _poll(self) -> None:
        self.refresh()
        self.root.after(2500, self._poll)

    def open_controller(self) -> None:
        snapshot = self.runtime.snapshot()
        controllers = snapshot.get("controllers") or []
        url = str((controllers[0] if controllers else {}).get("url") or "").strip()
        webbrowser.open(url or local_addresses(int(self.config["port"]))[0])

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (64, 64), "#0b1118")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((6, 6, 58, 58), radius=13, fill="#46cede")
            draw.text((20, 13), "B", fill="#071014", stroke_width=1, font=None)
            menu = pystray.Menu(
                pystray.MenuItem("Open ByteSqueeze Worker", lambda: self.root.after(0, self.show), default=True),
                pystray.MenuItem("New pairing code", lambda: self.root.after(0, self.new_pairing)),
                pystray.MenuItem("Open controller", lambda: self.root.after(0, self.open_controller)),
                pystray.MenuItem("Exit", lambda: self.root.after(0, self.exit)),
            )
            self.tray = pystray.Icon("ByteSqueezeWorker", image, APP_NAME, menu)
            self.tray.run_detached()
        except Exception as exc:
            self.append_log(f"System tray unavailable; window will minimize normally ({exc})")

    def hide(self) -> None:
        if self.tray is not None:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def exit(self) -> None:
        snapshot = self.runtime.snapshot()
        running = int((snapshot.get("summary") or {}).get("running") or 0)
        if running:
            from tkinter import messagebox

            if not messagebox.askyesno(
                "Exit ByteSqueeze Worker?",
                f"{running} job(s) are still running. Exiting will interrupt them. Continue?",
            ):
                return
        self.runtime.stop()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self, *, minimized: bool = False) -> None:
        if minimized or bool(self.config.get("start_minimized")):
            self.root.after(100, self.hide)
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ByteSqueeze Windows Worker")
    parser.add_argument("--encode-one", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", metavar="RESULT_JSON", help=argparse.SUPPRESS)
    parser.add_argument("--minimized", action="store_true", help="start in the notification area")
    args = parser.parse_args(argv)
    if args.encode_one:
        configure_runtime_tools()
        from worker.encode_runner import run

        return run()
    config = load_config()
    configure_environment(config)
    if args.self_test:
        result = {"ok": False}
        try:
            from worker.app import create_worker_app

            app = create_worker_app(announce_pairing=False)
            app.config.update(TESTING=True)
            client = app.test_client()
            health = client.get("/api/health").get_json() or {}
            discovery = client.get("/api/node/discovery").get_json() or {}
            hardware = discovery.get("hardware") or {}
            result = {
                "ok": bool(health.get("ok") and discovery.get("ok")),
                "release": health.get("release"),
                "worker_mode": discovery.get("worker_mode"),
                "work_path": (health.get("work") or {}).get("path"),
                "platform": hardware.get("platform"),
                "gpus": hardware.get("gpus") or [],
                "encoder_families": hardware.get("encoder_families") or [],
                "encoders": hardware.get("encoders") or [],
                "handbrake_path": hardware.get("handbrake_path"),
                "ffmpeg_path": hardware.get("ffmpeg_path"),
                "ffprobe_path": hardware.get("ffprobe_path"),
            }
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        Path(args.self_test).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0 if result.get("ok") else 1
    WorkerWindow(config).run(minimized=args.minimized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
