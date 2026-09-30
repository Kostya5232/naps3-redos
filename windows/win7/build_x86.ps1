param(
    [string]$GtkPrefix = "C:\gtk-build\gtk\Win32\release"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$gtkBin = Join-Path $GtkPrefix "bin"
$typelibs = Join-Path $GtkPrefix "lib\girepository-1.0"
if (-not (Test-Path -LiteralPath $gtkBin -PathType Container) -or
    -not (Test-Path -LiteralPath $typelibs -PathType Container)) {
    throw "GTK x86 или его типы GI не найдены: $GtkPrefix"
}

$env:Path = "$gtkBin;$env:Path"
$env:GI_TYPELIB_PATH = $typelibs
$env:NAPS3_GTK_PREFIX = $GtkPrefix
$env:NAPS3_BUNDLE_PDFIUM = "1"
Remove-Item Env:PDFTOPPM_EXE -ErrorAction SilentlyContinue

python -c "import struct,sys; assert sys.version_info[:2] == (3,8) and struct.calcsize('P') == 4"
if ($LASTEXITCODE -ne 0) { throw "Нужен Python 3.8 x86" }

python -m pip install 'Pillow==9.5.0' 'pypdfium2==3.8.0' 'pyinstaller==5.13.2' 'pyinstaller-hooks-contrib==2023.10'
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости x86" }

python (Join-Path $root "windows\build_wia_bridge.py")
if ($LASTEXITCODE -ne 0) { throw "Не удалось собрать WIA-мост" }
python (Join-Path $root "windows\build_twain_bridge.py")
if ($LASTEXITCODE -ne 0) { throw "Не удалось собрать TWAIN-мост" }

$wiaBridge = Join-Path $root "build\windows-wia\NAPS3.WiaBridge.exe"
$twainBridge = Join-Path $root "build\windows-twain\NAPS3.TwainBridge.exe"
& $wiaBridge -Action selftest
if ($LASTEXITCODE -ne 0) { throw "Самопроверка WIA-моста завершилась ошибкой" }

$env:WIA_BRIDGE_EXE = $wiaBridge
$env:TWAIN_BRIDGE_EXE = $twainBridge
$env:TWAIN_LIBRARY_DLL = Join-Path $root "build\windows-twain\NTwain.dll"
$env:TWAIN_LICENSE_TXT = Join-Path $root "build\windows-twain\NTwain.LICENSE.txt"

@'
from pathlib import Path
from PIL import Image
root = Path.cwd()
with Image.open(root / "naps3.png") as source:
    source.convert("RGBA").save(
        root / "windows" / "naps3.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
'@ | python -
if ($LASTEXITCODE -ne 0) { throw "Не удалось создать значок" }

python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $root "dist\windows7-x86") `
    --workpath (Join-Path $root "build\windows7-x86-pyinstaller") `
    (Join-Path $root "windows\naps3-windows.spec")
if ($LASTEXITCODE -ne 0) { throw "Сборка PyInstaller завершилась ошибкой" }

$app = Join-Path $root "dist\windows7-x86\NAPS3\NAPS3.exe"
if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
    throw "Не создана программа: $app"
}
Write-Host "Готово: $app"
