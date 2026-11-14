# PyInstaller spec: one folder with two executables sharing the runtime.
#   PerfMonitor.exe  windowed: the UI, and the collector (`PerfMonitor.exe -m perfmon.collector`, see launcher.py)
#   pm.exe           console: the CLI
# Build: pyinstaller packaging/perfmon.spec --noconfirm   (from the repository root)
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
           (str(ROOT / "perfmon" / "etw"), "perfmon/etw")],
    excludes=["tkinter", "unittest", "pydoc_data"],
    noarchive=False,
)
pyz = PYZ(a.pure)


def exe(name, console):
    return EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=console, icon=ICON,
               version=str(ROOT / "build" / "version.txt"), upx=False)


coll = COLLECT(exe("PerfMonitor", False), exe("pm", True), a.binaries, a.datas, name="PerfMonitor", upx=False)
