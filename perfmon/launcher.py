"""Entry point of the installed build's Python (PyInstaller). pm.exe is the CLI. nxframe-core.exe has no window and
runs a module the way `python -m` does: `nxframe-core.exe -m perfmon.<module> args` (the collector task, the API
server of the window, their self-spawns). The window itself is NXFrame.exe (src-tauri)."""
import runpy
import sys
from pathlib import Path


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "-m":
        sys.argv = [sys.executable, *args[2:]]
        runpy.run_module(args[1], run_name="__main__", alter_sys=True)
    elif Path(sys.executable).stem.lower() == "pm":
        from perfmon import cli
        cli.main()
    else:
        sys.exit(2)   # nothing to run


if __name__ == "__main__":
    main()
