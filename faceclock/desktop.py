"""
FaceClock desktop edition
=========================

Double-click launcher used by the Windows installer (FaceClock.exe):

* stores the database, logs and settings in the user's data folder
  (Windows: %APPDATA%\\FaceClock), never next to the program;
* uses the face models bundled with the program (no download);
* starts the web server on this computer only (127.0.0.1);
* opens FaceClock in its own window (Edge / Chrome "app" mode, no address bar);
* shows a small control window: punch terminal, back-office, employee portal,
  data folder, quit.

Command line (for support / CI):
    FaceClock.exe --selftest out.json   start, check the server and the face engine, exit
    FaceClock.exe --data-dir PATH       use another data folder
    FaceClock.exe --no-window           run the server only (no control window)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "FaceClock"
DEFAULT_PORT = 8765          # fixed port: the browser then remembers the camera permission


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def resource_dir() -> Path:
    """Folder holding bundled resources (PyInstaller) or the source checkout."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "faceclock"


# ---------------------------------------------------------------------------
# Persistent settings (secret key, port, kiosk token)
# ---------------------------------------------------------------------------
def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_state(path: Path, state: dict):
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def ensure_secret(state: dict) -> str:
    if not state.get("secret_key"):
        state["secret_key"] = secrets.token_hex(32)
    return state["secret_key"]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def health(port: int, timeout=2.0):
    """Return the /health JSON of a FaceClock server on this port, or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if "face_engine" in data else None
    except Exception:
        return None


def build_app(data_dir: Path, state_file: Path, secret_key: str):
    from faceclock import create_app
    from faceclock.config import Config

    return create_app(
        Config,
        DATABASE=str(data_dir / "faceclock.db"),
        MODELS_DIR=str(resource_dir() / "models"),
        SECRET_KEY=secret_key,
        SECRET_KEY_FROM_ENV=True,
        DESKTOP_MODE=True,
        DESKTOP_STATE_FILE=str(state_file),
    )


def start_server(app, port: int):
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="faceclock-server", daemon=True)
    thread.start()
    return server


def wait_until_up(port: int, seconds=20.0):
    end = time.time() + seconds
    while time.time() < end:
        if health(port, timeout=1.0):
            return True
        time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# Opening windows
# ---------------------------------------------------------------------------
def find_browser():
    """A Chromium browser able to open an app window (Edge ships with Windows)."""
    candidates = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if root:
                candidates += [Path(root) / "Microsoft/Edge/Application/msedge.exe",
                               Path(root) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                       Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for c in candidates:
        if c.exists():
            return str(c)
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_window(url: str):
    browser = find_browser()
    if browser:
        try:
            subprocess.Popen([browser, f"--app={url}", "--new-window"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            logging.exception("Could not start %s", browser)
    webbrowser.open(url)


def open_folder(path: Path):
    if sys.platform == "win32":
        os.startfile(str(path))                       # noqa: S606 - opens Explorer
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------------
# Control window (Tkinter, bundled with Python)
# ---------------------------------------------------------------------------
def control_window(base_url: str, data_dir: Path, on_quit):
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    icon = resource_dir() / "packaging" / "faceclock.ico"
    if sys.platform == "win32" and icon.exists():
        try:
            root.iconbitmap(str(icon))
        except tk.TclError:
            pass

    bg, ink, muted, brand = "#121a2b", "#ffffff", "#9aa6bf", "#2453d4"
    root.configure(bg=bg, padx=28, pady=22)

    title = tk.Frame(root, bg=bg)
    title.pack(anchor="w")
    tk.Label(title, text="Face", fg=ink, bg=bg, font=("Segoe UI", 22, "bold")).pack(side="left")
    tk.Label(title, text="Clock", fg="#7aa2ff", bg=bg, font=("Segoe UI", 22, "bold")).pack(side="left")
    tk.Label(root, text="Running on this computer", fg=muted, bg=bg,
             font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 16))

    def button(text, command, primary=False):
        b = tk.Button(root, text=text, command=command, width=34, relief="flat", cursor="hand2",
                      font=("Segoe UI", 11, "bold" if primary else "normal"),
                      bg=brand if primary else "#24314f", fg=ink,
                      activebackground="#3a64e0" if primary else "#2f3e63", activeforeground=ink,
                      padx=10, pady=9)
        b.pack(pady=4, fill="x")
        return b

    button("Punch terminal (camera)", lambda: open_window(f"{base_url}/desktop/kiosk"), primary=True)
    button("Back-office (administration)", lambda: open_window(f"{base_url}/desktop/home"))
    button("Employee portal", lambda: open_window(f"{base_url}/me/"))
    button("Open data folder", lambda: open_folder(data_dir))

    tk.Label(root, text="Keep this window open while FaceClock is in use.", fg=muted, bg=bg,
             font=("Segoe UI", 9)).pack(anchor="w", pady=(14, 4))

    def quit_app():
        if messagebox.askokcancel(APP_NAME, "Stop FaceClock on this computer?"):
            on_quit()
            root.destroy()

    button("Quit", quit_app)
    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.mainloop()


# ---------------------------------------------------------------------------
# Self-test (used by the build pipeline on the packaged program)
# ---------------------------------------------------------------------------
def selftest(out_file: str | None, require_models: bool) -> int:
    report = {"ok": False}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        try:
            app = build_app(tmp, tmp / "desktop.json", secrets.token_hex(16))
            port = free_port()
            server = start_server(app, port)
            up = wait_until_up(port)
            report["server_up"] = up
            report["health"] = health(port)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/setup", timeout=5) as r:
                report["setup_page"] = r.status
            # face engine smoke test on a blank frame: must answer "no face", not crash
            from faceclock.face import decode_image  # noqa: F401  (import check)
            eng = app.extensions["faceclock"]["engine"]
            report["engine"] = eng.name
            if eng.available:
                import numpy as np
                report["faces_on_blank_image"] = len(eng.detect(np.zeros((480, 640, 3), np.uint8)))
            server.shutdown()
            report["ok"] = bool(up and report["health"] and report["setup_page"] == 200
                                and (eng.available or not require_models))
        except Exception as exc:  # report everything, the CI log shows it
            import traceback
            report["error"] = f"{exc!r}"
            report["traceback"] = traceback.format_exc()
    text = json.dumps(report, indent=2, default=str)
    if out_file:
        Path(out_file).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--selftest", nargs="?", const="", default=None, metavar="OUT_JSON")
    parser.add_argument("--allow-missing-models", action="store_true")
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest is not None:
        _redirect_output(Path(tempfile.gettempdir()) / "faceclock-selftest.log")
        sys.exit(selftest(args.selftest or None, require_models=not args.allow_missing_models))

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _redirect_output(data_dir / "faceclock.log")
    logging.info("Starting %s, data in %s", APP_NAME, data_dir)

    state_file = data_dir / "desktop.json"
    state = load_state(state_file)
    secret = ensure_secret(state)
    port = int(state.get("port", DEFAULT_PORT))

    # Already running? Just bring the back-office up again.
    if health(port):
        open_window(f"http://127.0.0.1:{port}/desktop/home")
        return
    if not port_is_free(port):                       # port taken by another program
        port = free_port()
    state["port"] = port
    save_state(state_file, state)

    app = build_app(data_dir, state_file, secret)
    server = start_server(app, port)
    base_url = f"http://127.0.0.1:{port}"
    if not wait_until_up(port):
        logging.error("Server did not start")
    open_window(f"{base_url}/desktop/home")           # first start shows the setup wizard

    if args.no_window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return
    control_window(base_url, data_dir, on_quit=server.shutdown)


def _redirect_output(log_path: Path):
    """A windowed program has no console: send logs and stray prints to a file."""
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None or getattr(sys, "frozen", False):
        sys.stdout = stream
    if sys.stderr is None or getattr(sys, "frozen", False):
        sys.stderr = stream
    logging.basicConfig(level=logging.INFO, stream=stream,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


if __name__ == "__main__":
    main()
