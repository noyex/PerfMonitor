"""Entry point of the installed build's Python (PyInstaller). pm.exe is the CLI. nxframe-core.exe has no window and
runs a module the way `python -m` does: `nxframe-core.exe -m perfmon.<module> args` (the collector task, the API
server of the window, their self-spawns). The window itself is NXFrame.exe (src-tauri)."""
import os
import runpy
import sys
from pathlib import Path

# Variables that make .NET, PowerShell or Python load code from a place of the user's choosing. A user can set them
# for all their processes (HKCU\Environment) and the collector runs elevated, so they are dropped before anything loads
CODE_ENV = ("COR_", "CORECLR_", "COMPLUS_", "DOTNET_", "PYTHON", "PSMODULEPATH")


def main():
    for name in [n for n in os.environ if n.upper().startswith(CODE_ENV)]:
        del os.environ[name]
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
