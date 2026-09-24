"""Build the Windows programs: dist/CanExpert (CanExpert.exe, DummyECU.exe, TestExpert.exe) and the zip of them.

    python -m pip install -r requirements-build.txt
    python tools/build_windows.py

Runs PyInstaller with CanExpert.spec, checks that each program starts (--smoke-test, without showing a
window), and zips the folder. Nothing is installed or signed.
"""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def smoke_test(program: Path):
    """The program builds its main window and exits 0; offscreen, so nothing appears."""
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run([str(program), "--smoke-test"], env=environment, timeout=120, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(f"{program.name} did not start (exit code {result.returncode}):\n"
                         f"{result.stdout.decode(errors='replace')}{result.stderr.decode(errors='replace')}")
    print(f"{program.name}: starts")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from canexpert import __version__
    try:
        import PyInstaller.__main__
    except ImportError:
        raise SystemExit("PyInstaller is missing: python -m pip install -r requirements-build.txt") from None
    PyInstaller.__main__.run([str(ROOT / "CanExpert.spec"), "--noconfirm", "--clean",
                              "--distpath", str(DIST), "--workpath", str(ROOT / "build")])
    folder = DIST / "CanExpert"
    for name in ("CanExpert.exe", "DummyECU.exe", "TestExpert.exe"):
        smoke_test(folder / name)
    archive = DIST / f"CanExpert-{__version__}-windows.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(folder.rglob("*")):
            bundle.write(path, Path("CanExpert") / path.relative_to(folder))
    print(f"{archive} ({archive.stat().st_size // (1024 * 1024)} MB)")
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
