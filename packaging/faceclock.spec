# -*- mode: python ; coding: utf-8 -*-
# =====================================================================
# PyInstaller build of the FaceClock desktop program (Windows)
#   pyinstaller packaging/faceclock.spec --noconfirm
# Run from the repository root, after: python scripts/download_models.py
# =====================================================================
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

# --- Files that must ship with the program -----------------------------
datas = [
    (str(ROOT / "faceclock" / "templates"), "faceclock/templates"),
    (str(ROOT / "faceclock" / "static"), "faceclock/static"),
    (str(ROOT / "faceclock" / "schema.sql"), "faceclock"),
    (str(ROOT / "packaging" / "faceclock.ico"), "packaging"),
]
models = sorted((ROOT / "models").glob("*.onnx"))
if len(models) < 2:
    raise SystemExit("Face models missing: run  python scripts/download_models.py  first")
datas += [(str(m), "models") for m in models]
datas += collect_data_files("tzdata")          # time zones (Windows has no system database)

hiddenimports = collect_submodules("faceclock") + ["tzdata"]

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FaceClock",
    icon=str(ROOT / "packaging" / "faceclock.ico"),
    console=False,                              # windowed program, no black console
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="FaceClock", upx=False)
