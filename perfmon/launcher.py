"""Entry point of the installed build (PyInstaller): PerfMonitor.exe opens the UI, pm.exe is the CLI, and
`<exe> -m perfmon.<module> args` runs a module the way `python -m` does (the collector task, its self-spawns)."""
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
        from perfmon.ui import app
        app.main()


if __name__ == "__main__":
    main()
