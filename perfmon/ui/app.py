"""Desktop window for the UI (pywebview / Edge WebView2). Run: pythonw -m perfmon.ui.app"""
import socket
import sys
import webbrowser
from pathlib import Path

from .. import config
from .server import serve


def port_in_use(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    port = config.load()["ui_port"]
    if not port_in_use(port):
        serve(port)
    url = f"http://127.0.0.1:{port}/"
    if "--browser" in sys.argv:
        webbrowser.open(url)
        input() if sys.stdin and sys.stdin.isatty() else __import__("time").sleep(10**9)
        return
    try:
        import webview
        webview.create_window("PerfMonitor", url, width=1440, height=920, min_size=(1000, 700),
                              background_color="#000000")
        webview.start(icon=str(Path(__file__).parent / "static" / "favicon.ico"))
    except Exception:
        webbrowser.open(url)


if __name__ == "__main__":
    main()
