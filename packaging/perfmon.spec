# PyInstaller spec: the Python side of the installed build, one folder with two executables sharing the runtime.
#   nxframe-core.exe  no window: runs a module, e.g. `nxframe-core.exe -m perfmon.collector` (see launcher.py)
#   pm.exe            console: the CLI
# The window (NXFrame.exe, src-tauri) is built separately and put next to them by the installer.
# Build (from the repository root): python packaging/version_info.py   (writes build/version.txt)
#                                   pyinstaller packaging/perfmon.spec --noconfirm   ->  dist\NXFrame\
from pathlib import Path

ROOT = Path(SPECPATH).parent
PKG = ROOT / "perfmon"
# every module of the package by its file (collect_submodules would need the package importable from here)
MODULES = sorted(".".join(f.relative_to(ROOT).with_suffix("").parts) for f in PKG.rglob("*.py")
                 if "__pycache__" not in f.parts)
ICON = str(ROOT / "perfmon" / "ui" / "static" / "favicon.ico")

a = Analysis(
    [str(ROOT / "perfmon" / "launcher.py")],
    pathex=[str(ROOT)],
    # modules are started by name (runpy) and pywin32 imports some lazily
    hiddenimports=MODULES + ["win32timezone"],
    datas=[(str(ROOT / "perfmon" / "ui" / "static"), "perfmon/ui/static"),
           (str(ROOT / "perfmon" / "etw"), "perfmon/etw"),
           (str(ROOT / "perfmon" / "agent"), "perfmon/agent")],
    # webview: the pywebview window of a source checkout without NXFrame.exe, never used here
    excludes=["tkinter", "unittest", "pydoc_data", "webview"],
    noarchive=False,
)
pyz = PYZ(a.pure)


def exe(name, console):
    return EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=console, icon=ICON,
               version=str(ROOT / "build" / "version.txt"), upx=False)


coll = COLLECT(exe("nxframe-core", False), exe("pm", True), a.binaries, a.datas, name="NXFrame", upx=False)
