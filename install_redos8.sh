#!/usr/bin/env bash
# Установка или обновление NAPS3 на РЕД ОС 8.
# Запуск: sudo ./install_redos8.sh --user имя_пользователя

set -Eeuo pipefail

TARGET_USER=""

while (($#)); do
    case "$1" in
        --user)
            [[ $# -ge 2 ]] || {
                echo "После --user нужно имя пользователя." >&2
                exit 2
            }
            TARGET_USER="$2"
            shift 2
            ;;
        -h|--help)
            echo "Использование: sudo $0 [--user ИМЯ]"
            exit 0
            ;;
        *)
            echo "Неизвестный параметр: $1" >&2
            exit 2
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Запустите установщик через sudo или от root." >&2
    exit 1
fi

if rpm -q naps3 >/dev/null 2>&1; then
    echo "Обнаружен RPM-пакет NAPS3. Ручной установщик нельзя" >&2
    echo "запускать поверх RPM: они управляют одними и теми же файлами." >&2
    echo "Для обновления установите новый RPM через dnf." >&2
    exit 1
fi

if [[ $(uname -m) != "x86_64" ]]; then
    echo "Исправленный ipp-usb этой сборки предназначен для x86_64." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for file in \
    naps3.py windows_backend.py naps3.svg naps3.png ipp-usb.conf \
    ipp-usb-naps3 ipp-usb-m428.conf ipp-usb-naps3.service.conf \
    packaging/naps3-launcher \
    packaging/99-naps3-canon-mf4410.rules \
    packaging/naps3-usb-permissions LICENSE.ipp-usb; do
    [[ -f "$SCRIPT_DIR/$file" ]] || {
        echo "В папке установщика отсутствует файл: $file" >&2
        exit 1
    }
done

package_exists() {
    dnf -q list --showduplicates "$1" >/dev/null 2>&1
}

install_first_available() {
    local candidate
    for candidate in "$@"; do
        if package_exists "$candidate"; then
            dnf -y install "$candidate"
            return 0
        fi
    done

    echo "В подключённых репозиториях не найден пакет: $*" >&2
    return 1
}

install_optional_first_available() {
    local candidate
    for candidate in "$@"; do
        if package_exists "$candidate"; then
            dnf -y install "$candidate"
            return 0
        fi
    done

    echo "Предупреждение: дополнительный пакет не найден: $*" >&2
    return 0
}

echo "Установка системных компонентов…"
dnf -y makecache

install_first_available python3
install_first_available python3-gobject pygobject3
install_first_available gtk3
install_first_available python3-pillow python3-Pillow
install_first_available sane-backends
install_first_available sane-backends-drivers-scanners sane-backends
install_first_available sane-airscan
install_first_available ipp-usb
install_optional_first_available hplip hplip-common
install_first_available poppler-utils poppler

# The distro example enables trace-all and DNS-SD retries. Both are unsuitable
# on a production workstation with Avahi disabled and slow down colour duplex.
install -d -m 755 /etc/ipp-usb
install -m 644 "$SCRIPT_DIR/ipp-usb.conf" /etc/ipp-usb/ipp-usb.conf

# РЕД ОС 8 поставляет ipp-usb 0.9.27. На M428/M429 он читает цветной
# eSCL-поток по 4096 байт и после каждого 8-КиБ USB burst получает ZLP,
# добавляя к нему 10 мс backoff. Исправленная сборка использует большой
# входной буфер и не делает эту паузу только для точного VID:PID M428/M429.
# Системный пакет не перезаписывается: drop-in легко вернуть удалением.
install -d -m 755 /opt/naps3
install -m 755 "$SCRIPT_DIR/ipp-usb-naps3" /opt/naps3/ipp-usb-naps3
install -m 644 "$SCRIPT_DIR/LICENSE.ipp-usb" /opt/naps3/LICENSE.ipp-usb
install -d -m 755 /etc/ipp-usb/quirks
install -m 644 \
    "$SCRIPT_DIR/ipp-usb-m428.conf" \
    /etc/ipp-usb/quirks/90-naps3-hp-m428.conf
install -d -m 755 /etc/systemd/system/ipp-usb.service.d
install -m 644 \
    "$SCRIPT_DIR/ipp-usb-naps3.service.conf" \
    /etc/systemd/system/ipp-usb.service.d/90-naps3-m428.conf

# На M428/M429 hpaio может видеть USB-устройство, но падать с I/O
# при открытии. ipp-usb даёт стабильный eSCL для стекла и АПД.
# Unit статический и запускается udev, поэтому enable ему не нужен.
systemctl unmask ipp-usb.service 2>/dev/null || true
systemctl daemon-reload
systemctl restart ipp-usb.service 2>/dev/null || true

# Canon MF4410 uses the SANE pixma backend. The late rule also covers remote
# desktop sessions where systemd-logind does not add an uaccess ACL.
install -d -m 755 /etc/udev/rules.d
rm -f /etc/udev/rules.d/60-naps3-scanners.rules
install -m 644 \
    "$SCRIPT_DIR/packaging/99-naps3-canon-mf4410.rules" \
    /etc/udev/rules.d/99-naps3-canon-mf4410.rules
"$SCRIPT_DIR/packaging/naps3-usb-permissions"

echo "Остановка запущенной старой версии NAPS3…"
pkill -f '/opt/naps3/naps3.py' 2>/dev/null || true
sleep 1

install -d -m 755 /opt/naps3
install -m 755 "$SCRIPT_DIR/packaging/naps3-usb-permissions" \
    /opt/naps3/naps3-usb-permissions
install -m 755 "$SCRIPT_DIR/naps3.py" /opt/naps3/naps3.py.new
mv -f /opt/naps3/naps3.py.new /opt/naps3/naps3.py
install -m 644 "$SCRIPT_DIR/windows_backend.py" /opt/naps3/windows_backend.py.new
mv -f /opt/naps3/windows_backend.py.new /opt/naps3/windows_backend.py
install -m 644 "$SCRIPT_DIR/naps3.png" /opt/naps3/naps3.png
rm -rf /opt/naps3/__pycache__

install -d -m 755 /usr/share/icons/hicolor/scalable/apps
install -m 644 \
    "$SCRIPT_DIR/naps3.svg" \
    /usr/share/icons/hicolor/scalable/apps/naps3.svg

install -d -m 755 /usr/share/icons/hicolor/256x256/apps
install -m 644 \
    "$SCRIPT_DIR/naps3.png" \
    /usr/share/icons/hicolor/256x256/apps/naps3.png

install -m 755 "$SCRIPT_DIR/packaging/naps3-launcher" /usr/local/bin/naps3.new
mv -f /usr/local/bin/naps3.new /usr/local/bin/naps3

cat >/usr/share/applications/ru.redos.NAPS3.desktop <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=NAPS3
GenericName=Сканирование документов
Comment=Многостраничное сканирование и сохранение PDF/изображений
Exec=/usr/local/bin/naps3
Icon=naps3
Terminal=false
StartupWMClass=NAPS3
DBusActivatable=false
Categories=Graphics;Scanning;
Keywords=сканер;сканирование;PDF;изображения;ADF;
StartupNotify=true
EOF

# Удаляем ярлык старой сборки, чтобы в меню не было дублей.
rm -f /usr/share/applications/naps3.desktop

update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true

if [[ -n "$TARGET_USER" ]] && id "$TARGET_USER" >/dev/null 2>&1; then
    user_home="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    desktop_dir=""

    for candidate in "$user_home/Рабочий стол" "$user_home/Desktop"; do
        if [[ -d "$candidate" ]]; then
            desktop_dir="$candidate"
            break
        fi
    done

    if [[ -n "$desktop_dir" ]]; then
        rm -f "$desktop_dir/NAPS3.desktop"
        cp \
            /usr/share/applications/ru.redos.NAPS3.desktop \
            "$desktop_dir/NAPS3.desktop"
        chown \
            "$TARGET_USER:$(id -gn "$TARGET_USER")" \
            "$desktop_dir/NAPS3.desktop"
        chmod 755 "$desktop_dir/NAPS3.desktop"

        runuser -u "$TARGET_USER" -- \
            gio set \
            "$desktop_dir/NAPS3.desktop" \
            metadata::trusted true \
            >/dev/null 2>&1 || true
    fi
fi

/usr/bin/python3 -m py_compile /opt/naps3/naps3.py /opt/naps3/windows_backend.py

INSTALLED_VERSION="$(
    sed -nE 's/^APP_VERSION = "([^"]+)"/\1/p' /opt/naps3/naps3.py |
    head -n 1
)"
EXPECTED_VERSION="$(sed -nE 's/^APP_VERSION = "([^"]+)"/\1/p' "$SCRIPT_DIR/naps3.py" | head -n 1)"
if [[ "$INSTALLED_VERSION" != "$EXPECTED_VERSION" ]]; then
    echo "Ошибка проверки: установлена версия '$INSTALLED_VERSION', ожидалась $EXPECTED_VERSION." >&2
    exit 1
fi

cmp -s "$SCRIPT_DIR/ipp-usb-naps3" /opt/naps3/ipp-usb-naps3 || {
    echo "Ошибка проверки исправленного ipp-usb." >&2
    exit 1
}

echo
echo "NAPS3 $INSTALLED_VERSION установлен или обновлён."
echo "Доступны ipp-usb/eSCL и локальные SANE-драйверы, включая Canon pixma."
echo
echo "Полностью закройте старое окно NAPS3 и запустите приложение снова:"
echo "  naps3"
