"""Optional, user-local Kyocera MA4000x SANE backend from Kyocera's site.

No vendor files are shipped with NAPS3.  In particular, the vendor RPM is
never executed: its scripts alter system libraries and its udev rule grants
access to every USB device.  Only the verified backend and its dependencies
are copied into this user's private NAPS3 directory.
"""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import os
import platform
import struct
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional


DRIVER_VERSION = "2.0.3523"
BACKEND = "kyocera_wc3_usb"
OFFICIAL_PAGE = (
    "https://www.kyoceradocumentsolutions.us/en/support/"
    "downloads.name-L3VzL2VzL21mcC9FQ09TWVNNQTQwMDBY.html"
)
LICENSE_URL = OFFICIAL_PAGE
ARCHIVE_URL = (
    "https://www.kyoceradocumentsolutions.us/content/dam/"
    "download-center-americas-cf/us/drivers/drivers/"
    "MA4000WIFX_PA4000wx_SANE_v2_0_3523_zip.download.zip"
)
ARCHIVE_SHA256 = "a0c3f9a303742520da94bb47b37da9866b2a45c1c5bd002c093170735443137f"
RPM_NAME = "kyocera-sane-2.0-3523.x86_64.rpm"
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_PAYLOAD_BYTES = 80 * 1024 * 1024

_FILES = {
    "./etc/sane.d/kyocera_wc3_usb.conf": "sane/kyocera_wc3_usb.conf",
    "./etc/sane.d/kyocera_devices.conf": "sane/kyocera_devices.conf",
    "./usr/lib64/libkmadrwapi.so": "lib/libkmadrwapi.so",
    "./usr/lib64/libkmcmnapi2.so": "lib/libkmcmnapi2.so",
    "./usr/lib64/libkmencapi.so": "lib/libkmencapi.so",
    "./usr/lib64/libkmip.so.1.0.705": "lib/libkmip.so.1.0.705",
    "./usr/lib64/libkmscnapi.so": "lib/libkmscnapi.so",
    "./usr/lib64/sane/libsane-kyocera_wc3_usb.so.1.0.24":
        "lib/libsane-kyocera_wc3_usb.so.1.0.24",
    "./usr/local/kyocera/scanner/libcrypto.so.1.1": "lib/libcrypto.so.1.1",
    "./usr/local/kyocera/scanner/libjpeg.so.8.4.0": "lib/libjpeg.so.8.4.0",
    "./usr/local/kyocera/scanner/libssl.so.1.1": "lib/libssl.so.1.1",
    "./usr/local/kyocera/scanner/libtiff.so.4.3.4": "lib/libtiff.so.4.3.4",
}
_LINKS = {
    "libkmip.so.1": "libkmip.so.1.0.705",
    "libkmscnapi.so.1": "libkmscnapi.so",
    "libkmencapi.so.1": "libkmencapi.so",
    "libkmadrwapi.so.1": "libkmadrwapi.so",
    "libkmcmnapi2.so.1": "libkmcmnapi2.so",
    "libsane-kyocera_wc3_usb.so.1": "libsane-kyocera_wc3_usb.so.1.0.24",
    "libjpeg.so.8": "libjpeg.so.8.4.0",
    "libtiff.so.4": "libtiff.so.4.3.4",
}


class KyoceraDriverError(RuntimeError):
    pass


def driver_root(data_home: Optional[Path] = None) -> Path:
    if data_home is None:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return data_home / "naps3" / "kyocera-sane" / DRIVER_VERSION


def usb_device_present(sysfs_root: Path = Path("/sys/bus/usb/devices")) -> bool:
    try:
        for vendor_path in sysfs_root.glob("*/idVendor"):
            if vendor_path.read_text(encoding="ascii").strip().lower() != "0482":
                continue
            product_path = vendor_path.with_name("idProduct")
            if product_path.read_text(encoding="ascii").strip().lower() == "0de0":
                return True
    except (OSError, UnicodeError):
        pass
    return False


def is_installed(root: Optional[Path] = None) -> bool:
    root = root or driver_root()
    try:
        marker = json.loads((root / "installed.json").read_text(encoding="utf-8"))
        return (
            marker.get("archive_sha256") == ARCHIVE_SHA256
            and (root / "sane/dll.conf").read_text(encoding="ascii").strip() == BACKEND
            and all((root / target).is_file() for target in _FILES.values())
            and all((root / "lib" / link).is_file() for link in _LINKS)
        )
    except (OSError, ValueError, AttributeError):
        return False


def sane_environment(root: Optional[Path] = None) -> Optional[dict[str, str]]:
    root = root or driver_root()
    if not is_installed(root):
        return None
    env = {**os.environ, "LC_ALL": "C"}
    existing_libraries = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = str(root / "lib") + (
        os.pathsep + existing_libraries if existing_libraries else ""
    )
    env["SANE_CONFIG_DIR"] = str(root / "sane")
    env.pop("SANE_AIRSCAN_DEVICE", None)
    return env


def download_official_archive() -> bytes:
    request = urllib.request.Request(
        ARCHIVE_URL, headers={"User-Agent": "NAPS3 scanner setup"}
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        archive = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise KyoceraDriverError("Архив драйвера превышает ожидаемый размер.")
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise KyoceraDriverError(
            "Контрольная сумма драйвера Kyocera изменилась. "
            "Не устанавливайте неизвестный пакет; обновите NAPS3."
        )
    return archive


def _rpm_payload(rpm: bytes) -> bytes:
    def header_end(offset: int) -> int:
        if rpm[offset:offset + 4] != b"\x8e\xad\xe8\x01":
            raise KyoceraDriverError("Некорректный заголовок RPM Kyocera.")
        count, size = struct.unpack_from(">II", rpm, offset + 8)
        if count > 10000 or size > MAX_ARCHIVE_BYTES:
            raise KyoceraDriverError("Недопустимый размер заголовка RPM.")
        end = offset + 16 + count * 16 + size
        if end > len(rpm):
            raise KyoceraDriverError("RPM Kyocera обрезан.")
        return end

    if rpm[:4] != b"\xed\xab\xee\xdb" or len(rpm) < 128:
        raise KyoceraDriverError("В архиве Kyocera нет корректного RPM.")
    signature_end = header_end(96)
    main_end = header_end((signature_end + 7) & ~7)
    compressed = rpm[main_end:]
    if not compressed.startswith(b"\xfd7zXZ\x00"):
        raise KyoceraDriverError("Неизвестный формат данных RPM Kyocera.")
    decoder = lzma.LZMADecompressor()
    try:
        payload = decoder.decompress(compressed, max_length=MAX_PAYLOAD_BYTES + 1)
    except lzma.LZMAError as exc:
        raise KyoceraDriverError("Не удалось прочитать RPM Kyocera.") from exc
    if not decoder.eof or len(payload) > MAX_PAYLOAD_BYTES:
        raise KyoceraDriverError("Распакованный RPM Kyocera слишком велик или обрезан.")
    return payload


def _selected_files(payload: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    offset = 0
    for _ in range(1000):
        if payload[offset:offset + 6] != b"070701":
            raise KyoceraDriverError("Повреждён список файлов драйвера Kyocera.")
        if offset + 110 > len(payload):
            raise KyoceraDriverError("Список файлов драйвера обрезан.")
        try:
            fields = [
                int(payload[offset + 6 + index * 8:offset + 14 + index * 8], 16)
                for index in range(13)
            ]
        except ValueError as exc:
            raise KyoceraDriverError("Повреждён заголовок файла драйвера.") from exc
        mode, size, name_size = fields[1], fields[6], fields[11]
        if name_size < 2 or name_size > 4096 or size > MAX_PAYLOAD_BYTES:
            raise KyoceraDriverError("Недопустимый файл в архиве драйвера.")
        name_begin = offset + 110
        name_end = name_begin + name_size
        if name_end > len(payload) or payload[name_end - 1] != 0:
            raise KyoceraDriverError("Имя файла драйвера обрезано.")
        name = payload[name_begin:name_end - 1].decode("utf-8", "replace")
        content_begin = (name_end + 3) & ~3
        content_end = content_begin + size
        if content_end > len(payload):
            raise KyoceraDriverError("Файл драйвера обрезан.")
        if name == "TRAILER!!!":
            break
        if name in _FILES:
            if (mode & 0o170000) != 0o100000 or not size or name in files:
                raise KyoceraDriverError("Некорректный файл в RPM Kyocera.")
            files[name] = payload[content_begin:content_end]
        offset = (content_end + 3) & ~3
    else:
        raise KyoceraDriverError("Слишком много файлов в RPM Kyocera.")
    if set(files) != set(_FILES):
        raise KyoceraDriverError("В RPM Kyocera отсутствуют нужные файлы.")
    return files


def install_verified_archive(archive: bytes, root: Optional[Path] = None) -> Path:
    """Install only allowlisted files, without running the RPM or changing system files."""
    root = root or driver_root()
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise KyoceraDriverError("Контрольная сумма архива Kyocera не совпадает.")
    if is_installed(root):
        return root
    if root.exists():
        raise KyoceraDriverError(
            f"Папка драйвера уже существует, но неполна: {root}. "
            "Удалите её вручную после проверки содержимого."
        )
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        names = package.namelist()
        if names.count(RPM_NAME) != 1:
            raise KyoceraDriverError("В архиве Kyocera не найден ожидаемый RPM.")
        rpm_info = package.getinfo(RPM_NAME)
        if rpm_info.file_size > MAX_ARCHIVE_BYTES:
            raise KyoceraDriverError("RPM Kyocera превышает ожидаемый размер.")
        selected = _selected_files(_rpm_payload(package.read(rpm_info)))

    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".kyocera-", dir=root.parent) as temporary:
        staging = Path(temporary)
        (staging / "lib").mkdir()
        (staging / "sane").mkdir()
        for name, relative_path in _FILES.items():
            (staging / relative_path).write_bytes(selected[name])
        for link, target in _LINKS.items():
            (staging / "lib" / link).symlink_to(target)
        (staging / "sane/dll.conf").write_text(BACKEND + "\n", encoding="ascii")
        (staging / "installed.json").write_text(
            json.dumps({"version": DRIVER_VERSION, "archive_sha256": ARCHIVE_SHA256}),
            encoding="utf-8",
        )
        staging.rename(root)
    return root


def install_official_driver() -> Path:
    if os.name != "posix" or platform.machine() != "x86_64":
        raise KyoceraDriverError("Этот драйвер Kyocera предназначен для Linux x86_64.")
    if is_installed():
        return driver_root()
    try:
        archive = download_official_archive()
        return install_verified_archive(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise KyoceraDriverError(f"Не удалось установить драйвер Kyocera: {exc}") from exc
