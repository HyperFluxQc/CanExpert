# PyInstaller spec for Windows: CanExpert.exe, DummyECU.exe and TestExpert.exe in one folder, dist/CanExpert.
#
#     python tools/build_windows.py        (installs nothing; needs requirements-build.txt)
#
# Everything sits next to the executables (contents_directory "."), because a frozen CAN Expert looks for
# Configurations/, Databases/, DBC/, ODX/, examples/ and docs/ beside itself (canexpert/paths.py).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))
from canexpert import __version__  # noqa: E402

DATA = [(str(ROOT / folder), folder) for folder in ("Configurations", "Databases", "DBC", "ODX", "examples", "TestModules")
        if (ROOT / folder).exists()]
DATA += [(str(ROOT / "docs" / "USER_MANUAL.md"), "docs"), (str(ROOT / "canexpert" / "resources"), "canexpert/resources")]
DATA += collect_data_files("odxtools") + collect_data_files("cantools")
# The About box reads the libraries' versions from their metadata.
for _distribution in ("python-can", "cantools", "odxtools", "pyqtgraph", "PyQtAds"):
    DATA += copy_metadata(_distribution)
# python-can opens its interfaces by name at run time, so the analysis cannot see them.
HIDDEN = collect_submodules("can.interfaces")


def version_resource(description, name):
    """The Windows version resource: what Explorer shows under Properties > Details."""
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    numbers = tuple(int(part) for part in __version__.split(".")[:3]) + (0,)
    strings = [StringStruct("CompanyName", "CAN Expert"), StringStruct("FileDescription", description),
               StringStruct("FileVersion", __version__), StringStruct("InternalName", name),
               StringStruct("OriginalFilename", f"{name}.exe"), StringStruct("ProductName", "CAN Expert"),
               StringStruct("ProductVersion", __version__)]
    return VSVersionInfo(ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
                         kids=[StringFileInfo([StringTable("040904B0", strings)]),
                               VarFileInfo([VarStruct("Translation", [0x0409, 1200])])])


def program(script, name, icon, description):
    analysis = Analysis([str(ROOT / script)], pathex=[str(ROOT)], datas=DATA, hiddenimports=HIDDEN,
                        excludes=["tkinter", "PySide2", "PySide6", "PyQt6"], noarchive=False)
    executable = EXE(PYZ(analysis.pure), analysis.scripts, [], exclude_binaries=True, name=name,
                     icon=str(ROOT / "canexpert" / "resources" / icon), console=False,
                     version=version_resource(description, name) if sys.platform == "win32" else None,
                     contents_directory=".")
    return analysis, executable


main_analysis, main_exe = program("main.py", "CanExpert", "canexpert.ico", "CAN Expert - CAN and UDS tool")
ecu_analysis, ecu_exe = program("dummy_ecu.py", "DummyECU", "dummy_ecu.ico", "CAN Expert Dummy ECU")
test_analysis, test_exe = program("test_expert.py", "TestExpert", "test_expert.ico",
                                  "TestExpert - UDS conformance tests from a CDD, ODX or PDX file")

coll = COLLECT(main_exe, main_analysis.binaries, main_analysis.datas,
               ecu_exe, ecu_analysis.binaries, ecu_analysis.datas,
               test_exe, test_analysis.binaries, test_analysis.datas, name="CanExpert")
