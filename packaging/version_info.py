"""Writes build/version.txt, the Windows version resource of the Python executables, from perfmon.__version__ and
prints the version. Fails when the window (src-tauri) carries another version: one release, one number."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from perfmon import __version__  # noqa: E402

tauri = ROOT / "src-tauri"
others = {
    "src-tauri/tauri.conf.json": json.loads((tauri / "tauri.conf.json").read_text(encoding="utf-8"))["version"],
    "src-tauri/Cargo.toml": re.search(r'^version\s*=\s*"([^"]+)"', (tauri / "Cargo.toml").read_text(encoding="utf-8"),
                                      re.M).group(1),
}
for name, version in others.items():
    if version != __version__:
        sys.exit(f"{name} has version {version}, perfmon/__init__.py has {__version__}")

nums = (tuple(int(x) for x in __version__.split(".")) + (0, 0, 0, 0))[:4]   # x.y.z -> (x, y, z, 0)
out = ROOT / "build" / "version.txt"
out.parent.mkdir(exist_ok=True)
out.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'noyex'),
    StringStruct('FileDescription', 'NX Frame'),
    StringStruct('FileVersion', '{__version__}'),
    StringStruct('ProductName', 'NX Frame'),
    StringStruct('ProductVersion', '{__version__}'),
    StringStruct('LegalCopyright', 'MIT License')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
""", encoding="utf-8")
print(__version__)
