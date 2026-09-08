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

VERSION="$(sed -nE 's/^APP_VERSION = "([^"]+)"/\1/p' "$SOURCE" | head -n 1)"
[[ -n "$VERSION" ]] || { echo "Не удалось определить версию." >&2; exit 1; }

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
cp -a "$TARGET" "$BACKUP"
if [[ -f "$BACKEND_TARGET" ]]; then
    cp -a "$BACKEND_TARGET" "$BACKEND_BACKUP"
fi

pkill -f "$TARGET" 2>/dev/null || true
sleep 1

install -m 0755 "$SOURCE" "${TARGET}.new"
install -m 0644 "$BACKEND_SOURCE" "${BACKEND_TARGET}.new"
mv -f "${TARGET}.new" "$TARGET"
mv -f "${BACKEND_TARGET}.new" "$BACKEND_TARGET"
rm -rf "$(dirname "$TARGET")/__pycache__"
if ! /usr/bin/python3 -m py_compile "$TARGET" "$BACKEND_TARGET"; then
    cp -a "$BACKUP" "$TARGET"
    if [[ -f "$BACKEND_BACKUP" ]]; then
        cp -a "$BACKEND_BACKUP" "$BACKEND_TARGET"
    fi
    exit 1
fi

INSTALLED_VERSION="$(sed -nE 's/^APP_VERSION = "([^"]+)"/\1/p' "$TARGET" | head -n 1)"
[[ "$INSTALLED_VERSION" == "$VERSION" ]] || {
    echo "Проверка версии не пройдена: $INSTALLED_VERSION" >&2
    cp -a "$BACKUP" "$TARGET"
    if [[ -f "$BACKEND_BACKUP" ]]; then
        cp -a "$BACKEND_BACKUP" "$BACKEND_TARGET"
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
