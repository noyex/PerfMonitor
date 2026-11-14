"""Writes build/version.txt, the Windows version resource of the executables, from perfmon.__version__."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from perfmon import __version__  # noqa: E402

nums = (tuple(int(x) for x in __version__.split(".")) + (0, 0, 0, 0))[:4]   # x.y.z -> (x, y, z, 0)
out = ROOT / "build" / "version.txt"
out.parent.mkdir(exist_ok=True)
out.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'noyex'),
    StringStruct('FileDescription', 'PerfMonitor'),
    StringStruct('FileVersion', '{__version__}'),
    StringStruct('ProductName', 'PerfMonitor'),
    StringStruct('ProductVersion', '{__version__}'),
    StringStruct('LegalCopyright', 'MIT License')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
""", encoding="utf-8")
print(__version__)
