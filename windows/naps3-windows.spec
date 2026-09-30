from pathlib import Path
import os
import sys


root = Path(SPECPATH).parent
pdf_tool_path = os.environ.get("PDFTOPPM_EXE")
pdfium_bundle = os.environ.get("NAPS3_BUNDLE_PDFIUM") == "1"
if pdfium_bundle == bool(pdf_tool_path):
    raise RuntimeError("Select exactly one PDF renderer: pdftoppm or PDFium")
wia_tool = Path(os.environ["WIA_BRIDGE_EXE"])
twain_tool = Path(os.environ["TWAIN_BRIDGE_EXE"])
twain_library = Path(os.environ["TWAIN_LIBRARY_DLL"])
twain_license = Path(os.environ["TWAIN_LICENSE_TXT"])
prefix = Path(sys.prefix)

datas = [
    (str(wia_tool), "windows"),
    (str(twain_tool), "windows"),
    (str(twain_library), "windows"),
    (str(twain_license), "windows"),
    (str(root / "naps3.png"), "."),
    (str(root / "naps3.svg"), "."),
]
binaries = []
if pdfium_bundle:
    import pypdfium2

    pdfium_dll = Path(pypdfium2.__file__).parent / "pdfium.dll"
    if not pdfium_dll.is_file():
        raise FileNotFoundError(pdfium_dll)
    binaries.append((str(pdfium_dll), "pypdfium2"))
else:
    binaries.append((str(Path(pdf_tool_path)), "."))
for source, destination in (
    (prefix / "etc" / "fonts", "etc/fonts"),
    (prefix / "share" / "poppler", "share/poppler"),
):
    if source.is_dir():
        datas.append((str(source), destination))

gtk_prefix_path = os.environ.get("NAPS3_GTK_PREFIX")
if gtk_prefix_path:
    gtk_prefix = Path(gtk_prefix_path)
    gtk_bin = gtk_prefix / "bin"
    gtk_typelibs = gtk_prefix / "lib" / "girepository-1.0"
    if not gtk_bin.is_dir() or not gtk_typelibs.is_dir():
        raise FileNotFoundError("GTK x86 runtime is incomplete")
    binaries.extend((str(dll), ".") for dll in gtk_bin.glob("*.dll"))
    datas.append((str(gtk_typelibs), "lib/girepository-1.0"))
    for source, destination in (
        (gtk_prefix / "etc" / "fonts", "etc/fonts"),
        (gtk_prefix / "share" / "glib-2.0" / "schemas", "share/glib-2.0/schemas"),
        (gtk_prefix / "share" / "icons" / "Adwaita", "share/icons/Adwaita"),
        (gtk_prefix / "lib" / "gdk-pixbuf-2.0", "lib/gdk-pixbuf-2.0"),
    ):
        if source.is_dir():
            datas.append((str(source), destination))

app_icon = root / "windows" / "naps3.ico"

a = Analysis(
    [str(root / "naps3.py")],
    pathex=[str(root)],
    binaries=binaries,
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
