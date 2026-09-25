import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


project_root = Path(SPECPATH).resolve().parent
backend_dir = project_root / "backend"
conda_bin = Path(sys.prefix) / "Library" / "bin"
sys.path.insert(0, str(backend_dir))

hiddenimports = ["webview.platforms.edgechromium"]
datas = [
    (str(project_root / "frontend" / "dist"), "frontend_dist"),
    (str(project_root / "desktop" / "assets" / "PaperReader.ico"), "."),
    (str(backend_dir / "app" / "assets" / "fonts"), "app/assets/fonts"),
    # The PDF translation worker is started as `python -m
    # workers.pdfmathtranslate` with the bundle root as its working directory,
    # so the package has to keep its own name at the top level.
    (str(project_root / "workers"), "workers"),
]
datas += collect_data_files("pypdfium2")
datas += collect_data_files("reportlab")
# Conda Pythons keep the OpenSSL DLLs that _ssl/_hashlib link against in
# Library\bin; PyInstaller does not collect them on its own. Globbed so both
# OpenSSL 1.1 (libssl-1_1-x64.dll) and 3 (libssl-3-x64.dll) layouts work.
conda_dll_names = {"libexpat.dll", "liblzma.dll", "libbz2.dll", "ffi.dll", "sqlite3.dll"}
conda_dll_names |= {p.name for p in conda_bin.glob("libssl-*.dll")}
conda_dll_names |= {p.name for p in conda_bin.glob("libcrypto-*.dll")}
binaries = [
    (str(conda_bin / name), ".")
    for name in sorted(conda_dll_names)
    if (conda_bin / name).is_file()
]

a = Analysis(
    [str(project_root / "desktop" / "launcher.py")],
    pathex=[str(backend_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # requests only needs chardet OR charset_normalizer; chardet >= 6 ships
        # mypyc-compiled extensions that fail to import from a frozen app
        # (SystemError: module filename missing), so keep it out of the bundle.
        "chardet",
        "pytest",
        "IPython",
        "ipykernel",
        "jupyter_client",
        "jupyter_core",
        "jedi",
        "parso",
        "tkinter",
        "_tkinter",
        "zmq",
        "numpy",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PaperReader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / "desktop" / "assets" / "PaperReader.ico"),
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PaperReader",
)
