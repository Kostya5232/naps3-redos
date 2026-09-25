#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python}"
PDFTOPPM_EXE="$(command -v pdftoppm.exe || command -v pdftoppm || true)"

if [[ -z "$PDFTOPPM_EXE" ]]; then
    echo "Не найден pdftoppm из пакета poppler." >&2
    exit 1
fi

export PDFTOPPM_EXE

"$PYTHON" windows/build_wia_bridge.py
"$PYTHON" windows/build_twain_bridge.py
WIA_BRIDGE_FILE="$ROOT/build/windows-wia/NAPS3.WiaBridge.exe"
TWAIN_BRIDGE_FILE="$ROOT/build/windows-twain/NAPS3.TwainBridge.exe"
test -s "$WIA_BRIDGE_FILE"
test -s "$TWAIN_BRIDGE_FILE"
export WIA_BRIDGE_EXE="$(cygpath -w "$WIA_BRIDGE_FILE")"
export TWAIN_BRIDGE_EXE="$(cygpath -w "$TWAIN_BRIDGE_FILE")"
export TWAIN_LIBRARY_DLL="$(cygpath -w "$ROOT/build/windows-twain/NTwain.dll")"
export TWAIN_LICENSE_TXT="$(cygpath -w "$ROOT/build/windows-twain/NTwain.LICENSE.txt")"

"$PYTHON" - <<'PY'
from pathlib import Path
from PIL import Image

root = Path.cwd()
with Image.open(root / "naps3.png") as source:
    icon = source.convert("RGBA")
    icon.save(
        root / "windows" / "naps3.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
PY

rm -rf -- "$ROOT/build/windows" "$ROOT/dist/windows/NAPS3"
mkdir -p "$ROOT/build/windows" "$ROOT/dist/windows"

"$PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$ROOT/dist/windows" \
    --workpath "$ROOT/build/windows" \
    "$ROOT/windows/naps3-windows.spec"

test -s "$ROOT/dist/windows/NAPS3/NAPS3.exe"
"$PYTHON" windows/pin_cairo.py "$ROOT/dist/windows/NAPS3"
echo "Готово: $ROOT/dist/windows/NAPS3/NAPS3.exe"
