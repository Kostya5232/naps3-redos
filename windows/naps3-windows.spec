from pathlib import Path
import os
import sys


root = Path(SPECPATH).parent
pdf_tool = Path(os.environ["PDFTOPPM_EXE"])
wia_tool = Path(os.environ["WIA_BRIDGE_EXE"])
prefix = Path(sys.prefix)

datas = [
    (str(wia_tool), "windows"),
    (str(root / "naps3.png"), "."),
    (str(root / "naps3.svg"), "."),
]
for source, destination in (
    (prefix / "etc" / "fonts", "etc/fonts"),
    (prefix / "share" / "poppler", "share/poppler"),
):
    if source.is_dir():
        datas.append((str(source), destination))

app_icon = root / "windows" / "naps3.ico"

a = Analysis(
    [str(root / "naps3.py")],
    pathex=[str(root)],
    binaries=[(str(pdf_tool), ".")],
    datas=datas,
    hiddenimports=[
        "gi.repository.Gdk",
        "gi.repository.GdkPixbuf",
        "gi.repository.Gio",
        "gi.repository.GLib",
        "gi.repository.Gtk",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "windows" / "startup_hook.py")],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NAPS3",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(app_icon) if app_icon.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NAPS3",
)
