#!/usr/bin/env bash
# Собирает RPM без установки rpm-build в систему.
set -Eeuo pipefail

if [[ $(uname -m) != "x86_64" ]]; then
    echo "Пакет с исправленным ipp-usb собирается только для x86_64." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(
    sed -nE 's/^APP_VERSION = "([^"]+)"/\1/p' "$SCRIPT_DIR/naps3.py" |
    head -n 1
)"

if [[ -z "$VERSION" ]]; then
    echo "Не удалось определить версию NAPS3." >&2
    exit 1
fi

for file in \
    naps3.py windows_backend.py naps3.svg naps3.png ipp-usb.conf ipp-usb-naps3 \
    ipp-usb-m428.conf LICENSE LICENSE.ipp-usb README.txt RPM_INSTALL.txt \
    packaging/naps3.spec packaging/naps3-launcher \
    packaging/ru.redos.NAPS3.desktop \
    packaging/ipp-usb-naps3-rpm.service.conf \
    packaging/99-naps3-canon-mf4410.rules \
    packaging/naps3-usb-permissions; do
    [[ -s "$SCRIPT_DIR/$file" ]] || {
        echo "Отсутствует файл пакета: $file" >&2
        exit 1
    }
done

WORK_DIR="$(mktemp -d /tmp/naps3-rpm-build.XXXXXX)"
trap 'rm -rf -- "$WORK_DIR"' EXIT

RPMBUILD="$(command -v rpmbuild || true)"
RPM_CONFIG_DIR=""

if [[ -z "$RPMBUILD" ]]; then
    command -v dnf >/dev/null 2>&1 || {
        echo "Для загрузки rpm-build требуется dnf." >&2
        exit 1
    }
    command -v rpm2cpio >/dev/null 2>&1 || {
        echo "Не найден rpm2cpio." >&2
        exit 1
    }
    command -v cpio >/dev/null 2>&1 || {
        echo "Не найден cpio." >&2
        exit 1
    }

    mkdir -p "$WORK_DIR/tools/rpms" "$WORK_DIR/tools/root/usr/lib/rpm"
    dnf -4 -q download \
        --arch x86_64 \
        --destdir "$WORK_DIR/tools/rpms" \
        rpm-build
    RPM_PACKAGE="$(
        find "$WORK_DIR/tools/rpms" -maxdepth 1 -type f \
            -name 'rpm-build-[0-9]*.x86_64.rpm' |
        head -n 1
    )"
    [[ -n "$RPM_PACKAGE" ]] || {
        echo "Не удалось загрузить rpm-build для x86_64." >&2
        exit 1
    }

    cp -a /usr/lib/rpm/. "$WORK_DIR/tools/root/usr/lib/rpm/"
    (
        cd "$WORK_DIR/tools/root"
        rpm2cpio "$RPM_PACKAGE" | cpio -idm --quiet
    )
    RPMBUILD="$WORK_DIR/tools/root/usr/bin/rpmbuild"
    RPM_CONFIG_DIR="$WORK_DIR/tools/root/usr/lib/rpm"
fi

TOP_DIR="$WORK_DIR/rpmbuild"
SOURCE_DIR="$WORK_DIR/naps3-$VERSION"
mkdir -p \
    "$TOP_DIR/BUILD" "$TOP_DIR/BUILDROOT" "$TOP_DIR/RPMS" \
    "$TOP_DIR/SOURCES" "$TOP_DIR/SPECS" "$TOP_DIR/SRPMS" \
    "$SOURCE_DIR/packaging"

for file in \
    naps3.py windows_backend.py naps3.svg naps3.png ipp-usb.conf ipp-usb-naps3 \
    ipp-usb-m428.conf LICENSE LICENSE.ipp-usb README.txt RPM_INSTALL.txt; do
    install -m 0644 "$SCRIPT_DIR/$file" "$SOURCE_DIR/$file"
done
chmod 0755 "$SOURCE_DIR/naps3.py" "$SOURCE_DIR/ipp-usb-naps3"

for file in \
    naps3-launcher ru.redos.NAPS3.desktop \
    ipp-usb-naps3-rpm.service.conf 99-naps3-canon-mf4410.rules \
    naps3-usb-permissions; do
    install -m 0644 \
        "$SCRIPT_DIR/packaging/$file" \
        "$SOURCE_DIR/packaging/$file"
done
chmod 0755 "$SOURCE_DIR/packaging/naps3-launcher"
chmod 0755 "$SOURCE_DIR/packaging/naps3-usb-permissions"

tar --sort=name \
    --owner=0 --group=0 --numeric-owner \
    -C "$WORK_DIR" \
    -czf "$TOP_DIR/SOURCES/naps3-$VERSION.tar.gz" \
    "naps3-$VERSION"
install -m 0644 \
    "$SCRIPT_DIR/packaging/naps3.spec" \
    "$TOP_DIR/SPECS/naps3.spec"

RPM_ARGS=(
    -bb "$TOP_DIR/SPECS/naps3.spec"
    --define "_topdir $TOP_DIR"
    --target x86_64
)
if [[ -n "$RPM_CONFIG_DIR" ]]; then
    RPM_ARGS+=(--define "_rpmconfigdir $RPM_CONFIG_DIR")
fi

"$RPMBUILD" "${RPM_ARGS[@]}"

RPM_FILE="$(
    find "$TOP_DIR/RPMS" -type f -name 'naps3-*.x86_64.rpm' |
    head -n 1
)"
[[ -n "$RPM_FILE" ]] || {
    echo "RPM не был создан." >&2
    exit 1
}

mkdir -p "$SCRIPT_DIR/dist"
install -m 0644 "$RPM_FILE" "$SCRIPT_DIR/dist/$(basename "$RPM_FILE")"
install -m 0644 \
    "$SCRIPT_DIR/RPM_INSTALL.txt" \
    "$SCRIPT_DIR/dist/УСТАНОВКА.txt"

(
    cd "$SCRIPT_DIR/dist"
    sha256sum "$(basename "$RPM_FILE")" > SHA256SUMS
)

echo
echo "Готово: $SCRIPT_DIR/dist/$(basename "$RPM_FILE")"
cat "$SCRIPT_DIR/dist/SHA256SUMS"
