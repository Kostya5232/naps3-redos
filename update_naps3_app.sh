#!/usr/bin/env bash
# Обновляет только Python-приложение NAPS3 до версии из этого архива.
# Поддерживает RPM-установку и старую ручную установку.
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустите через sudo: sudo $0" >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/naps3.py"
[[ -s "$SOURCE" ]] || { echo "Не найден $SOURCE" >&2; exit 1; }
BACKEND_SOURCE="$SCRIPT_DIR/windows_backend.py"
[[ -s "$BACKEND_SOURCE" ]] || { echo "Не найден $BACKEND_SOURCE" >&2; exit 1; }
UDEV_RULE_SOURCE="$SCRIPT_DIR/packaging/99-naps3-canon-mf4410.rules"
[[ -s "$UDEV_RULE_SOURCE" ]] || { echo "Не найден $UDEV_RULE_SOURCE" >&2; exit 1; }
PERMISSION_HELPER="$SCRIPT_DIR/packaging/naps3-usb-permissions"
[[ -x "$PERMISSION_HELPER" ]] || { echo "Не найден $PERMISSION_HELPER" >&2; exit 1; }
LAUNCHER_SOURCE="$SCRIPT_DIR/packaging/naps3-launcher"
[[ -x "$LAUNCHER_SOURCE" ]] || { echo "Не найден $LAUNCHER_SOURCE" >&2; exit 1; }
/usr/bin/bash -n "$LAUNCHER_SOURCE"

VERSION="$(sed -nE 's/^APP_VERSION = "([^"]+)"/\1/p' "$SOURCE" | head -n 1)"
[[ -n "$VERSION" ]] || { echo "Не удалось определить версию." >&2; exit 1; }

pixma_backend_installed() {
    compgen -G '/usr/lib64/sane/libsane-pixma.so*' >/dev/null ||
        compgen -G '/usr/lib/sane/libsane-pixma.so*' >/dev/null ||
        compgen -G '/usr/lib/x86_64-linux-gnu/sane/libsane-pixma.so*' >/dev/null
}

if ! pixma_backend_installed; then
    command -v dnf >/dev/null 2>&1 || {
        echo "Не найден backend pixma и команда dnf." >&2
        exit 1
    }
    echo "Установка SANE-драйвера Canon pixma…"
    dnf install -y sane-backends-drivers-scanners
fi

pixma_backend_installed || {
    echo "После установки не найден backend SANE pixma." >&2
    exit 1
}

TARGET=""
if [[ -f /usr/libexec/naps3/naps3.py ]]; then
    TARGET=/usr/libexec/naps3/naps3.py
elif [[ -f /opt/naps3/naps3.py ]]; then
    TARGET=/opt/naps3/naps3.py
else
    echo "Установленный NAPS3 не найден." >&2
    echo "Для новой ручной установки используйте install_redos8.sh." >&2
    exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${TARGET}.backup-${STAMP}"
BACKEND_TARGET="$(dirname "$TARGET")/windows_backend.py"
BACKEND_BACKUP="${BACKEND_TARGET}.backup-${STAMP}"
if [[ "$TARGET" == /usr/libexec/naps3/naps3.py ]]; then
    LAUNCHER_TARGET=/usr/bin/naps3
else
    LAUNCHER_TARGET=/usr/local/bin/naps3
fi
LAUNCHER_BACKUP="${LAUNCHER_TARGET}.backup-${STAMP}"
LAUNCHER_EXISTED=0
cp -a "$TARGET" "$BACKUP"
if [[ -f "$BACKEND_TARGET" ]]; then
    cp -a "$BACKEND_TARGET" "$BACKEND_BACKUP"
fi
if [[ -f "$LAUNCHER_TARGET" ]]; then
    LAUNCHER_EXISTED=1
    cp -a "$LAUNCHER_TARGET" "$LAUNCHER_BACKUP"
fi

pkill -f "$TARGET" 2>/dev/null || true
sleep 1

install -m 0755 "$SOURCE" "${TARGET}.new"
install -m 0644 "$BACKEND_SOURCE" "${BACKEND_TARGET}.new"
install -m 0755 "$LAUNCHER_SOURCE" "${LAUNCHER_TARGET}.new"
mv -f "${TARGET}.new" "$TARGET"
mv -f "${BACKEND_TARGET}.new" "$BACKEND_TARGET"
mv -f "${LAUNCHER_TARGET}.new" "$LAUNCHER_TARGET"
rm -rf "$(dirname "$TARGET")/__pycache__"
if ! /usr/bin/python3 -m py_compile "$TARGET" "$BACKEND_TARGET"; then
    cp -a "$BACKUP" "$TARGET"
    if [[ -f "$BACKEND_BACKUP" ]]; then
        cp -a "$BACKEND_BACKUP" "$BACKEND_TARGET"
    fi
    if [[ -f "$LAUNCHER_BACKUP" ]]; then
        cp -a "$LAUNCHER_BACKUP" "$LAUNCHER_TARGET"
    elif (( LAUNCHER_EXISTED == 0 )); then
        rm -f "$LAUNCHER_TARGET"
    fi
    exit 1
fi

install -d -m 0755 /etc/udev/rules.d
rm -f /etc/udev/rules.d/60-naps3-scanners.rules
install -m 0644 "$UDEV_RULE_SOURCE" /etc/udev/rules.d/99-naps3-canon-mf4410.rules
install -m 0755 "$PERMISSION_HELPER" "$(dirname "$TARGET")/naps3-usb-permissions"
"$PERMISSION_HELPER"

INSTALLED_VERSION="$(sed -nE 's/^APP_VERSION = "([^"]+)"/\1/p' "$TARGET" | head -n 1)"
[[ "$INSTALLED_VERSION" == "$VERSION" ]] || {
    echo "Проверка версии не пройдена: $INSTALLED_VERSION" >&2
    cp -a "$BACKUP" "$TARGET"
    if [[ -f "$BACKEND_BACKUP" ]]; then
        cp -a "$BACKEND_BACKUP" "$BACKEND_TARGET"
    fi
    if [[ -f "$LAUNCHER_BACKUP" ]]; then
        cp -a "$LAUNCHER_BACKUP" "$LAUNCHER_TARGET"
    elif (( LAUNCHER_EXISTED == 0 )); then
        rm -f "$LAUNCHER_TARGET"
    fi
    exit 1
}

echo
echo "NAPS3 обновлён до версии $INSTALLED_VERSION."
echo "Резервная копия: $BACKUP"
echo "Запустите приложение командой: naps3"
if command -v rpm >/dev/null 2>&1 && rpm -q naps3 >/dev/null 2>&1; then
    echo "Примечание: RPM-база сохранит номер старого пакета до установки нового RPM."
fi
