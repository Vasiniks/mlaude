#!/usr/bin/env python3
"""mlaude unified app: router child process + optional tray icon.

Runs router/router.py as a child (kept runnable standalone), shows the
tray when pystray+Pillow are present, otherwise runs headless — the web
dashboard stays the control surface either way. Never exits on its own;
the autostart supervisor (LaunchAgent KeepAlive / task restart) revives it.
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTER_PY = os.path.normpath(os.path.join(APP_DIR, "..", "router", "router.py"))
SYNC_PY = os.path.normpath(os.path.join(APP_DIR, "..", "router", "sync.py"))
ROUTER_DIR = os.path.dirname(APP_DIR)


def log_path():
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Logs",
                            "claude-router.log")
    return os.path.join(ROUTER_DIR, "router.log")


def wait_for_router(port, timeout=15):
    """True once anything answers HTTP (even Anthropic's root 404)."""
    url = "http://127.0.0.1:%d/" % port
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True  # an HTTP status is an answer
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


class Router:
    """Owns the router child process (lock-guarded for tray callbacks)."""

    def __init__(self, port):
        self.port = port
        self.lock = threading.Lock()
        self.proc = None

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            log = open(log_path(), "a", encoding="utf-8")
            env = dict(os.environ, ROUTER_PORT=str(self.port))
            self.proc = subprocess.Popen(
                [sys.executable, ROUTER_PY], env=env,
                stdout=log, stderr=subprocess.STDOUT,
                cwd=os.path.dirname(ROUTER_PY))

    def stop(self):
        with self.lock:
            proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def restart(self):
        self.stop()
        self.start()


def do_sync():
    """Run one model sync; returns its exit code."""
    try:
        return subprocess.run(
            [sys.executable, SYNC_PY], timeout=300).returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.stderr.write("sync failed: %s\n" % e)
        return 1


def open_dashboard(port):
    import webbrowser
    url = "http://127.0.0.1:%d/ui" % port
    try:
        return webbrowser.open(url)
    except Exception as e:  # noqa: BLE001 - opening is best-effort
        sys.stderr.write("could not open %s: %s\n" % (url, e))
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="mlaude unified app.")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("ROUTER_PORT", "8787")))
    ap.add_argument("--open", action="store_true",
                    help="open the dashboard once the router answers")
    ap.add_argument("--no-tray", action="store_true")
    args = ap.parse_args(argv)

    router = Router(args.port)
    router.start()
    if not wait_for_router(args.port):
        sys.stderr.write("router did not answer; see %s\n" % log_path())
        return 1
    if args.open:
        open_dashboard(args.port)

    stop_all = threading.Event()

    def quit_app():
        router.stop()
        stop_all.set()

    if not args.no_tray:
        try:
            import tray as tray_mod
        except ImportError:
            tray_mod = None
        if tray_mod is not None and tray_mod.available():
            sys.stderr.write("tray icon up; dashboard at /ui\n")
            tray_mod.run_tray({
                "open": lambda: open_dashboard(args.port),
                "sync": lambda: threading.Thread(
                    target=do_sync, daemon=True).start(),
                "restart": router.restart,
                "quit": quit_app,
            })
            return 0

    sys.stderr.write("headless; dashboard at http://127.0.0.1:%d/ui\n"
                     % args.port)

    def on_signal(_signum, _frame):
        quit_app()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    while not stop_all.wait(1):
        if router.proc is not None and router.proc.poll() is not None:
            sys.stderr.write("router exited; restarting\n")
            router.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
