#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустите через sudo или от root." >&2
    exit 1
fi

if rpm -q naps3 >/dev/null 2>&1; then
    echo "NAPS3 установлен как RPM-пакет." >&2
    echo "Этот старый скрипт не будет удалять файлы, которыми управляет RPM." >&2
    echo "Используйте: sudo dnf remove naps3" >&2
    exit 1
fi

rm -rf /opt/naps3
rm -f /usr/local/bin/naps3
rm -f /usr/share/applications/naps3.desktop
rm -f /usr/share/applications/ru.redos.NAPS3.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/naps3.svg
rm -f /usr/share/icons/hicolor/256x256/apps/naps3.png
rm -f /etc/ipp-usb/quirks/90-naps3-hp-m428.conf
rm -f /etc/systemd/system/ipp-usb.service.d/90-naps3-m428.conf
rm -f /etc/udev/rules.d/60-naps3-scanners.rules
rmdir /etc/systemd/system/ipp-usb.service.d 2>/dev/null || true
systemctl daemon-reload
systemctl restart ipp-usb.service 2>/dev/null || true
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --action=add --subsystem-match=usb \
    --attr-match=idVendor=04a9 --attr-match=idProduct=2737 \
    2>/dev/null || true

update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true

echo "NAPS3 удалён."
