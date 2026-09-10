#!/usr/bin/env python3
"""
NAPS3 — графическое сканирование документов для РЕД ОС и Windows.

Версия 0.9:
- добавлен официальный CLI регистрации сканера для Printer Doctor;
- профиль проверяется по точному SANE ID или eSCL-адресу;
- настройки обновляются атомарно с резервной копией и блокировкой GUI;
- регистрация пользовательского профиля от root запрещена;
- добавлена Windows-версия с системным backend Windows Image Acquisition;
- интерфейс, страницы, импорт и защищённое сохранение общие для обеих ОС;
- Windows-сборка включает GTK 3, Pillow и pdftoppm;
- интерфейс полностью переведён с Tkinter на GTK 3 / PyGObject;
- используются системная тема, HeaderBar, стандартные диалоги и значки РЕД ОС;
- сохранён рабочий backend sane-airscan для принтеров с eSCL/IPP-over-USB;
- локальные Linux-сканеры обнаруживаются через установленные SANE-backend'ы;
- USB M428/M429 переведён с нерабочего hpaio на ipp-usb/eSCL;
- сломанные hpaio-профили больше не показываются как рабочие;
- ручной дуплекс показывается только для одностороннего АПД;
- рабочий сценарий не запускает sudo, pkexec или systemctl;
- отключена трассировка каждого USB-блока в ipp-usb;
- устранён 10-мс простой после каждого нулевого USB-пакета M428/M429;
- локальный airscan:eN сразу открывается с правильной USB-конфигурацией.
- отмена гарантированно останавливает зависший backend и возвращает интерфейс;
- выделение страницы сохраняется после поворота.
- в режиме отдельных файлов доступен PDF; каждая страница сохраняется в отдельный одностраничный PDF.
- PDF выбран по умолчанию в списке форматов сохранения.
- восстановление больше не переключает текущее задание на другое МФУ;
- сохранение защищено временной записью и откатом заменённых файлов;
- изменения страниц блокируются во время сохранения;
- при закрытии предлагается сохранить изменённый документ.
- повреждённые ответы SANE проверяются до добавления страницы в проект;
- ошибка предпросмотра больше не создаёт повторяющиеся модальные окна.
- фоновая проверка устройства больше не пересекается со сканированием;
- локальный SANE один раз повторяет запуск при кратком ответе Device busy.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from collections import deque
import json
import locale
import ssl
import os
import re
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on Windows
    fcntl = None

from windows_backend import (
    WindowsBackendError,
    bridge_error_text,
    build_wia_scan_command,
    discover_wia_scanners,
    find_runtime_executable,
    probe_wia_device,
    resource_path,
    run_wia_bridge,
    windows_creation_flags,
)


IS_WINDOWS = os.name == "nt"

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")

    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk
except (ImportError, ValueError) as exc:
    dependency_hint = (
        "Переустановите NAPS3 для Windows."
        if IS_WINDOWS
        else "Установите зависимости:\n  sudo dnf install python3-gobject gtk3"
    )
    print(
        "Не удалось загрузить GTK 3 / PyGObject.\n"
        f"{dependency_hint}\n\n"
        f"Технические сведения: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError as exc:
    dependency_hint = (
        "Переустановите NAPS3 для Windows."
        if IS_WINDOWS
        else "Установите пакет:\n  sudo dnf install python3-pillow"
    )
    print(
        "Не удалось загрузить Pillow.\n"
        f"{dependency_hint}\n\n"
        f"Технические сведения: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)


APP_ID = "ru.redos.NAPS3"
APP_NAME = "NAPS3"
APP_VERSION = "0.9.0"
DEFAULT_MATCH = ""
DEFAULT_SCANNER_NAME = "Сканер Windows" if IS_WINDOWS else "Сканер"
DEFAULT_ESCL_URL = "http://127.0.0.1:60000/eSCL"
PROFILE_KEY = "scanner_profile"
LEGACY_DEFAULT_MATCHES = {"mfp-yur"}
MAX_ADF_PAGES = 200
MAX_SCAN_PIXELS = 50_000_000

if IS_WINDOWS:
    CONFIG_DIR = Path(
        os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
    ) / "NAPS3"
    CACHE_DIR = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    ) / "NAPS3"
else:
    CONFIG_DIR = Path.home() / ".config" / "naps3"
    CACHE_DIR = Path.home() / ".cache" / "naps3"
CONFIG_FILE = CONFIG_DIR / "settings.json"
CONFIG_LOCK_FILE = CONFIG_DIR / "settings.lock"
FAST_SANE_DIR = CACHE_DIR / "sane-fast"
USB_SANE_DIR = CACHE_DIR / "sane-usb-hp"
SCAN_LOG_FILE = CACHE_DIR / "scan.log"

DEFAULT_EXPORT_FORMAT = "PDF"

FORMAT_INFO = {
    "PDF": {"extension": ".pdf", "pillow": "PDF", "label": "PDF"},
    "PNG": {"extension": ".png", "pillow": "PNG", "label": "PNG"},
    "JPEG": {"extension": ".jpg", "pillow": "JPEG", "label": "JPEG"},
    "TIFF": {"extension": ".tiff", "pillow": "TIFF", "label": "TIFF"},
    "BMP": {"extension": ".bmp", "pillow": "BMP", "label": "BMP"},
    "WEBP": {"extension": ".webp", "pillow": "WEBP", "label": "WebP"},
}


class FileChooserSortGuard:
    """Temporarily enforce predictable sorting in GTK file choosers."""

    SCHEMA_ID = "org.gtk.Settings.FileChooser"

    def __init__(self) -> None:
        self.settings: Optional[Gio.Settings] = None
        self.previous: dict[str, object] = {}

    def __enter__(self) -> "FileChooserSortGuard":
        try:
            source = Gio.SettingsSchemaSource.get_default()
            schema = source.lookup(self.SCHEMA_ID, True) if source else None
            if schema is None:
                return self

            self.settings = Gio.Settings.new_full(schema, None, None)
            self.previous = {
                "sort-column": self.settings.get_enum("sort-column"),
                "sort-order": self.settings.get_enum("sort-order"),
                "sort-directories-first": self.settings.get_boolean(
                    "sort-directories-first"
                ),
            }
            self.settings.set_enum("sort-column", 0)
            self.settings.set_enum("sort-order", 0)
            self.settings.set_boolean("sort-directories-first", True)
            Gio.Settings.sync()
        except (GLib.Error, TypeError, ValueError):
            self.settings = None
            self.previous = {}
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self.settings is not None and self.previous:
            try:
                self.settings.set_enum(
                    "sort-column", int(self.previous["sort-column"])
                )
                self.settings.set_enum(
                    "sort-order", int(self.previous["sort-order"])
                )
                self.settings.set_boolean(
                    "sort-directories-first",
                    bool(self.previous["sort-directories-first"]),
                )
                Gio.Settings.sync()
            except (GLib.Error, TypeError, ValueError):
                pass
        return False


class Naps3Error(RuntimeError):
    """Ошибка, сформулированная для пользователя."""


class SettingsBusyError(Naps3Error):
    """Настройки заняты запущенным экземпляром приложения."""


def export_target_state(path: Path) -> Optional[tuple[int, ...]]:
    """Remember the file approved for replacement, without following links."""
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode):
        raise Naps3Error(
            f"Нельзя заменить «{path}»: это не обычный файл. "
            "Выберите другое имя."
        )
    return (
        value.st_dev, value.st_ino, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns, value.st_mode,
    )


@contextmanager
def staged_export_files(
    outputs: list[Path],
    expected: Optional[dict[Path, Optional[tuple[int, ...]]]] = None,
) -> Iterator[list[Path]]:
    """Encode the whole set before replacement; roll back a failed commit."""
    if not outputs or len({path.resolve() for path in outputs}) != len(outputs):
        raise Naps3Error("Для каждой страницы нужен отдельный путь сохранения.")
    states = (
        {path: export_target_state(path) for path in outputs}
        if expected is None else expected
    )
    temporary: list[Path] = []
    backups: dict[Path, Path] = {}
    committed: dict[Path, Optional[tuple[int, ...]]] = {}
    retained: set[Path] = set()

    def allocate(output: Path, prefix: str) -> Path:
        descriptor, name = tempfile.mkstemp(
            dir=output.parent, prefix=prefix, suffix=".tmp"
        )
        os.close(descriptor)
        path = Path(name)
        temporary.append(path)
        return path

    def check_targets() -> None:
        for output in outputs:
            if export_target_state(output) != states[output]:
                raise Naps3Error(
                    f"Файл «{output}» изменился после выбора места сохранения. "
                    "Проверьте его и повторите сохранение."
                )

    try:
        check_targets()
        staged = [allocate(output, ".naps3-") for output in outputs]
        yield staged
        for path in staged:
            with path.open("rb+") as stream:
                os.fsync(stream.fileno())
        check_targets()
        for output, path in zip(outputs, staged):
            if states[output] is not None:
                backup = allocate(output, ".naps3-backup-")
                shutil.copy2(output, backup)
                with backup.open("rb+") as stream:
                    os.fsync(stream.fileno())
                backups[output] = backup
                os.chmod(path, stat.S_IMODE(states[output][-1]))
        check_targets()
        for output, path in zip(outputs, staged):
            # Check again immediately before replacement, including new files.
            if export_target_state(output) != states[output]:
                raise Naps3Error(f"Файл «{output}» изменился во время сохранения.")
            os.replace(path, output)
            committed[output] = None
            committed[output] = export_target_state(output)
    except Exception as exc:
        failed: list[str] = []
        for output, written_state in reversed(list(committed.items())):
            backup = backups.get(output)
            try:
                if written_state is not None and export_target_state(output) != written_state:
                    raise OSError("Файл изменён другой программой")
                if backup is not None:
                    os.replace(backup, output)
                else:
                    output.unlink()
            except (OSError, Naps3Error):
                if backup is not None:
                    retained.add(backup)
                    failed.append(f"{output}\nПрежний файл: {backup}")
                else:
                    failed.append(str(output))
        if failed:
            raise Naps3Error(
                "Сохранение прервано. Не удалось полностью отменить замену "
                "файлов; проверьте указанные пути:\n\n" + "\n\n".join(failed)
            ) from exc
        raise
    finally:
        for path in temporary:
            if path not in retained:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


@dataclass(slots=True)
class Page:
    path: Path
    label: str


@dataclass(slots=True)
class EsclJobSnapshot:
    """State of one exact ScanJob reported inside ScannerStatus."""

    uri: str
    state: str
    images_completed: Optional[int]
    images_to_transfer: Optional[int]
    age: Optional[int]

    @property
    def is_terminal(self) -> bool:
        state = self.state.casefold()
        return any(
            marker in state
            for marker in ("completed", "aborted", "canceled", "cancelled")
        )


def interleave_manual_duplex_pages(
    front_pages: list[Path],
    back_pages: list[Path],
    reverse_backs: bool = False,
) -> list[Path]:
    """Merge two simplex passes into front/back document order.

    Extra pages are preserved at the end so a loading mistake never discards
    a scan. The caller reports a count mismatch to the user.
    """
    ordered_backs = list(reversed(back_pages)) if reverse_backs else list(back_pages)
    merged: list[Path] = []
    paired_count = min(len(front_pages), len(ordered_backs))
    for index in range(paired_count):
        merged.extend((front_pages[index], ordered_backs[index]))
    merged.extend(front_pages[paired_count:])
    merged.extend(ordered_backs[paired_count:])
    return merged


def rotate_page_file_180(path: Path) -> Path:
    """Rotate one prepared PNG in place using an atomic replacement."""
    temporary = path.with_name(f"{path.stem}.rotating{path.suffix}")
    try:
        with Image.open(path) as source_image:
            source = ImageOps.exif_transpose(source_image).copy()
        rotated = source.transpose(Image.Transpose.ROTATE_180)
        source.close()
        rotated.save(temporary, format="PNG", compress_level=2)
        rotated.close()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def compact_details(value: object, limit: int = 700) -> str:
    text = str(value).replace("\x00", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = "…" + text[-limit:]
    return text


def friendly_general_error(exc: BaseException, operation: str) -> str:
    if isinstance(exc, Naps3Error):
        return str(exc)
    if isinstance(exc, PermissionError):
        return (
            f"Не удалось {operation}: нет прав на выбранный файл или папку. "
            "Выберите другой каталог или проверьте права доступа."
        )
    if isinstance(exc, FileNotFoundError):
        return (
            f"Не удалось {operation}: нужный файл или системный компонент не найден."
        )
    if isinstance(exc, subprocess.TimeoutExpired):
        return (
            f"Не удалось {operation}: устройство или программа не ответили вовремя. "
            "Проверьте подключение и повторите операцию."
        )
    if isinstance(exc, UnidentifiedImageError):
        return (
            f"Не удалось {operation}: файл не является поддерживаемым изображением "
            "или повреждён."
        )
    if isinstance(exc, OSError):
        return (
            f"Не удалось {operation} из-за ошибки работы с файлом или устройством.\n\n"
            f"Технические сведения: {compact_details(exc)}"
        )
    return (
        f"Не удалось {operation}. Повторите операцию. Если ошибка сохранится, "
        "переподключите МФУ и перезапустите NAPS3.\n\n"
        f"Технические сведения: {compact_details(exc)}"
    )


def friendly_scan_error(raw: str, cancelled: bool = False) -> str:
    details = compact_details(raw)
    lower = details.lower()

    if cancelled:
        return "Сканирование отменено пользователем."
    if (
        "document feeder out of documents" in lower
        or "no documents" in lower
        or "0x80210003" in lower
        or "нет документов" in lower
    ):
        return (
            "В автоподатчике нет документов. Положите листы в лоток до "
            "срабатывания датчика и повторите сканирование."
        )
    if (
        "device busy" in lower
        or "resource busy" in lower
        or "0x80210006" in lower
    ):
        return (
            "Сканер занят другой программой. Закройте другие программы "
            "сканирования, затем повторите попытку."
        )
    if "0x80210002" in lower or "paper jam" in lower:
        return "В автоподатчике замята бумага. Устраните замятие и повторите сканирование."
    if "0x80210020" in lower or "multi-feed" in lower:
        return "Сканер обнаружил подачу нескольких листов. Проверьте стопку бумаги."
    if "0x80210007" in lower or "warming up" in lower:
        return "Сканер ещё прогревается. Подождите несколько секунд и повторите операцию."
    if (
        "0x80210005" in lower
        or "0x80210008" in lower
        or "0x8021000a" in lower
        or "0x8021000d" in lower
        or "0x80210015" in lower
        or "offline" in lower
    ):
        return (
            "Выбранный сканер не отвечает Windows. Разбудите МФУ, проверьте "
            "кабель и установленный WIA-драйвер, затем повторите операцию."
        )
    if "permission denied" in lower or "access denied" in lower:
        if IS_WINDOWS:
            return (
                "Нет доступа к сканеру Windows. Закройте другие программы "
                "сканирования или обратитесь к администратору."
            )
        return (
            "Нет доступа к USB-сканеру. Установите локальное обновление NAPS3 "
            f"{APP_VERSION} от имени администратора, затем повторите подключение."
        )
    if any(
        marker in lower
        for marker in (
            "premature end",
            "truncated",
            "broken data stream",
            "cannot identify image",
            "неполное изображение",
            "повреждённое изображение",
        )
    ):
        return (
            "Сканер передал неполное изображение. Повреждённая страница не "
            "добавлена в проект. Проверьте USB-кабель, перезапустите МФУ и "
            "повторите сканирование."
        )
    if "invalid argument" in lower:
        return (
            "Сканер отклонил выбранные параметры или временный адрес "
            "sane-airscan устарел. Попробуйте 300 dpi, A4 и цветной режим."
        )
    if "i/o error" in lower or "input/output error" in lower:
        return (
            "Произошла ошибка обмена со сканером. Проверьте USB-кабель, "
            "питание МФУ и повторите операцию."
        )
    if (
        "no such device" in lower
        or "failed to open" in lower
        or "open of device" in lower
    ):
        return (
            "Сканер отключён или его временный адрес изменился. Переподключите "
            "USB-кабель, нажмите «Найти заново» и повторите сканирование."
        )
    if "unsupported" in lower or "not supported" in lower:
        return (
            "Выбранный режим не поддерживается сканером. Попробуйте "
            "автоподатчик, 300 dpi и цветной режим."
        )
    if "out of memory" in lower:
        return (
            "Недостаточно памяти для сканирования. Уменьшите разрешение "
            "или число одновременно обрабатываемых страниц."
        )
    if not details:
        return (
            "Сканер не передал ни одной страницы. Проверьте наличие бумаги "
            "в автоподатчике и подключение."
        )
    return (
        "Не удалось завершить сканирование. Проверьте бумагу, подключение "
        "и питание МФУ.\n\n"
        f"Технические сведения: {details}"
    )


def translate_scan_status(line: str) -> str:
    text = line.strip()
    lower = text.lower()

    match = re.search(r"scanning page\s+(\d+)", lower)
    if match:
        return f"Сканируется страница {match.group(1)}…"

    match = re.search(r"scanned page\s+(\d+)", lower)
    if match:
        return f"Страница {match.group(1)} получена."

    match = re.search(r"batch terminated,\s*(\d+)\s*pages? scanned", lower)
    if match:
        return f"Сканирование завершено. Получено страниц: {match.group(1)}."

    if "scanning infinity pages" in lower:
        return "Сканирование всех документов из автоподатчика…"
    if "document feeder out of documents" in lower:
        return "Автоподатчик пуст — пакетное сканирование завершено."
    if "invalid argument" in lower:
        return "Сканер отклонил параметры. Проверяется сохранённый профиль…"
    if "device busy" in lower:
        return "Сканер занят другой программой."
    return "Сканирование выполняется…"


def safe_decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def append_scan_log(message: str) -> None:
    """Append compact scanner diagnostics without interrupting the UI."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with SCAN_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def terminate_subprocess(
    process: subprocess.Popen,
    grace_seconds: float = 1.5,
) -> None:
    """Stop one isolated subprocess and escalate if its backend is stuck.

    Scanner backends occasionally ignore SIGTERM while waiting for USB I/O.
    Every cancellable child is started in its own session, so killing its
    process group cannot affect NAPS3 or unrelated user applications.
    """
    if process.poll() is not None:
        return

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        return

    try:
        process.wait(timeout=max(0.05, grace_seconds))
        return
    except subprocess.TimeoutExpired:
        append_scan_log(
            f"Cancel escalation: process pid={process.pid} ignored SIGTERM"
        )

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        return

    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        append_scan_log(
            f"Cancel failed: process pid={process.pid} survived SIGKILL"
        )


def parse_escl_state(status_text: str) -> dict[str, bool]:
    lower = status_text.casefold()
    return {
        "adf_empty": any(
            marker in lower
            for marker in (
                "scanneradfempty",
                "adfempty",
                "adf state empty",
            )
        ),
        "adf_loaded": any(
            marker in lower
            for marker in (
                "scanneradfloaded",
                "adfloaded",
                "adf state loaded",
            )
        ),
        "job_completed": any(
            marker in lower
            for marker in (
                "jobcompleted",
                "jobstatecompleted",
                "<pwg:jobstate>completed",
            )
        ),
        "job_processing": any(
            marker in lower
            for marker in (
                "jobprocessing",
                "jobstateprocessing",
                "<pwg:jobstate>processing",
            )
        ),
    }


def parse_escl_images_to_transfer(*status_texts: str) -> Optional[int]:
    """Return the current eSCL ImagesToTransfer counter when available.

    HP devices may report an empty feeder immediately after physically pulling
    the last sheet, while one or more already-fed pages are still being
    processed and have not yet become available through NextDocument.  The
    transfer counter is therefore more useful than the feeder sensor for
    deciding whether a ScanJob has been fully drained.
    """
    values: list[int] = []
    pattern = re.compile(
        r"<(?:[A-Za-z_][\w.-]*:)?ImagesToTransfer(?:\s[^>]*)?>"
        r"\s*(\d+)\s*"
        r"</(?:[A-Za-z_][\w.-]*:)?ImagesToTransfer>",
        re.IGNORECASE,
    )
    for status_text in status_texts:
        if not status_text:
            continue
        for match in pattern.finditer(status_text):
            try:
                values.append(max(0, int(match.group(1))))
            except (TypeError, ValueError):
                continue
    if not values:
        return None
    return max(values)


def parse_escl_jobs(status_text: str) -> list[EsclJobSnapshot]:
    """Parse individual jobs instead of mixing stale and current JobInfo."""
    if not status_text:
        return []
    try:
        root = ET.fromstring(status_text)
    except ET.ParseError:
        return []

    def local_name(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    def child_text(parent: ET.Element, name: str) -> str:
        for child in parent.iter():
            if local_name(child).casefold() == name.casefold():
                return (child.text or "").strip()
        return ""

    def optional_int(value: str) -> Optional[int]:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    jobs: list[EsclJobSnapshot] = []
    for element in root.iter():
        if local_name(element).casefold() != "jobinfo":
            continue
        uri = child_text(element, "JobUri")
        uuid_value = child_text(element, "JobUuid")
        if not uri and uuid_value:
            uri = uuid_value
        jobs.append(
            EsclJobSnapshot(
                uri=uri,
                state=child_text(element, "JobState"),
                images_completed=optional_int(
                    child_text(element, "ImagesCompleted")
                ),
                images_to_transfer=optional_int(
                    child_text(element, "ImagesToTransfer")
                ),
                age=optional_int(child_text(element, "Age")),
            )
        )
    return jobs


def find_escl_job(
    status_text: str,
    job_url: str,
) -> Optional[EsclJobSnapshot]:
    """Return only the JobInfo that belongs to ``job_url``."""
    target_path = urllib.parse.urlsplit(job_url).path.rstrip("/")
    target_id = target_path.rsplit("/", 1)[-1]
    for job in parse_escl_jobs(status_text):
        job_path = urllib.parse.urlsplit(job.uri).path.rstrip("/")
        job_id = job_path.rsplit("/", 1)[-1]
        if job_path == target_path or (target_id and job_id == target_id):
            return job
    return None


def escl_job_is_drained(
    job: Optional[EsclJobSnapshot],
    received_pages: int,
) -> bool:
    """True only when the exact terminal job has no unreceived images."""
    if job is None or not job.is_terminal or job.images_to_transfer != 0:
        return False
    return (
        job.images_completed is None
        or job.images_completed <= received_pages
    )


def load_settings() -> dict[str, object]:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict[str, object]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".settings-",
        suffix=".tmp",
        dir=CONFIG_DIR,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as output:
            output.write(json.dumps(settings, ensure_ascii=False, indent=2))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if not IS_WINDOWS:
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, CONFIG_FILE)
    except Exception:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def acquire_settings_lock(*, blocking: bool) -> Optional[object]:
    """Lock settings writes between the GUI and external integrations."""
    if fcntl is None:
        return None
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handle = CONFIG_LOCK_FILE.open("a+b")
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError as exc:
        handle.close()
        raise SettingsBusyError(
            "NAPS3 сейчас открыт. Закройте его окно и повторите регистрацию."
        ) from exc
    return handle


def release_settings_lock(handle: Optional[object]) -> None:
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def backup_settings_file() -> Optional[Path]:
    if not CONFIG_FILE.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = CONFIG_DIR / f"settings.json.backup-{stamp}-{uuid.uuid4().hex[:8]}"
    shutil.copy2(CONFIG_FILE, backup)
    if not IS_WINDOWS:
        os.chmod(backup, 0o600)
    return backup


def ensure_fast_sane_config(
    profile: dict[str, object],
) -> None:
    """Create an app-local sane-airscan configuration for a known URL."""
    url = profile_url(profile)
    if not url:
        raise Naps3Error(
            "Для сохранённого профиля не определён локальный eSCL-адрес."
        )

    FAST_SANE_DIR.mkdir(parents=True, exist_ok=True)
    name = profile_name(profile).replace('"', "'").strip()

    (FAST_SANE_DIR / "dll.conf").write_text(
        "airscan\n",
        encoding="utf-8",
    )
    (FAST_SANE_DIR / "airscan.conf").write_text(
        "[devices]\n"
        f'"{name}" = {url}, eSCL\n'
        "\n"
        "[options]\n"
        "discovery = disable\n"
        "ws-discovery = off\n"
        "protocol = auto\n",
        encoding="utf-8",
    )


def normalized_escl_url(url: str) -> str:
    value = url.strip()
    if not value:
        return ""

    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or "127.0.0.1"
    if host in {"localhost", "::1"}:
        host = "127.0.0.1"

    port = parsed.port or 80
    path = parsed.path.rstrip("/")
    if not path:
        path = "/eSCL"
    elif not path.casefold().endswith("/escl"):
        path += "/eSCL"

    return urllib.parse.urlunsplit(
        (parsed.scheme or "http", f"{host}:{port}", path, "", "")
    )


def normalized_match_filter(value: object) -> str:
    """Drop the HP alias that older releases inserted for every Linux user."""
    match_filter = str(value or "").strip()
    if match_filter.casefold() in LEGACY_DEFAULT_MATCHES:
        return ""
    return match_filter


def profile_name(profile: dict[str, object]) -> str:
    return str(profile.get("name") or DEFAULT_SCANNER_NAME)


def profile_url(profile: dict[str, object]) -> str:
    raw_url = str(profile.get("url") or "").strip()
    return normalized_escl_url(raw_url) if raw_url else ""


def is_legacy_default_profile(profile: dict[str, object]) -> bool:
    """Identify the old automatically-created MFP-YUR/HP M428 profile."""
    backend = str(profile.get("backend") or "airscan").casefold()
    if IS_WINDOWS or backend != "airscan":
        return False
    name = str(profile.get("name") or "").casefold()
    if "mfp-yur" not in name:
        return False
    url = profile_url(profile)
    host = urllib.parse.urlsplit(url).hostname if url else ""
    return host in {"127.0.0.1", "localhost", "::1"}


def profile_environment(
    profile: dict[str, object],
) -> dict[str, str]:
    ensure_fast_sane_config(profile)
    env = {
        **os.environ,
        "LC_ALL": "C",
        "SANE_CONFIG_DIR": str(FAST_SANE_DIR),
    }
    env.pop("SANE_AIRSCAN_DEVICE", None)
    return env


def _urlopen_scanner(
    request: urllib.request.Request,
    timeout: float,
):
    """Open a scanner URL, accepting a self-signed HTTPS certificate."""
    context = None
    if request.full_url.casefold().startswith("https://"):
        context = ssl._create_unverified_context()
    return urllib.request.urlopen(
        request,
        timeout=timeout,
        context=context,
    )


def fetch_escl_capabilities(
    url: str,
    timeout: float = 0.8,
) -> bytes:
    normalized = normalized_escl_url(url)
    if not normalized:
        return b""

    request = urllib.request.Request(
        normalized.rstrip("/") + "/ScannerCapabilities",
        headers={"User-Agent": f"NAPS3/{APP_VERSION}"},
    )
    try:
        with _urlopen_scanner(request, timeout) as response:
            data = response.read(262144)
            if int(getattr(response, "status", 200)) >= 400:
                return b""
            return data if b"ScannerCapabilities" in data else b""
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        urllib.error.HTTPError,
    ):
        return b""


def extract_escl_model(data: bytes) -> str:
    if not data:
        return ""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in {"MakeAndModel", "Model", "ScannerName"}:
            value = (element.text or "").strip()
            if value:
                return value
    return ""


def parse_escl_adf_capabilities(data: bytes) -> dict[str, object]:
    """Return the modes and resolutions advertised for the feeder only."""
    result: dict[str, object] = {
        "simplex_resolutions": [],
        "duplex_resolutions": [],
        "color_modes": [],
        "capabilities_known": False,
        "has_adf": False,
        "supports_duplex": False,
    }
    if not data:
        return result
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return result

    result["capabilities_known"] = True

    def local_name(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    def values(parent: ET.Element, name: str) -> list[str]:
        return [
            (element.text or "").strip()
            for element in parent.iter()
            if local_name(element) == name and (element.text or "").strip()
        ]

    adf = next(
        (element for element in root.iter() if local_name(element) == "Adf"),
        None,
    )
    if adf is None:
        return result

    result["has_adf"] = True

    for caps in adf:
        caps_name = local_name(caps)
        if caps_name not in {"AdfSimplexInputCaps", "AdfDuplexInputCaps"}:
            continue
        resolution_key = (
            "duplex_resolutions"
            if caps_name == "AdfDuplexInputCaps"
            else "simplex_resolutions"
        )
        if caps_name == "AdfDuplexInputCaps":
            result["supports_duplex"] = True
        resolutions: set[int] = set()
        for raw_value in values(caps, "XResolution"):
            try:
                resolutions.add(int(raw_value))
            except ValueError:
                continue
        result[resolution_key] = sorted(resolutions)
        if not result["color_modes"]:
            result["color_modes"] = list(dict.fromkeys(values(caps, "ColorMode")))

    if any(value.casefold() == "duplex" for value in values(adf, "AdfOption")):
        result["supports_duplex"] = True
    return result


def adf_profile_fields(capabilities: dict[str, object]) -> dict[str, object]:
    """Convert parsed feeder capabilities to persistent profile fields."""
    return {
        "adf_simplex_resolutions": list(
            capabilities.get("simplex_resolutions", []) or []
        ),
        "adf_duplex_resolutions": list(
            capabilities.get("duplex_resolutions", []) or []
        ),
        "adf_color_modes": list(capabilities.get("color_modes", []) or []),
        "adf_capabilities_known": bool(
            capabilities.get("capabilities_known", False)
        ),
        "adf_present": bool(capabilities.get("has_adf", False)),
        "adf_duplex_supported": bool(
            capabilities.get("supports_duplex", False)
        ),
    }


def parse_sane_source_capabilities(output: str) -> dict[str, object]:
    """Read source support and the backend's exact option names."""
    match = re.search(
        r"^\s*--source\s+(?P<values>.+?)\s+\[[^\]]*\]\s*$",
        output,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return {
            "capabilities_known": False,
            "has_adf": False,
            "supports_duplex": False,
            "source_map": {},
        }
    sources = [
        value.strip().strip('"')
        for value in match.group("values").split("|")
    ]
    source_map: dict[str, str] = {}
    for source in sources:
        normalized = source.casefold().replace("_", " ").replace("-", " ")
        is_adf = "adf" in normalized or "document feeder" in normalized
        is_duplex = is_adf and (
            "duplex" in normalized or "double sided" in normalized
        )
        if (
            "flatbed" in normalized or "platen" in normalized
        ) and "Flatbed" not in source_map:
            source_map["Flatbed"] = source
        if is_adf and not is_duplex and "ADF" not in source_map:
            source_map["ADF"] = source
        if is_duplex and "ADF Duplex" not in source_map:
            source_map["ADF Duplex"] = source

    # Some backends expose only a duplex ADF value. It can still scan one side
    # when the frontend asks for the normal feeder source.
    if "ADF" not in source_map and "ADF Duplex" in source_map:
        source_map["ADF"] = source_map["ADF Duplex"]
    return {
        "capabilities_known": True,
        "has_adf": "ADF" in source_map,
        "supports_duplex": "ADF Duplex" in source_map,
        "source_map": source_map,
    }


def sane_source_for_profile(profile: dict[str, object], source: str) -> str:
    """Translate the common UI source to the exact SANE backend value."""
    mapping = profile.get("sane_sources")
    if isinstance(mapping, dict):
        mapped = str(mapping.get(source) or "").strip()
        if mapped:
            return mapped
    return source


def network_escl_url(address: str, timeout: float = 2.5) -> str:
    """Resolve an IP, hostname, or URL to a verified eSCL base URL."""
    value = address.strip()
    if not value:
        return ""

    candidates: list[str] = []
    if "://" in value:
        candidates.append(normalized_escl_url(value))
    else:
        host = value.split("/", 1)[0].strip()
        candidates.extend(
            [
                normalized_escl_url(f"http://{host}/eSCL"),
                normalized_escl_url(f"https://{host}/eSCL"),
            ]
        )

    for candidate in candidates:
        if candidate and fetch_escl_capabilities(candidate, timeout):
            return candidate
    return ""


def profile_scan_url(profile: dict[str, object]) -> str:
    saved = profile_url(profile)
    if saved:
        return saved
    address = str(profile.get("ip") or "").strip()
    return network_escl_url(address, 1.5) if address else ""


def prepare_limited_sane_config(
    directory: Path,
    backends: list[str],
) -> dict[str, str]:
    """Load only selected SANE backends to avoid slow network discovery."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dll.conf").write_text(
        "\n".join(backends) + "\n",
        encoding="utf-8",
    )

    system_config = Path("/etc/sane.d")
    for backend in backends:
        source = system_config / f"{backend}.conf"
        target = directory / source.name
        try:
            if source.is_file():
                shutil.copy2(source, target)
            elif target.exists():
                target.unlink()
        except OSError:
            pass

    env = {**os.environ, "LC_ALL": "C", "SANE_CONFIG_DIR": str(directory)}
    env.pop("SANE_AIRSCAN_DEVICE", None)
    return env


def local_sane_backend_environment(backend: str) -> dict[str, str]:
    """Open vendor backends even when the system dll.conf omits them."""
    normalized = backend.casefold().strip()
    if normalized in {"hp", "hpaio"}:
        return prepare_limited_sane_config(USB_SANE_DIR, ["hpaio"])
    if normalized == "pixma":
        return prepare_limited_sane_config(
            USB_SANE_DIR / "pixma",
            ["pixma"],
        )
    env = {**os.environ, "LC_ALL": "C"}
    env.pop("SANE_AIRSCAN_DEVICE", None)
    return env


def list_backend_devices(
    backends: list[str],
    timeout: float,
    directory: Path,
) -> str:
    try:
        result = subprocess.run(
            ["scanimage", "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            env=prepare_limited_sane_config(directory, backends),
        )
        return safe_decode(result.stdout)
    except FileNotFoundError as exc:
        raise Naps3Error(
            "Не установлен scanimage. Установите пакет sane-backends."
        ) from exc
    except subprocess.TimeoutExpired:
        return ""


def list_system_sane_devices(timeout: float = 12.0) -> str:
    """List installed SANE devices without restricting discovery to HP."""
    env = {**os.environ, "LC_ALL": "C"}
    env.pop("SANE_AIRSCAN_DEVICE", None)
    try:
        result = subprocess.run(
            ["scanimage", "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            env=env,
        )
        return safe_decode(result.stdout)
    except FileNotFoundError as exc:
        raise Naps3Error(
            "Не установлен scanimage. Установите пакет sane-backends."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        # scanimage may already have printed local devices before a slow
        # network backend reached the timeout.
        output = safe_decode(exc.stdout or b"")
        append_scan_log(
            "SANE discovery timed out; keeping completed results: "
            f"{compact_details(output, 500)}"
        )
        return output


def parse_generic_sane_devices(output: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r"^device [`'](?P<id>[^`']+)[`'] is a (?P<description>.+)$",
        re.IGNORECASE,
    )
    devices: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = pattern.match(line)
        if not match:
            continue
        devices.append(
            {
                "id": match.group("id").strip(),
                "name": match.group("description").strip(),
                "line": line,
            }
        )
    return devices


def sane_backend_name(device_id: str) -> str:
    return device_id.partition(":")[0].strip().casefold()


def is_selectable_sane_device(device_id: str) -> bool:
    """Exclude network discovery and virtual test devices from the local list."""
    return sane_backend_name(device_id) not in {
        "airscan",
        "escl",
        "net",
        "test",
        "v4l",
    }


def build_sane_profile(
    item: dict[str, str],
    help_output: str = "",
) -> dict[str, object]:
    device_id = item.get("id", "").strip()
    backend = sane_backend_name(device_id) or "sane"
    name = item.get("name", "").strip() or "USB-сканер"
    source_capabilities = parse_sane_source_capabilities(help_output)
    is_hpaio = backend in {"hp", "hpaio"}
    return {
        "name": name,
        "url": "",
        "device_id": device_id,
        "transport": (
            "HPLIP hpaio, прямой USB"
            if is_hpaio
            else f"SANE {backend}, локальный сканер"
        ),
        "connection_kind": "usb-hpaio" if is_hpaio else "usb-sane",
        "backend": backend,
        "ip": "",
        "saved_at": int(time.time()),
        "profile_source": f"{backend}-local",
        "sane_sources": dict(source_capabilities["source_map"]),
        "adf_capabilities_known": bool(
            source_capabilities["capabilities_known"]
        ),
        "adf_present": bool(source_capabilities["has_adf"]),
        "adf_duplex_supported": bool(
            source_capabilities["supports_duplex"]
        ),
    }


def build_hpaio_profile(
    item: dict[str, str],
    help_output: str = "",
) -> dict[str, object]:
    """Compatibility wrapper for existing callers and saved-profile tests."""
    return build_sane_profile(item, help_output)


def discover_usb_scanners(
    match_filter: str = "",
) -> list[dict[str, object]]:
    """
    Find only scanners physically connected to this PC.

    Fast path: ipp-usb/eSCL on 127.0.0.1.
    Compatibility path: all locally installed SANE backends, including
    Canon pixma, Epson, Brother and HP hpaio drivers.
    """
    if IS_WINDOWS:
        try:
            return discover_wia_scanners(match_filter)
        except WindowsBackendError as exc:
            raise Naps3Error(str(exc)) from exc

    profiles: list[dict[str, object]] = []

    url = discover_loopback_escl_url(match_filter)
    if url:
        capabilities = fetch_escl_capabilities(url, 1.2)
        model = extract_escl_model(capabilities) or DEFAULT_SCANNER_NAME
        adf_capabilities = parse_escl_adf_capabilities(capabilities)
        profiles.append(
            {
                "name": model,
                "url": url,
                "device_id": "",
                "transport": "sane-airscan/eSCL через ipp-usb",
                "connection_kind": "usb",
                "backend": "airscan",
                "ip": "127.0.0.1",
                "saved_at": int(time.time()),
                "profile_source": "loopback",
                **adf_profile_fields(adf_capabilities),
            }
        )

    # ipp-usb may compete with a vendor backend for the same physical device.
    # Once a working local eSCL proxy exists, keep that already-tested path and
    # do not offer duplicate SANE entries for the same scanner.
    if not url:
        sane_output = list_system_sane_devices()
        sane_items = parse_generic_sane_devices(sane_output)

        # Vendor backends can be installed but omitted from the system
        # dll.conf. Probe the two compatibility paths explicitly.
        fallback_backends = (
            ("pixma", USB_SANE_DIR / "pixma"),
            ("hpaio", USB_SANE_DIR),
        )
        for fallback_backend, config_dir in fallback_backends:
            accepted_names = (
                {"hp", "hpaio"}
                if fallback_backend == "hpaio"
                else {"pixma"}
            )
            if any(
                sane_backend_name(item.get("id", "")) in accepted_names
                for item in sane_items
            ):
                continue
            sane_items.extend(
                parse_generic_sane_devices(
                    list_backend_devices(
                        [fallback_backend],
                        7.0,
                        config_dir,
                    )
                )
            )

        for item in sane_items:
            raw_device_id = item.get("id", "").strip()
            if not raw_device_id or not is_selectable_sane_device(raw_device_id):
                continue
            backend = sane_backend_name(raw_device_id)
            probe_env = local_sane_backend_environment(backend)
            try:
                probe = subprocess.run(
                    ["scanimage", "-d", raw_device_id, "--help"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=5,
                    check=False,
                    env=probe_env,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode != 0:
                append_scan_log(
                    "SANE device ignored: listed but cannot be "
                    f"opened: {compact_details(safe_decode(probe.stdout), 400)}"
                )
                continue
            profiles.append(
                build_sane_profile(item, safe_decode(probe.stdout))
            )

    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for profile in profiles:
        key = str(profile.get("device_id") or profile.get("url") or profile.get("name"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(profile)
    return unique


def choose_usb_profile(
    profiles: list[dict[str, object]],
    match_filter: str,
) -> Optional[dict[str, object]]:
    if not profiles:
        return None
    filter_text = match_filter.casefold().strip()
    if filter_text:
        for profile in profiles:
            haystack = " ".join(
                str(profile.get(key, ""))
                for key in ("name", "device_id", "transport")
            ).casefold()
            if filter_text in haystack:
                return profile
    if len(profiles) == 1:
        return profiles[0]
    # Multiple devices require an explicit choice in the dialog. Selecting a
    # preferred vendor or model here caused unrelated HP devices to win.
    return None


def usb_diagnostic_text() -> str:
    details: list[str] = []
    try:
        result = subprocess.run(
            ["sane-find-scanner", "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
        raw_output = safe_decode(result.stdout)
        output = compact_details(raw_output, 300)
        if output:
            canon_found = (
                "vendor=0x04a9" in raw_output.casefold()
                and "product=0x2737" in raw_output.casefold()
            )
            canon_denied = re.search(
                r"could not open USB device\s+0x04a9/0x2737[^\n]*"
                r"(?:access denied|permission)",
                raw_output,
                flags=re.IGNORECASE,
            )
            if canon_denied:
                details.append(
                    "Canon MF4410 найден, но текущему пользователю не хватает "
                    f"прав доступа: {compact_details(canon_denied.group(0), 220)}"
                )
            elif canon_found:
                details.append(
                    "Canon MF4410 виден по USB, но backend pixma не создал "
                    "SANE-устройство. Установите пакет "
                    "sane-backends-drivers-scanners и повторите поиск."
                )
            elif "found usb scanner" in raw_output.casefold():
                details.append(
                    "USB-сканер обнаружен, но SANE не создал устройство. "
                    "Проверьте права доступа, драйвер производителя и "
                    "активный backend SANE."
                )
            else:
                details.append(f"USB: {output}")
    except Exception:
        pass
    return "; ".join(details)


def discover_network_scanners() -> list[dict[str, str]]:
    # Automatic sane-airscan discovery is Linux-specific. Windows users can
    # still add any eSCL scanner by its IP address in the same dialog.
    if IS_WINDOWS:
        return []

    output = list_backend_devices(
        ["airscan"],
        12.0,
        CACHE_DIR / "sane-network-airscan",
    )

    devices: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in parse_airscan_candidates(output):
        if item.get("kind") != "network":
            continue
        key = (item.get("id", ""), item.get("ip", ""))
        if key in seen:
            continue
        seen.add(key)
        devices.append(item)
    return devices


def build_network_profile(
    name: str,
    address: str,
    device_id: str = "",
    *,
    discover_device_id: bool = True,
) -> dict[str, object]:
    url = network_escl_url(address, 3.0)
    if not url:
        raise Naps3Error(
            "По указанному адресу не найден eSCL-сканер. Проверьте IP, "
            "доступность МФУ и включение AirScan/eSCL в его веб-интерфейсе."
        )

    capabilities = fetch_escl_capabilities(url, 3.0)
    model = extract_escl_model(capabilities)
    adf_capabilities = parse_escl_adf_capabilities(capabilities)
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or address.strip()

    # Reuse the system airscan ID when available; otherwise the direct eSCL
    # engine can scan by URL and SANE remains a fallback.
    if not device_id and discover_device_id:
        for item in discover_network_scanners():
            if item.get("ip") == host:
                device_id = item.get("id", "")
                if not name:
                    name = item.get("name", "")
                break

    return {
        "name": name.strip() or model or f"Сетевой сканер {host}",
        "url": url,
        "device_id": device_id,
        "transport": "прямой eSCL + sane-airscan",
        "connection_kind": "network",
        "ip": host,
        "saved_at": int(time.time()),
        "profile_source": "network-dialog",
        **adf_profile_fields(adf_capabilities),
    }


def validate_registration_value(
    value: str,
    label: str,
    *,
    maximum_length: int,
) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise Naps3Error(f"Не указано поле «{label}».")
    if len(cleaned) > maximum_length:
        raise Naps3Error(f"Поле «{label}» слишком длинное.")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise Naps3Error(f"Поле «{label}» содержит управляющие символы.")
    return cleaned


def build_registered_sane_profile(
    device_id: str,
    name: str = "",
    connection: str = "",
) -> dict[str, object]:
    """Probe one exact SANE device and build a profile for external tools."""
    device_id = validate_registration_value(
        device_id,
        "идентификатор устройства",
        maximum_length=1024,
    )
    name = name.strip()
    if name:
        name = validate_registration_value(name, "имя", maximum_length=256)
    backend = sane_backend_name(device_id)
    if not backend:
        raise Naps3Error("Не удалось определить SANE-backend устройства.")
    if connection not in {"", "usb", "network"}:
        raise Naps3Error("Тип подключения должен быть usb или network.")

    output = ""
    for attempt in range(2):
        try:
            probe = subprocess.run(
                ["scanimage", "-d", device_id, "--help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=8,
                check=False,
                env=local_sane_backend_environment(backend),
            )
        except FileNotFoundError as exc:
            raise Naps3Error(
                "Не установлен scanimage. Установите пакет sane-backends."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            output = safe_decode(exc.stdout or b"")
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise Naps3Error(
                "Сканер не ответил за отведённое время. Проверьте подключение "
                "и повторите регистрацию."
            ) from exc

        output = safe_decode(probe.stdout)
        if probe.returncode == 0:
            profile = build_sane_profile(
                {"id": device_id, "name": name or device_id},
                output,
            )
            if connection == "network":
                profile["connection_kind"] = "network"
                profile["transport"] = f"SANE {backend}, сетевой сканер"
            profile["profile_source"] = "printer-doctor-cli"
            return profile
        if attempt == 0 and any(
            marker in output.casefold()
            for marker in (
                "device busy",
                "resource busy",
                "sane_status_device_busy",
                "status = busy",
            )
        ):
            time.sleep(1.0)
            continue
        break

    raise Naps3Error(friendly_scan_error(output))


def register_scanner_profile(
    *,
    device_id: str = "",
    address: str = "",
    name: str = "",
    connection: str = "",
) -> tuple[dict[str, object], Optional[Path]]:
    """Validate and atomically store one profile without changing other settings."""
    if bool(device_id.strip()) == bool(address.strip()):
        raise Naps3Error(
            "Укажите ровно один способ подключения: --device-id или --address."
        )
    if address.strip() and connection:
        raise Naps3Error(
            "Параметр --connection применяется только вместе с --device-id."
        )

    if address.strip():
        address = validate_registration_value(
            address,
            "адрес",
            maximum_length=2048,
        )
        clean_name = name.strip()
        if clean_name:
            clean_name = validate_registration_value(
                clean_name,
                "имя",
                maximum_length=256,
            )
        profile = build_network_profile(
            clean_name,
            address,
            discover_device_id=False,
        )
        profile["profile_source"] = "printer-doctor-cli"
    else:
        profile = build_registered_sane_profile(device_id, name, connection)

    settings = load_settings()
    backup = backup_settings_file()
    settings[PROFILE_KEY] = profile
    save_settings(settings)
    return profile, backup


def build_escl_scan_settings(
    source: str,
    mode: str,
    dpi: int,
    paper: str,
) -> bytes:
    width, height = (
        (2480, 3508) if paper == "A4" else (2550, 3300)
    )
    input_source = "Platen" if source == "Flatbed" else "Feeder"
    color_mode = {
        "Color": "RGB24",
        "Gray": "Grayscale8",
        # HP M227 advertises only RGB24 and Grayscale8 over eSCL.
        # Lineart is thresholded locally after transfer.
        "Lineart": "Grayscale8",
    }.get(mode, "RGB24")
    duplex = "true" if source == "ADF Duplex" else "false"

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
        'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">\n'
        '  <pwg:Version>2.0</pwg:Version>\n'
        '  <pwg:ScanRegions><pwg:ScanRegion>\n'
        '    <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>\n'
        '    <pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>\n'
        f'    <pwg:Width>{width}</pwg:Width><pwg:Height>{height}</pwg:Height>\n'
        '  </pwg:ScanRegion></pwg:ScanRegions>\n'
        f'  <pwg:InputSource>{input_source}</pwg:InputSource>\n'
        f'  <scan:ColorMode>{color_mode}</scan:ColorMode>\n'
        '  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>\n'
        '  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>\n'
        f'  <scan:XResolution>{dpi}</scan:XResolution>\n'
        f'  <scan:YResolution>{dpi}</scan:YResolution>\n'
        f'  <scan:Duplex>{duplex}</scan:Duplex>\n'
        '</scan:ScanSettings>'
    )
    return xml.encode("utf-8")


def create_escl_job(base_url: str, settings_xml: bytes) -> str:
    scan_jobs_url = normalized_escl_url(base_url).rstrip("/") + "/ScanJobs"
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "Accept": "*/*",
        "User-Agent": f"NAPS3/{APP_VERSION}",
        "Connection": "keep-alive",
    }
    parsed = urllib.parse.urlsplit(scan_jobs_url)
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        headers["Host"] = "localhost"
    request = urllib.request.Request(
        scan_jobs_url,
        data=settings_xml,
        headers=headers,
        method="POST",
    )
    try:
        with _urlopen_scanner(request, 20.0) as response:
            location = response.headers.get("Location", "").strip()
            if int(getattr(response, "status", 201)) not in {200, 201}:
                raise Naps3Error(
                    f"МФУ отклонило потоковое сканирование: HTTP {response.status}."
                )
    except urllib.error.HTTPError as exc:
        details = safe_decode(exc.read()) if hasattr(exc, "read") else ""
        raise Naps3Error(
            "МФУ не приняло параметры прямого eSCL-сканирования "
            f"(HTTP {exc.code}). {compact_details(details, 300)}"
        ) from exc
    if not location:
        raise Naps3Error("МФУ создало задание без адреса ScanJob.")
    if location.casefold().startswith(("http://", "https://")):
        return location
    if location.startswith("/"):
        parsed_base = urllib.parse.urlsplit(scan_jobs_url)
        origin = urllib.parse.urlunsplit(
            (parsed_base.scheme, parsed_base.netloc, "/", "", "")
        )
        return urllib.parse.urljoin(origin, location)
    base = normalized_escl_url(base_url).rstrip("/") + "/"
    return urllib.parse.urljoin(base, location)


def cancel_escl_job(job_url: str) -> bool:
    if not job_url:
        return False
    request = urllib.request.Request(
        job_url,
        headers={"User-Agent": f"NAPS3/{APP_VERSION}"},
        method="DELETE",
    )
    try:
        with _urlopen_scanner(request, 2.0):
            pass
        return True
    except urllib.error.HTTPError as exc:
        return exc.code in {404, 410}
    except Exception:
        return False


def cancel_escl_io(response: Optional[object], job_url: str) -> None:
    """Close active eSCL I/O outside the GTK thread and release the job."""
    if response is not None:
        try:
            response.close()  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass
    if job_url:
        cancel_escl_job(job_url)


def scanner_status_text(
    base_url: str,
    timeout: float = 0.45,
) -> str:
    request = urllib.request.Request(
        normalized_escl_url(base_url).rstrip("/") + "/ScannerStatus",
        headers={
            "User-Agent": f"NAPS3/{APP_VERSION}",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with _urlopen_scanner(request, timeout) as response:
            return safe_decode(response.read(131072))
    except Exception:
        return ""


def scan_job_status_text(
    job_url: str,
    timeout: float = 0.45,
) -> str:
    request = urllib.request.Request(
        job_url,
        headers={
            "Accept": "text/xml,application/xml,*/*",
            "User-Agent": f"NAPS3/{APP_VERSION}",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with _urlopen_scanner(request, timeout) as response:
            return safe_decode(response.read(131072))
    except Exception:
        return ""


def escl_job_url(base_url: str, job_uri: str) -> str:
    """Resolve a JobUri/JobUuid from ScannerStatus to an absolute URL."""
    value = job_uri.strip()
    if not value:
        return ""
    if value.casefold().startswith(("http://", "https://")):
        return value
    normalized = normalized_escl_url(base_url)
    parsed = urllib.parse.urlsplit(normalized)
    origin = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/", "", "")
    )
    if "/" not in value:
        value = normalized.rstrip("/") + "/ScanJobs/" + value
    return urllib.parse.urljoin(origin, value)


def cleanup_terminal_escl_jobs(base_url: str) -> int:
    """Release stale terminal jobs that make HP answer 409/503 to POST."""
    status_text = scanner_status_text(base_url, 1.2)
    released = 0
    for job in parse_escl_jobs(status_text):
        if not job.is_terminal:
            continue
        job_url = escl_job_url(base_url, job.uri)
        if job_url and cancel_escl_job(job_url):
            released += 1
            append_scan_log(
                "eSCL released stale job "
                f"state={job.state or 'unknown'} age={job.age} "
                f"pending={job.images_to_transfer} url={job_url}"
            )
    return released


def adf_is_empty(status_text: str) -> bool:
    # JobCompleted is deliberately not treated as an empty feeder. Some HP
    # devices keep the previous job state in ScannerStatus briefly, which used
    # to truncate the second and later scans after one page.
    return parse_escl_state(status_text)["adf_empty"]


def adf_has_documents(status_text: str) -> bool:
    return parse_escl_state(status_text)["adf_loaded"]


def scan_job_is_completed(status_text: str) -> bool:
    return parse_escl_state(status_text)["job_completed"]


def probe_escl_url(url: str, timeout: float = 0.8) -> bool:
    return bool(fetch_escl_capabilities(url, timeout))


def discover_loopback_escl_url(
    match_filter: str = "",
) -> str:
    """
    Find the current ipp-usb port on this PC.

    Only 127.0.0.1 is checked. Network scanners are not contacted.
    """
    urls = [
        f"http://127.0.0.1:{port}/eSCL"
        for port in range(60000, 60033)
    ]
    results: list[tuple[str, bytes]] = []

    # Directly after connecting a cable or starting ipp-usb the TCP listener
    # appears slightly before the printer starts answering eSCL.  A single
    # 350 ms pass therefore made the application fall back to broken hpaio on
    # this M428/M429.  The retry only runs when the fast pass found nothing;
    # closed localhost ports still fail immediately.
    for attempt, timeout in enumerate((0.45, 1.2)):
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(fetch_escl_capabilities, url, timeout): url
                for url in urls
            }
            for future in concurrent.futures.as_completed(futures):
                data = future.result()
                if data:
                    results.append((futures[future], data))
        if results:
            break
        if attempt == 0:
            time.sleep(0.35)

    if not results:
        return ""

    filter_text = match_filter.casefold().strip()
    if filter_text:
        for url, data in results:
            if filter_text in safe_decode(data).casefold():
                return normalized_escl_url(url)

    return normalized_escl_url(sorted(results, key=lambda item: item[0])[0][0])


def default_profile_if_available(
    match_filter: str = "",
) -> Optional[dict[str, object]]:
    if IS_WINDOWS:
        return None

    url = discover_loopback_escl_url(match_filter)
    if not url:
        return None

    capabilities = fetch_escl_capabilities(url, 1.2)
    adf_capabilities = parse_escl_adf_capabilities(capabilities)

    return {
        "name": extract_escl_model(capabilities) or DEFAULT_SCANNER_NAME,
        "url": url,
        "device_id": "",
        "transport": "sane-airscan/eSCL через ipp-usb",
        "connection_kind": "usb",
        "ip": "127.0.0.1",
        "saved_at": int(time.time()),
        "profile_source": "loopback",
        **adf_profile_fields(adf_capabilities),
    }


def parse_airscan_candidates(
    output: str,
) -> list[dict[str, str]]:
    """
    Parse local USB and network sane-airscan devices.

    The application still prefers a physical USB connection, but it may use
    an explicitly matching network scanner when USB is unavailable.
    """
    candidates: list[dict[str, str]] = []
    pattern = re.compile(
        r"^device `(?P<id>airscan:[^']+)'(?P<rest>.*)$",
        re.IGNORECASE,
    )
    ip_pattern = re.compile(
        r"\bip=(?P<ip>[0-9a-fA-F:.]+)",
        re.IGNORECASE,
    )

    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = pattern.search(line)
        if not match:
            continue

        rest = match.group("rest")
        ip_match = ip_pattern.search(rest)
        ip_address = ip_match.group("ip") if ip_match else ""
        is_local = (
            ip_address in {"127.0.0.1", "::1"}
            or "(USB)" in rest.upper()
        )

        device_id = match.group("id")
        parts = device_id.split(":", 2)
        name = parts[2].strip() if len(parts) == 3 else DEFAULT_SCANNER_NAME

        candidates.append(
            {
                "id": device_id,
                "name": name,
                "line": line,
                "ip": ip_address,
                "kind": "usb" if is_local else "network",
            }
        )

    return candidates


def choose_airscan_candidate(
    candidates: list[dict[str, str]],
    match_filter: str,
) -> Optional[dict[str, str]]:
    if not candidates:
        return None

    local_candidates = [
        item for item in candidates if item.get("kind") == "usb"
    ]
    network_candidates = [
        item for item in candidates if item.get("kind") == "network"
    ]
    filter_text = match_filter.casefold().strip()

    def filter_match(
        items: list[dict[str, str]],
    ) -> Optional[dict[str, str]]:
        if not filter_text:
            return None
        for item in items:
            haystack = (item["line"] + " " + item["name"]).casefold()
            if filter_text in haystack:
                return item
        return None

    # Prefer a matching physical USB device.
    selected = filter_match(local_candidates)
    if selected:
        return selected

    if len(local_candidates) == 1:
        return local_candidates[0]

    selected = filter_match(network_candidates)
    if selected:
        return selected

    if not local_candidates and len(network_candidates) == 1:
        return network_candidates[0]

    # Several devices always require a user choice.
    return None


def parse_local_escl_urls(
    output: str,
    match_filter: str,
) -> list[str]:
    pattern = re.compile(
        r"escl:(?P<url>https?://"
        r"(?:localhost|127\.0\.0\.1|\[?::1\]?):\d+(?:/[^'`\s]*)?)",
        re.IGNORECASE,
    )
    preferred: list[str] = []
    other: list[str] = []
    filter_text = match_filter.casefold().strip()

    for line in output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        url = normalized_escl_url(match.group("url"))
        if not url:
            continue
        target = (
            preferred
            if filter_text and filter_text in line.casefold()
            else other
        )
        if url not in target:
            target.append(url)

    return preferred + [url for url in other if url not in preferred]


def parse_full_discovery_profile(
    output: str,
    match_filter: str,
) -> Optional[dict[str, object]]:
    airscan = choose_airscan_candidate(
        parse_airscan_candidates(output),
        match_filter,
    )

    verified_url = ""
    for url in parse_local_escl_urls(output, match_filter):
        if probe_escl_url(url):
            verified_url = url
            break

    if not verified_url:
        verified_url = discover_loopback_escl_url(match_filter)

    if airscan:
        is_usb = airscan.get("kind") == "usb"
        network_url = (
            network_escl_url(airscan.get("ip", ""), 1.5)
            if not is_usb and airscan.get("ip")
            else ""
        )
        selected_url = verified_url if is_usb else network_url
        capabilities = (
            fetch_escl_capabilities(selected_url, 1.2)
            if selected_url
            else b""
        )
        adf_capabilities = parse_escl_adf_capabilities(capabilities)
        return {
            "name": airscan["name"] or DEFAULT_SCANNER_NAME,
            "url": verified_url if is_usb else network_url,
            "device_id": airscan["id"],
            "transport": (
                "sane-airscan/eSCL через ipp-usb"
                if is_usb and verified_url
                else (
                    "sane-airscan, локальный USB"
                    if is_usb
                    else "прямой eSCL + sane-airscan"
                )
            ),
            "connection_kind": "usb" if is_usb else "network",
            "ip": airscan.get("ip", ""),
            "saved_at": int(time.time()),
            "profile_source": "airscan",
            **adf_profile_fields(adf_capabilities),
        }

    if verified_url:
        capabilities = fetch_escl_capabilities(verified_url, 1.2)
        adf_capabilities = parse_escl_adf_capabilities(capabilities)
        return {
            "name": DEFAULT_SCANNER_NAME,
            "url": verified_url,
            "device_id": "",
            "transport": "sane-airscan/eSCL через ipp-usb",
            "connection_kind": "usb",
            "ip": "127.0.0.1",
            "saved_at": int(time.time()),
            "profile_source": "loopback",
            **adf_profile_fields(adf_capabilities),
        }

    return None


def discover_scanner_profile(
    match_filter: str = "",
) -> dict[str, object]:
    """Find a physical USB scanner first, then a matching network device."""
    usb_profiles = discover_usb_scanners(match_filter)
    selected_usb = choose_usb_profile(usb_profiles, match_filter)
    if selected_usb:
        return selected_usb
    if usb_profiles:
        raise Naps3Error(
            "Найдено несколько локальных сканеров. Нажмите «Подключить "
            "сканер…» и выберите нужное устройство."
        )

    network_devices = discover_network_scanners()
    selected_network = choose_airscan_candidate(
        network_devices,
        match_filter,
    )
    if selected_network:
        address = selected_network.get("ip", "")
        try:
            return build_network_profile(
                selected_network.get("name", ""),
                address,
                selected_network.get("id", ""),
            )
        except Naps3Error:
            return {
                "name": selected_network.get("name") or "Сетевой сканер",
                "url": "",
                "device_id": selected_network.get("id", ""),
                "transport": "sane-airscan, сеть",
                "connection_kind": "network",
                "backend": "airscan",
                "ip": address,
                "saved_at": int(time.time()),
                "profile_source": "network-airscan",
            }

    if IS_WINDOWS:
        raise Naps3Error(
            "Сканер Windows не найден. Убедитесь, что МФУ включено и "
            "подключено, затем установите WIA-драйвер производителя."
        )

    diagnostic = usb_diagnostic_text()
    message = (
        "Локальный сканер не найден. Проверьте USB-кабель, питание МФУ "
        "и наличие подходящего SANE-драйвера производителя."
    )
    if diagnostic:
        message += f"\n\nДиагностика: {diagnostic}"
    raise Naps3Error(message)


def resolve_profile_device(
    profile: dict[str, object],
) -> tuple[str, dict[str, str], dict[str, object]]:
    if not profile_url(profile):
        raise Naps3Error(
            "В сохранённом профиле нет eSCL-адреса. "
            "Выберите нужный сканер заново."
        )

    env = profile_environment(profile)
    try:
        result = subprocess.run(
            ["scanimage", "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=6,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise Naps3Error(
            "Сохранённый сканер не ответил вовремя. "
            "Проверьте его готовность и подключение."
        ) from exc

    output = safe_decode(result.stdout)
    pattern = re.compile(
        r"^device `(?P<id>airscan:[^']+)'",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        updated = dict(profile)
        updated["device_id"] = match.group("id")
        updated["saved_at"] = int(time.time())
        return match.group("id"), env, updated

    raise Naps3Error(
        "Сохранённый локальный адрес больше не создаёт устройство "
        "sane-airscan. Проверьте подключение и при необходимости "
        "выберите этот сканер заново."
    )


def icon_image(icon_name: str, size: Gtk.IconSize = Gtk.IconSize.BUTTON) -> Gtk.Image:
    return Gtk.Image.new_from_icon_name(icon_name, size)


def toolbar_button(
    label: str,
    icon_name: str,
    tooltip: str,
    callback,
) -> Gtk.Button:
    button = Gtk.Button()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    box.pack_start(icon_image(icon_name), False, False, 0)
    box.pack_start(Gtk.Label(label=label), False, False, 0)
    button.add(box)
    button.set_tooltip_text(tooltip)
    button.connect("clicked", callback)
    return button


class ExportDialog(Gtk.Dialog):
    MODES = (
        ("pdf", "Все страницы одним PDF-файлом"),
        ("tiff_multi", "Все страницы одним многостраничным TIFF"),
        ("selected_image", "Текущая страница как одно изображение"),
        ("images_all", "Все страницы отдельными изображениями"),
    )

    def __init__(
        self,
        parent: Gtk.Window,
        page_count: int,
        has_selection: bool,
    ) -> None:
        super().__init__(
            title="Сохранение сканов",
            transient_for=parent,
            modal=True,
        )
        self.set_default_size(520, 360)
        self.add_button("Отмена", Gtk.ResponseType.CANCEL)
        self.add_button("Продолжить", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_spacing(12)
        content.set_border_width(18)

        title = Gtk.Label()
        title.set_markup("<b>Выберите способ сохранения</b>")
        title.set_xalign(0)
        content.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(
            label=f"В проекте страниц: {page_count}"
        )
        subtitle.set_xalign(0)
        subtitle.get_style_context().add_class("dim-label")
        content.pack_start(subtitle, False, False, 0)

        mode_frame = Gtk.Frame()
        mode_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin=12,
        )
        mode_frame.add(mode_box)
        content.pack_start(mode_frame, False, False, 0)

        self.mode_buttons: dict[str, Gtk.RadioButton] = {}
        group: Optional[Gtk.RadioButton] = None
        for mode_id, label in self.MODES:
            button = Gtk.RadioButton.new_with_label_from_widget(group, label)
            if group is None:
                group = button
            if mode_id == "selected_image" and not has_selection:
                button.set_sensitive(False)
            button.connect("toggled", self._update_controls)
            self.mode_buttons[mode_id] = button
            mode_box.pack_start(button, False, False, 0)

        self.mode_buttons["pdf"].set_active(True)

        options_grid = Gtk.Grid(
            column_spacing=12,
            row_spacing=10,
        )
        content.pack_start(options_grid, False, False, 0)

        format_label = Gtk.Label(label="Формат файла")
        format_label.set_xalign(0)
        options_grid.attach(format_label, 0, 0, 1, 1)

        self.format_combo = Gtk.ComboBoxText()
        for name in FORMAT_INFO:
            self.format_combo.append(name, name)
        self.format_combo.set_active_id(DEFAULT_EXPORT_FORMAT)
        self.format_combo.connect("changed", self._update_controls)
        options_grid.attach(self.format_combo, 1, 0, 1, 1)

        quality_label = Gtk.Label(label="Качество")
        quality_label.set_xalign(0)
        options_grid.attach(quality_label, 0, 1, 1, 1)

        self.quality_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL,
            40,
            100,
            1,
        )
        self.quality_scale.set_value(92)
        self.quality_scale.set_hexpand(True)
        self.quality_scale.set_draw_value(True)
        options_grid.attach(self.quality_scale, 1, 1, 1, 1)

        self.show_all()
        self._update_controls()

    def selected_mode(self) -> str:
        for mode_id, button in self.mode_buttons.items():
            if button.get_active():
                return mode_id
        return "pdf"

    def get_result(self) -> dict[str, object]:
        return {
            "mode": self.selected_mode(),
            "format": self.format_combo.get_active_id() or DEFAULT_EXPORT_FORMAT,
            "quality": int(self.quality_scale.get_value()),
        }

    def _update_controls(self, *_args: object) -> None:
        mode = self.selected_mode()
        image_mode = mode in {"selected_image", "images_all"}
        self.format_combo.set_sensitive(image_mode)
        image_format = (
            self.format_combo.get_active_id() or DEFAULT_EXPORT_FORMAT
        )
        self.quality_scale.set_sensitive(
            image_mode and image_format in {"JPEG", "WEBP"}
        )


class USBScannerDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        profiles: list[dict[str, object]],
    ) -> None:
        super().__init__(
            title=(
                "Подключение сканера Windows"
                if IS_WINDOWS
                else "Подключение локального сканера"
            ),
            transient_for=parent,
            modal=True,
        )
        self.profiles = profiles
        self.set_default_size(560, 270)
        self.add_button("Отмена", Gtk.ResponseType.CANCEL)
        self.add_button("Подключить", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        area = self.get_content_area()
        area.set_spacing(12)
        area.set_border_width(18)

        title = Gtk.Label()
        title.set_markup(
            "<b>Выберите сканер Windows</b>"
            if IS_WINDOWS
            else "<b>Выберите локальный сканер</b>"
        )
        title.set_xalign(0)
        area.pack_start(title, False, False, 0)

        info = Gtk.Label(
            label=(
                "NAPS3 показывает сканеры, установленные в Windows через "
                "системный интерфейс WIA."
                if IS_WINDOWS
                else (
                    "NAPS3 проверяет driverless ipp-usb/eSCL и установленные "
                    "локальные SANE-драйверы, включая pixma и hpaio."
                )
            )
        )
        info.set_xalign(0)
        info.set_line_wrap(True)
        info.get_style_context().add_class("dim-label")
        area.pack_start(info, False, False, 0)

        self.combo = Gtk.ComboBoxText()
        for index, profile in enumerate(profiles):
            backend = (
                "Windows WIA"
                if profile.get("connection_kind") == "windows-wia"
                else (
                    "HPLIP / hpaio"
                    if profile.get("connection_kind") == "usb-hpaio"
                    else (
                        f"SANE / {profile.get('backend') or 'локальный'}"
                        if profile.get("connection_kind") == "usb-sane"
                        else "ipp-usb / eSCL"
                    )
                )
            )
            self.combo.append(
                str(index),
                f"{profile_name(profile)} — {backend}",
            )
        if profiles:
            self.combo.set_active(0)
        area.pack_start(self.combo, False, False, 0)

        note = Gtk.Label(
            label=(
                "Если устройства нет в списке, установите WIA-драйвер с сайта "
                "производителя МФУ и переподключите USB-кабель."
                if IS_WINDOWS
                else (
                    "Если списка нет, установите пакет SANE-драйвера вашего "
                    "сканера и переподключите USB-кабель."
                )
            )
        )
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        area.pack_start(note, False, False, 0)
        self.show_all()

    def get_profile(self) -> Optional[dict[str, object]]:
        active = self.combo.get_active_id()
        if active is None:
            return None
        try:
            return dict(self.profiles[int(active)])
        except (ValueError, IndexError):
            return None


class NetworkScannerDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        devices: list[dict[str, str]],
    ) -> None:
        super().__init__(
            title="Подключение сканера по сети",
            transient_for=parent,
            modal=True,
        )
        self.devices = devices
        self.set_default_size(540, 330)
        self.add_button("Отмена", Gtk.ResponseType.CANCEL)
        self.add_button("Подключить", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        area = self.get_content_area()
        area.set_spacing(12)
        area.set_border_width(18)

        title = Gtk.Label()
        title.set_markup("<b>Выберите сетевое МФУ или укажите его IP</b>")
        title.set_xalign(0)
        area.pack_start(title, False, False, 0)

        description = Gtk.Label(
            label=(
                "Подключение сохраняется в профиле. Для потокового АПД "
                "NAPS3 обращается к eSCL напрямую."
            )
        )
        description.set_xalign(0)
        description.set_line_wrap(True)
        description.get_style_context().add_class("dim-label")
        area.pack_start(description, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        area.pack_start(grid, False, False, 0)

        device_label = Gtk.Label(label="Найденные устройства")
        device_label.set_xalign(0)
        grid.attach(device_label, 0, 0, 1, 1)

        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.append("manual", "Указать адрес вручную")
        for index, item in enumerate(devices):
            name = item.get("name") or "Сетевой сканер"
            ip = item.get("ip") or "адрес не указан"
            self.device_combo.append(str(index), f"{name} — {ip}")
        self.device_combo.set_active(1 if devices else 0)
        self.device_combo.connect("changed", self._device_changed)
        grid.attach(self.device_combo, 1, 0, 1, 1)

        ip_label = Gtk.Label(label="IP, имя хоста или URL")
        ip_label.set_xalign(0)
        grid.attach(ip_label, 0, 1, 1, 1)

        self.address_entry = Gtk.Entry()
        self.address_entry.set_placeholder_text("Например: 172.27.5.67")
        self.address_entry.set_activates_default(True)
        grid.attach(self.address_entry, 1, 1, 1, 1)

        name_label = Gtk.Label(label="Название профиля")
        name_label.set_xalign(0)
        grid.attach(name_label, 0, 2, 1, 1)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_placeholder_text("Заполнится автоматически")
        grid.attach(self.name_entry, 1, 2, 1, 1)

        note = Gtk.Label(
            label=(
                "Можно выбрать любое eSCL-совместимое МФУ, а не только M428f. "
                "Сетевой адрес должен быть доступен с этого компьютера."
            )
        )
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        area.pack_start(note, False, False, 0)

        self._device_changed()
        self.show_all()

    def _device_changed(self, *_args: object) -> None:
        active_id = self.device_combo.get_active_id()
        if active_id is None or active_id == "manual":
            return
        try:
            item = self.devices[int(active_id)]
        except (ValueError, IndexError):
            return
        self.address_entry.set_text(item.get("ip", ""))
        self.name_entry.set_text(item.get("name", ""))

    def get_selection(self) -> tuple[str, str, str]:
        active_id = self.device_combo.get_active_id()
        device_id = ""
        if active_id and active_id != "manual":
            try:
                device_id = self.devices[int(active_id)].get("id", "")
            except (ValueError, IndexError):
                pass
        return (
            self.name_entry.get_text().strip(),
            self.address_entry.get_text().strip(),
            device_id,
        )


class PageRow(Gtk.ListBoxRow):
    def __init__(self, page: Page, index: int) -> None:
        super().__init__()
        self.page = page
        self.index = index

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=7,
            margin=8,
        )
        self.add(outer)

        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(page.path),
                180,
                220,
                True,
            )
            image = Gtk.Image.new_from_pixbuf(pixbuf)
        except (GLib.Error, OSError):
            image = icon_image("image-missing-symbolic", Gtk.IconSize.DIALOG)

        image.set_halign(Gtk.Align.CENTER)
        outer.pack_start(image, False, False, 0)

        label = Gtk.Label(label=f"Страница {index + 1}")
        label.set_xalign(0.5)
        label.get_style_context().add_class("page-title")
        outer.pack_start(label, False, False, 0)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=APP_NAME)

        self.set_default_size(1280, 800)
        self.set_size_request(980, 640)
        windows_icon = resource_path("naps3.png")
        if IS_WINDOWS and windows_icon.is_file():
            self.set_icon_from_file(str(windows_icon))
        else:
            self.set_icon_name("naps3")

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.session_dir = CACHE_DIR / f"session-{time.strftime('%Y%m%d-%H%M%S')}"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.pages: list[Page] = []
        self.document_revision = 0
        self.saved_revision = 0
        self.is_exporting = False
        self.selected_index: Optional[int] = None
        self.current_process: Optional[subprocess.Popen] = None
        self.current_escl_job_url: Optional[str] = None
        self.current_escl_response: Optional[object] = None
        self.is_busy = False
        self.cancel_requested = False
        self._profile_probe_token: Optional[object] = None
        self.scan_sequence = 0
        self._preview_failures: set[Path] = set()
        self.settings = load_settings()
        stored_profile = self.settings.get(PROFILE_KEY)
        self.scanner_profile: Optional[dict[str, object]] = (
            dict(stored_profile)
            if isinstance(stored_profile, dict)
            else None
        )
        self.busy_widgets: list[Gtk.Widget] = []

        self._build_ui()
        self._apply_css()
        self._load_ui_settings()
        self._update_page_actions()
        self.initialize_scanner_profile()

    # ------------------------------ UI ------------------------------

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = APP_NAME
        header.props.subtitle = "Сканирование документов"
        self.set_titlebar(header)

        self.scan_button = toolbar_button(
            "Сканировать",
            "document-new-symbolic",
            "Сканировать документы (Ctrl+P)",
            lambda _button: self.start_scan(),
        )
        self.scan_button.get_style_context().add_class("suggested-action")
        header.pack_start(self.scan_button)

        self.cancel_button = toolbar_button(
            "Остановить",
            "process-stop-symbolic",
            "Остановить текущую операцию",
            lambda _button: self.cancel_operation(),
        )
        self.cancel_button.get_style_context().add_class("destructive-action")
        self.cancel_button.set_no_show_all(True)
        header.pack_start(self.cancel_button)

        self.save_button = toolbar_button(
            "Сохранить",
            "document-save-symbolic",
            "Сохранить сканы (Ctrl+S)",
            lambda _button: self.export_pages(),
        )
        header.pack_end(self.save_button)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(main_box)

        toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin=10,
        )
        main_box.pack_start(toolbar, False, False, 0)

        actions = (
            (
                "Изображения",
                "image-x-generic-symbolic",
                "Импортировать изображения (Ctrl+O)",
                lambda _button: self.import_images(),
            ),
            (
                "PDF",
                "application-pdf-symbolic",
                "Импортировать PDF",
                lambda _button: self.import_pdf(),
            ),
            (
                "Влево",
                "object-rotate-left-symbolic",
                "Повернуть выбранную страницу влево",
                lambda _button: self.rotate_selected(-90),
            ),
            (
                "Вправо",
                "object-rotate-right-symbolic",
                "Повернуть выбранную страницу вправо",
                lambda _button: self.rotate_selected(90),
            ),
            (
                "Выше",
                "go-up-symbolic",
                "Переместить выбранную страницу выше",
                lambda _button: self.move_selected(-1),
            ),
            (
                "Ниже",
                "go-down-symbolic",
                "Переместить выбранную страницу ниже",
                lambda _button: self.move_selected(1),
            ),
            (
                "Удалить",
                "edit-delete-symbolic",
                "Удалить выбранную страницу",
                lambda _button: self.delete_selected(),
            ),
            (
                "Очистить",
                "edit-clear-all-symbolic",
                "Удалить все страницы из проекта",
                lambda _button: self.clear_pages(),
            ),
        )

        for label, icon, tooltip, callback in actions:
            button = toolbar_button(label, icon, tooltip, callback)
            toolbar.pack_start(button, False, False, 0)
            self.busy_widgets.append(button)

        toolbar.pack_start(Gtk.Separator(), False, False, 2)

        zoom_out = Gtk.Button.new_from_icon_name(
            "zoom-out-symbolic",
            Gtk.IconSize.BUTTON,
        )
        zoom_out.set_tooltip_text("Уменьшить предпросмотр")
        zoom_out.connect("clicked", lambda _button: self.change_zoom(-0.1))
        toolbar.pack_end(zoom_out, False, False, 0)

        zoom_in = Gtk.Button.new_from_icon_name(
            "zoom-in-symbolic",
            Gtk.IconSize.BUTTON,
        )
        zoom_in.set_tooltip_text("Увеличить предпросмотр")
        zoom_in.connect("clicked", lambda _button: self.change_zoom(0.1))
        toolbar.pack_end(zoom_in, False, False, 0)

        self.zoom_label = Gtk.Label(label="По размеру окна")
        self.zoom_label.get_style_context().add_class("dim-label")
        toolbar.pack_end(self.zoom_label, False, False, 4)

        self.settings_toggle = Gtk.ToggleButton()
        settings_toggle_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        settings_toggle_box.pack_start(
            icon_image("preferences-system-symbolic"),
            False,
            False,
            0,
        )
        settings_toggle_box.pack_start(
            Gtk.Label(label="Параметры"),
            False,
            False,
            0,
        )
        self.settings_toggle.add(settings_toggle_box)
        self.settings_toggle.set_tooltip_text(
            "Показать или скрыть параметры сканирования"
        )
        self.settings_toggle.set_active(True)
        self.settings_toggle.connect(
            "toggled",
            self._on_settings_toggled,
        )
        toolbar.pack_end(self.settings_toggle, False, False, 8)

        main_box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            0,
        )

        outer_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer_paned.set_position(220)
        main_box.pack_start(outer_paned, True, True, 0)

        # Левая панель: страницы.
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.get_style_context().add_class("sidebar")
        sidebar.set_size_request(190, -1)
        outer_paned.pack1(sidebar, resize=False, shrink=False)

        sidebar_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin=12,
        )
        sidebar.pack_start(sidebar_header, False, False, 0)

        pages_title = Gtk.Label()
        pages_title.set_markup("<b>Страницы</b>")
        pages_title.set_xalign(0)
        sidebar_header.pack_start(pages_title, True, True, 0)

        self.page_count_label = Gtk.Label(label="0")
        self.page_count_label.get_style_context().add_class("badge")
        sidebar_header.pack_end(self.page_count_label, False, False, 0)

        sidebar.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            0,
        )

        page_scrolled = Gtk.ScrolledWindow()
        page_scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        sidebar.pack_start(page_scrolled, True, True, 0)

        self.page_list = Gtk.ListBox()
        self.page_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.page_list.set_activate_on_single_click(True)
        self.page_list.connect("row-selected", self._on_page_selected)
        page_scrolled.add(self.page_list)

        # Центр + правая панель.
        # Обычный Gtk.Box используется намеренно: широкий separator у
        # вложенного Gtk.Paned в некоторых темах РЕД ОС выглядел как
        # чёрная полоса.
        content_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        outer_paned.pack2(content_box, resize=True, shrink=False)

        preview_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        preview_box.get_style_context().add_class("preview-shell")
        content_box.pack_start(preview_box, True, True, 0)

        preview_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin=12,
        )
        preview_box.pack_start(preview_header, False, False, 0)

        self.preview_title = Gtk.Label()
        self.preview_title.set_markup("<b>Предпросмотр</b>")
        self.preview_title.set_xalign(0)
        preview_header.pack_start(self.preview_title, True, True, 0)

        self.preview_info = Gtk.Label(label="")
        self.preview_info.get_style_context().add_class("dim-label")
        preview_header.pack_end(self.preview_info, False, False, 0)

        preview_box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            0,
        )

        self.preview_overlay = Gtk.Overlay()
        preview_box.pack_start(self.preview_overlay, True, True, 0)

        self.preview_scrolled = Gtk.ScrolledWindow()
        self.preview_scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        self.preview_overlay.add(self.preview_scrolled)

        self.preview_event = Gtk.EventBox()
        self.preview_event.get_style_context().add_class("preview-area")
        # Gtk 3 creates an internal viewport automatically for non-scrollable
        # children; add_with_viewport is deprecated and prints at every start.
        self.preview_scrolled.add(self.preview_event)

        self.preview_image = Gtk.Image()
        self.preview_image.set_halign(Gtk.Align.CENTER)
        self.preview_image.set_valign(Gtk.Align.CENTER)
        self.preview_event.add(self.preview_image)

        self.empty_preview = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        self.empty_preview.set_halign(Gtk.Align.CENTER)
        self.empty_preview.set_valign(Gtk.Align.CENTER)

        empty_icon = icon_image(
            "document-new-symbolic",
            Gtk.IconSize.DIALOG,
        )
        empty_icon.set_opacity(0.55)
        self.empty_preview.pack_start(empty_icon, False, False, 0)

        empty_label = Gtk.Label(
            label="Отсканированные страницы появятся здесь"
        )
        empty_label.get_style_context().add_class("empty-title")
        self.empty_preview.pack_start(empty_label, False, False, 0)

        empty_hint = Gtk.Label(
            label="Положите документы в автоподатчик и нажмите «Сканировать»"
        )
        empty_hint.get_style_context().add_class("dim-label")
        self.empty_preview.pack_start(empty_hint, False, False, 0)

        self.preview_overlay.add_overlay(self.empty_preview)
        self.preview_event.connect("size-allocate", self._on_preview_size_allocate)

        # Правая панель настроек.
        # Она фиксированной ширины, прижата к правой границе окна и может
        # полностью скрываться, освобождая место предпросмотру.
        self.settings_revealer = Gtk.Revealer()
        self.settings_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_LEFT
        )
        self.settings_revealer.set_transition_duration(180)
        self.settings_revealer.set_reveal_child(True)
        self.settings_revealer.set_halign(Gtk.Align.END)
        content_box.pack_end(
            self.settings_revealer,
            False,
            False,
            0,
        )

        settings_panel_container = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
        )
        settings_panel_container.set_size_request(314, -1)
        settings_panel_container.set_hexpand(False)
        settings_panel_container.set_halign(Gtk.Align.END)
        settings_panel_container.get_style_context().add_class(
            "settings-container"
        )
        self.settings_revealer.add(settings_panel_container)

        content_separator = Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL,
        )
        content_separator.set_size_request(1, -1)
        content_separator.get_style_context().add_class(
            "content-separator"
        )
        settings_panel_container.pack_start(
            content_separator,
            False,
            False,
            0,
        )

        settings_scrolled = Gtk.ScrolledWindow()
        settings_scrolled.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        settings_scrolled.set_size_request(313, -1)
        settings_scrolled.set_hexpand(False)
        settings_scrolled.set_vexpand(True)
        settings_scrolled.set_shadow_type(Gtk.ShadowType.NONE)
        settings_scrolled.set_overlay_scrolling(False)
        settings_scrolled.get_style_context().add_class("settings-shell")
        settings_panel_container.pack_start(
            settings_scrolled,
            False,
            True,
            0,
        )

        settings_viewport = Gtk.Viewport()
        settings_viewport.set_shadow_type(Gtk.ShadowType.NONE)
        settings_viewport.set_hexpand(False)
        settings_viewport.set_vexpand(True)
        settings_viewport.get_style_context().add_class("settings-viewport")
        settings_scrolled.add(settings_viewport)

        settings_surface = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
        )
        settings_surface.set_size_request(313, -1)
        settings_surface.set_hexpand(False)
        settings_surface.set_vexpand(True)
        settings_surface.get_style_context().add_class("settings-surface")
        settings_viewport.add(settings_surface)

        settings_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        settings_box.set_margin_top(12)
        settings_box.set_margin_bottom(12)
        settings_box.set_margin_start(12)
        settings_box.set_margin_end(12)
        settings_box.set_size_request(289, -1)
        settings_box.set_hexpand(False)
        settings_box.set_vexpand(True)
        settings_box.get_style_context().add_class("settings-panel")
        settings_surface.pack_start(settings_box, True, True, 0)

        settings_title = Gtk.Label()
        settings_title.set_markup("<b>Параметры сканирования</b>")
        settings_title.set_xalign(0)
        settings_box.pack_start(settings_title, False, False, 0)

        self.match_entry = Gtk.Entry()
        self._add_labeled_widget(
            settings_box,
            "Фильтр устройства",
            self.match_entry,
        )

        self.source_combo = Gtk.ComboBoxText()
        self.source_combo.append("ADF", "Автоподатчик")
        self.source_combo.append("ADF Duplex", "Автоподатчик, две стороны")
        self.source_combo.append("Flatbed", "Стекло")
        self._add_labeled_widget(
            settings_box,
            "Источник",
            self.source_combo,
        )

        self.mode_combo = Gtk.ComboBoxText()
        self.mode_combo.append("Color", "Цветной")
        self.mode_combo.append("Gray", "Оттенки серого")
        self.mode_combo.append("Lineart", "Чёрно-белый")
        self._add_labeled_widget(
            settings_box,
            "Режим",
            self.mode_combo,
        )

        self.dpi_combo = Gtk.ComboBoxText()
        for dpi in ("150", "200", "300", "600"):
            self.dpi_combo.append(dpi, f"{dpi} dpi")
        self._add_labeled_widget(
            settings_box,
            "Разрешение",
            self.dpi_combo,
        )

        self.paper_combo = Gtk.ComboBoxText()
        self.paper_combo.append("A4", "A4")
        self.paper_combo.append("Letter", "US Letter")
        self._add_labeled_widget(
            settings_box,
            "Формат бумаги",
            self.paper_combo,
        )

        self.stream_adf_check = Gtk.CheckButton(
            label="Ускоренный режим АПД"
        )
        self.stream_adf_check.set_tooltip_text(
            "Для USB используется надёжный пакетный SANE-режим без "
            "межстраничного PNG-сжатия. Для сетевых eSCL-сканеров может "
            "использоваться прямой потоковый режим. Неподдерживаемое "
            "разрешение АПД автоматически понижается до максимального."
        )
        self.stream_adf_check.set_active(True)
        settings_box.pack_start(
            self.stream_adf_check,
            False,
            False,
            2,
        )

        settings_box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            6,
        )

        device_title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        settings_box.pack_start(device_title_row, False, False, 2)

        device_title = Gtk.Label()
        device_title.set_markup("<b>Рабочее устройство</b>")
        device_title.set_xalign(0)
        device_title_row.pack_start(device_title, True, True, 0)

        self.device_status_badge = Gtk.Label(label="Проверка")
        self.device_status_badge.get_style_context().add_class(
            "status-badge"
        )
        self.device_status_badge.get_style_context().add_class(
            "status-checking"
        )
        device_title_row.pack_end(
            self.device_status_badge,
            False,
            False,
            0,
        )

        self.device_card = Gtk.EventBox()
        self.device_card.get_style_context().add_class("device-card")
        settings_box.pack_start(self.device_card, False, False, 2)

        device_card_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=11,
        )
        device_card_box.set_border_width(13)
        self.device_card.add(device_card_box)

        device_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=11,
        )
        device_card_box.pack_start(device_header, False, False, 0)

        device_icon_box = Gtk.EventBox()
        device_icon_box.get_style_context().add_class("device-icon-box")
        device_header.pack_start(device_icon_box, False, False, 0)

        self.device_icon = icon_image(
            "printer-symbolic",
            Gtk.IconSize.DIALOG,
        )
        self.device_icon.set_margin_top(8)
        self.device_icon.set_margin_bottom(8)
        self.device_icon.set_margin_start(9)
        self.device_icon.set_margin_end(9)
        device_icon_box.add(self.device_icon)

        device_identity = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=3,
        )
        device_header.pack_start(device_identity, True, True, 0)

        self.device_name_label = Gtk.Label(label="Сканер не выбран")
        self.device_name_label.set_xalign(0)
        self.device_name_label.set_line_wrap(True)
        self.device_name_label.set_max_width_chars(24)
        self.device_name_label.get_style_context().add_class(
            "device-name"
        )
        device_identity.pack_start(
            self.device_name_label,
            False,
            False,
            0,
        )

        self.device_model_label = Gtk.Label(label="USB или сеть")
        self.device_model_label.set_xalign(0)
        self.device_model_label.set_line_wrap(True)
        self.device_model_label.get_style_context().add_class(
            "device-subtitle"
        )
        device_identity.pack_start(
            self.device_model_label,
            False,
            False,
            0,
        )

        device_card_box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False,
            False,
            0,
        )

        device_grid = Gtk.Grid(
            column_spacing=9,
            row_spacing=7,
        )
        device_card_box.pack_start(device_grid, False, False, 0)

        connection_icon = icon_image(
            "network-wired-symbolic",
            Gtk.IconSize.MENU,
        )
        connection_icon.set_valign(Gtk.Align.CENTER)
        device_grid.attach(connection_icon, 0, 0, 1, 1)

        connection_caption = Gtk.Label(label="Подключение")
        connection_caption.set_xalign(0)
        connection_caption.get_style_context().add_class(
            "device-detail-caption"
        )
        device_grid.attach(connection_caption, 1, 0, 1, 1)

        self.device_connection_label = Gtk.Label(
            label="Нет подключения"
        )
        self.device_connection_label.set_xalign(0)
        self.device_connection_label.set_line_wrap(True)
        self.device_connection_label.get_style_context().add_class(
            "device-detail-value"
        )
        device_grid.attach(
            self.device_connection_label,
            1,
            1,
            1,
            1,
        )

        address_icon = icon_image(
            "emblem-system-symbolic",
            Gtk.IconSize.MENU,
        )
        address_icon.set_valign(Gtk.Align.CENTER)
        device_grid.attach(address_icon, 0, 2, 1, 1)

        address_caption = Gtk.Label(label="Адрес устройства")
        address_caption.set_xalign(0)
        address_caption.get_style_context().add_class(
            "device-detail-caption"
        )
        device_grid.attach(address_caption, 1, 2, 1, 1)

        self.device_address_label = Gtk.Label(label="Выберите устройство")
        self.device_address_label.set_xalign(0)
        self.device_address_label.set_selectable(True)
        self.device_address_label.get_style_context().add_class(
            "device-detail-value"
        )
        device_grid.attach(
            self.device_address_label,
            1,
            3,
            1,
            1,
        )

        self.device_message_label = Gtk.Label(
            label="Выполняется быстрая проверка устройства…"
        )
        self.device_message_label.set_xalign(0)
        self.device_message_label.set_line_wrap(True)
        self.device_message_label.get_style_context().add_class(
            "device-message"
        )
        device_card_box.pack_start(
            self.device_message_label,
            False,
            False,
            0,
        )

        refresh_button = toolbar_button(
            "Найти устройство заново",
            "view-refresh-symbolic",
            "Выполнить полный поиск и обновить сохранённый профиль",
            lambda _button: self.refresh_device(),
        )
        refresh_button.get_style_context().add_class(
            "device-refresh-button"
        )
        settings_box.pack_start(refresh_button, False, False, 0)
        self.busy_widgets.append(refresh_button)

        usb_button = toolbar_button(
            "Подключить сканер…",
            "drive-removable-media-symbolic",
            "Найти локальные сканеры через ipp-usb и SANE",
            lambda _button: self.connect_usb_scanner(),
        )
        usb_button.get_style_context().add_class(
            "device-usb-button"
        )
        settings_box.pack_start(usb_button, False, False, 0)
        self.busy_widgets.append(usb_button)

        network_button = toolbar_button(
            "Подключить по сети…",
            "network-wired-symbolic",
            "Выбрать найденное сетевое МФУ или указать его IP",
            lambda _button: self.connect_network_scanner(),
        )
        network_button.get_style_context().add_class(
            "device-network-button"
        )
        settings_box.pack_start(network_button, False, False, 0)
        self.busy_widgets.append(network_button)

        hint_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        settings_box.pack_start(hint_row, False, False, 2)

        hint_icon = icon_image(
            "dialog-information-symbolic",
            Gtk.IconSize.MENU,
        )
        hint_icon.set_valign(Gtk.Align.START)
        hint_row.pack_start(hint_icon, False, False, 1)

        hint_label = Gtk.Label(
            label=(
                "Для USB доступны ipp-usb/eSCL и HP HPLIP (hpaio). "
                "Потоковый режим используется для eSCL; hpaio сканирует "
                "автоподатчик штатным пакетным режимом SANE."
            )
        )
        hint_label.set_xalign(0)
        hint_label.set_line_wrap(True)
        hint_label.get_style_context().add_class("settings-hint")
        hint_row.pack_start(hint_label, True, True, 0)

        # Нижняя строка состояния.
        footer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin=8,
        )
        main_box.pack_end(footer, False, False, 0)

        self.spinner = Gtk.Spinner()
        footer.pack_start(self.spinner, False, False, 0)

        self.status_label = Gtk.Label(label="Готово")
        self.status_label.set_xalign(0)
        footer.pack_start(self.status_label, True, True, 0)

        version_label = Gtk.Label(label=f"Версия {APP_VERSION}")
        version_label.get_style_context().add_class("dim-label")
        footer.pack_end(version_label, False, False, 0)

        self.save_button.set_sensitive(False)
        self._connect_shortcuts()

    def _apply_css(self) -> None:
        """
        Загружает оформление так, чтобы несовместимость CSS не могла
        заблокировать запуск NAPS3.
        """
        primary_css = b"""
            .sidebar {
                background-color: shade(@theme_bg_color, 0.97);
                border-right: 1px solid alpha(@theme_fg_color, 0.12);
            }

            .settings-container,
            .settings-shell,
            .settings-viewport,
            .settings-surface,
            .settings-panel {
                background-color: @theme_bg_color;
                background-image: none;
            }

            .settings-container,
            .settings-shell,
            .settings-viewport,
            .settings-surface {
                border: none;
                box-shadow: none;
            }

            .content-separator {
                background-color: alpha(@theme_fg_color, 0.12);
                background-image: none;
                min-width: 1px;
                min-height: 1px;
                padding: 0;
                margin: 0;
            }

            .settings-panel entry,
            .settings-panel combobox,
            .settings-panel button {
                min-height: 30px;
            }

            .settings-panel entry,
            .settings-panel combobox {
                margin-bottom: 1px;
            }

            .device-card {
                background-color: @theme_base_color;
                background-image: none;
                border: 1px solid alpha(@theme_fg_color, 0.16);
                border-radius: 8px;
                box-shadow: 0 1px 2px alpha(black, 0.08);
            }

            .device-icon-box {
                background-color: alpha(@theme_selected_bg_color, 0.13);
                background-image: none;
                border-radius: 22px;
                color: @theme_selected_bg_color;
            }

            .device-name {
                font-size: 1.05em;
                font-weight: 600;
                color: @theme_fg_color;
            }

            .device-subtitle,
            .device-detail-caption,
            .device-message,
            .settings-hint {
                color: alpha(@theme_fg_color, 0.68);
            }

            .device-subtitle {
                font-size: 0.92em;
            }

            .device-detail-caption {
                font-size: 0.86em;
            }

            .device-detail-value {
                font-weight: 500;
                color: @theme_fg_color;
            }

            .device-message,
            .settings-hint {
                font-size: 0.90em;
            }

            .status-badge {
                border-radius: 10px;
                padding: 2px 8px;
                font-size: 0.86em;
                font-weight: 600;
            }

            .status-ready {
                background-color: alpha(#2e9b50, 0.16);
                color: #1f7a3d;
            }

            .status-checking {
                background-color: alpha(@theme_selected_bg_color, 0.14);
                color: @theme_selected_bg_color;
            }

            .status-error {
                background-color: alpha(#c43b3b, 0.14);
                color: #a72d2d;
            }

            .device-refresh-button {
                margin-top: 1px;
            }

            .preview-shell {
                background-color: @theme_bg_color;
            }

            .preview-area {
                background-color: #2d3442;
                padding: 22px;
            }

            list row {
                padding: 2px;
                border-bottom: 1px solid alpha(@theme_fg_color, 0.08);
            }

            list row:hover {
                background-color: alpha(@theme_selected_bg_color, 0.08);
            }

            list row:selected {
                background-color: alpha(@theme_selected_bg_color, 0.20);
            }

            .page-title {
                font-weight: 600;
            }

            .empty-title {
                color: #d7deea;
                font-size: 15px;
                font-weight: 600;
            }

            .dim-label {
                opacity: 0.72;
            }

            .badge {
                background-color: alpha(@theme_selected_bg_color, 0.15);
                color: @theme_fg_color;
                border-radius: 10px;
                padding: 2px 8px;
                font-weight: 600;
            }

            headerbar button.suggested-action {
                padding-left: 14px;
                padding-right: 14px;
            }
        """

        fallback_css = b"""
            .settings-container,
            .settings-shell,
            .settings-viewport,
            .settings-surface,
            .settings-panel {
                background-color: @theme_bg_color;
            }

            .content-separator {
                background-color: alpha(@theme_fg_color, 0.15);
                min-width: 1px;
                min-height: 1px;
            }

            .device-card {
                background-color: @theme_base_color;
                border: 1px solid alpha(@theme_fg_color, 0.16);
                border-radius: 6px;
            }

            .device-name,
            .status-badge,
            .badge {
                font-weight: bold;
            }

            .status-badge,
            .badge {
                border-radius: 8px;
                padding: 2px 7px;
            }

            .preview-area {
                background-color: #2d3442;
                padding: 18px;
            }

            .dim-label,
            .device-subtitle,
            .device-detail-caption,
            .device-message,
            .settings-hint {
                opacity: 0.72;
            }
        """

        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(primary_css)
        except GLib.Error as exc:
            print(
                "Предупреждение: основное оформление NAPS3 не принято GTK; "
                "используется безопасный вариант.",
                file=sys.stderr,
            )
            print(f"GTK CSS: {exc}", file=sys.stderr)

            provider = Gtk.CssProvider()
            try:
                provider.load_from_data(fallback_css)
            except GLib.Error as fallback_exc:
                print(
                    "Предупреждение: дополнительное оформление отключено; "
                    "используется системная тема РЕД ОС.",
                    file=sys.stderr,
                )
                print(
                    f"GTK CSS fallback: {fallback_exc}",
                    file=sys.stderr,
                )
                return

        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(
                screen,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _on_settings_toggled(
        self,
        button: Gtk.ToggleButton,
    ) -> None:
        visible = button.get_active()
        self.settings_revealer.set_reveal_child(visible)
        button.set_tooltip_text(
            "Скрыть параметры сканирования"
            if visible
            else "Показать параметры сканирования"
        )
        try:
            self._save_ui_settings()
        except OSError:
            pass

    def _add_labeled_widget(
        self,
        parent: Gtk.Box,
        label_text: str,
        widget: Gtk.Widget,
    ) -> None:
        label = Gtk.Label()
        label.set_markup(f"<b>{GLib.markup_escape_text(label_text)}</b>")
        label.set_xalign(0)
        parent.pack_start(label, False, False, 0)
        parent.pack_start(widget, False, False, 0)

    def _connect_shortcuts(self) -> None:
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)

        def add(key: str, mods: Gdk.ModifierType, callback) -> None:
            keyval = Gdk.keyval_from_name(key)
            accel.connect(
                keyval,
                mods,
                Gtk.AccelFlags.VISIBLE,
                lambda *_args: bool(callback() is None),
            )

        add("p", Gdk.ModifierType.CONTROL_MASK, self.start_scan)
        add("o", Gdk.ModifierType.CONTROL_MASK, self.import_images)
        add("s", Gdk.ModifierType.CONTROL_MASK, self.export_pages)
        add("Delete", Gdk.ModifierType(0), self.delete_selected)
        add("Left", Gdk.ModifierType.CONTROL_MASK, lambda: self.rotate_selected(-90))
        add("Right", Gdk.ModifierType.CONTROL_MASK, lambda: self.rotate_selected(90))
        add("Up", Gdk.ModifierType.MOD1_MASK, lambda: self.move_selected(-1))
        add("Down", Gdk.ModifierType.MOD1_MASK, lambda: self.move_selected(1))

    # -------------------------- settings ---------------------------

    def _load_ui_settings(self) -> None:
        self.match_entry.set_text(
            normalized_match_filter(self.settings.get("match", DEFAULT_MATCH))
        )
        self.source_combo.set_active_id(
            str(self.settings.get("source", "ADF"))
        )
        self.mode_combo.set_active_id(
            str(self.settings.get("mode", "Color"))
        )
        self.dpi_combo.set_active_id(
            str(self.settings.get("dpi", 300))
        )
        self.paper_combo.set_active_id(
            str(self.settings.get("paper", "A4"))
        )
        self.stream_adf_check.set_active(
            bool(self.settings.get("stream_adf", True))
        )

        settings_visible = bool(
            self.settings.get("settings_visible", True)
        )
        self.settings_toggle.set_active(settings_visible)
        self.settings_revealer.set_reveal_child(settings_visible)

    @staticmethod
    def _profile_adf_state(
        profile: dict[str, object],
    ) -> tuple[bool, bool, bool]:
        """Return (known, feeder present, hardware duplex supported)."""
        if "adf_capabilities_known" in profile:
            return (
                bool(profile.get("adf_capabilities_known")),
                bool(profile.get("adf_present")),
                bool(profile.get("adf_duplex_supported")),
            )

        # Compatibility with profiles saved by versions up to 0.6.2.
        simplex = profile.get("adf_simplex_resolutions", []) or []
        duplex = profile.get("adf_duplex_resolutions", []) or []
        if simplex or duplex:
            return True, True, bool(duplex)
        return False, True, False

    def _update_source_options(
        self,
        profile: dict[str, object],
        preferred_source: str = "",
    ) -> None:
        """Show manual duplex only for a verified simplex-only feeder."""
        current_source = (
            preferred_source
            or self.source_combo.get_active_id()
            or str(self.settings.get("source", "ADF"))
        )
        known, has_adf, supports_duplex = self._profile_adf_state(profile)

        options: list[tuple[str, str]] = []
        if not known or has_adf:
            options.append(("ADF", "Автоподатчик"))
            if known and not supports_duplex:
                options.append(
                    (
                        "ADF Manual Duplex",
                        "Автоподатчик, ручной дуплекс",
                    )
                )
            else:
                options.append(
                    (
                        "ADF Duplex",
                        "Автоподатчик, две стороны",
                    )
                )
        options.append(("Flatbed", "Стекло"))

        available = {option_id for option_id, _label in options}
        if current_source not in available:
            if (
                current_source == "ADF Manual Duplex"
                and "ADF Duplex" in available
            ):
                current_source = "ADF Duplex"
            elif (
                current_source == "ADF Duplex"
                and "ADF Manual Duplex" in available
            ):
                current_source = "ADF Manual Duplex"
            elif "ADF" in available:
                current_source = "ADF"
            else:
                current_source = "Flatbed"

        self.source_combo.remove_all()
        for option_id, label in options:
            self.source_combo.append(option_id, label)
        self.source_combo.set_active_id(current_source)

    def _save_ui_settings(self) -> None:
        settings: dict[str, object] = {
            "match": normalized_match_filter(self.match_entry.get_text()),
            "source": self.source_combo.get_active_id() or "ADF",
            "mode": self.mode_combo.get_active_id() or "Color",
            "dpi": int(self.dpi_combo.get_active_id() or "300"),
            "paper": self.paper_combo.get_active_id() or "A4",
            "stream_adf": self.stream_adf_check.get_active(),
            "settings_visible": self.settings_toggle.get_active(),
        }
        if self.scanner_profile:
            settings[PROFILE_KEY] = dict(self.scanner_profile)

        self.settings = settings
        save_settings(settings)

    # -------------------------- messages ---------------------------

    def show_error(self, title: str, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def show_info(self, title: str, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def ask_yes_no(self, title: str, message: str) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=title,
        )
        dialog.format_secondary_text(message)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES

    def _show_manual_duplex_dialog(
        self,
        sheet_count: int,
        event: threading.Event,
        result: dict[str, object],
    ) -> bool:
        """Ask for the physical second pass without blocking the worker UI."""
        self.set_status(
            f"Лицевые стороны готовы: {sheet_count}. "
            "Переложите выданную стопку обратно в АПД."
        )
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.NONE,
            text="Ручной дуплекс — второй проход",
        )
        dialog.format_secondary_text(
            f"Лицевые стороны отсканированы: {sheet_count}.\n\n"
            "1. Возьмите стопку ровно так, как её выдало МФУ.\n"
            "2. Не меняйте листы местами и не разворачивайте их по одному.\n"
            "3. Вставьте всю стопку обратно в АПД и дождитесь "
            "срабатывания датчика.\n\n"
            "NAPS3 сам развернёт порядок оборотов и повернёт их на 180°."
        )
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        scan_button = dialog.add_button(
            "Сканировать обороты",
            Gtk.ResponseType.OK,
        )
        scan_button.get_style_context().add_class("suggested-action")
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        result["continue"] = response == Gtk.ResponseType.OK
        event.set()
        return False

    def _wait_for_manual_duplex_turn(
        self,
        sheet_count: int,
    ) -> bool:
        event = threading.Event()
        result: dict[str, object] = {}
        GLib.idle_add(
            self._show_manual_duplex_dialog,
            sheet_count,
            event,
            result,
        )
        while not event.wait(0.1):
            if self.cancel_requested:
                return False
        return bool(result.get("continue"))

    # ----------------------- device discovery ----------------------

    def _set_device_status(
        self,
        state: str,
        status_text: str,
        message: str,
    ) -> None:
        context = self.device_status_badge.get_style_context()
        for css_class in (
            "status-ready",
            "status-checking",
            "status-error",
        ):
            context.remove_class(css_class)

        css_class = {
            "ready": "status-ready",
            "checking": "status-checking",
            "error": "status-error",
        }.get(state, "status-checking")

        context.add_class(css_class)
        self.device_status_badge.set_text(status_text)
        self.device_message_label.set_text(message.strip())

    def _set_device_profile(
        self,
        profile: dict[str, object],
        message: str,
        state: str = "ready",
    ) -> None:
        self._update_source_options(profile)
        if not profile:
            self.device_name_label.set_text("Сканер не выбран")
            self.device_model_label.set_text(
                "Windows WIA" if IS_WINDOWS else "USB или сеть"
            )
            self.device_connection_label.set_text("Нет подключения")
            self.device_address_label.set_text("Выберите устройство")
            self._set_device_status(
                "error" if state == "error" else "checking",
                "Не выбран" if state == "error" else "Проверка",
                message,
            )
            return

        name = profile_name(profile)
        url = profile_url(profile)
        parsed = urllib.parse.urlsplit(url) if url else None
        host = parsed.hostname if parsed else ""
        port = parsed.port if parsed else None

        display_name = name
        connection_kind = str(
            profile.get("connection_kind") or "usb"
        )
        connection_caption = {
            "network": "Сеть",
            "windows-wia": "Windows",
        }.get(connection_kind, "USB")
        model_suffix = connection_caption

        bracket_match = re.search(r"\[([^\]]+)\]", name)
        if bracket_match:
            model_suffix = (
                f"{bracket_match.group(1)} • {connection_caption}"
            )
            display_name = re.sub(
                r"\s*\[[^\]]+\]\s*",
                " ",
                name,
            ).strip()

        display_name = re.sub(
            r"\s*\(USB\)\s*$",
            "",
            display_name,
            flags=re.IGNORECASE,
        ).strip()

        self.device_name_label.set_text(
            display_name or DEFAULT_SCANNER_NAME
        )
        self.device_model_label.set_text(model_suffix)
        if connection_kind == "network":
            network_ip = str(profile.get("ip") or "").strip()
            backend = str(profile.get("backend") or "SANE")
            self.device_connection_label.set_text(
                "Сетевое eSCL через sane-airscan"
                if url
                else f"Сетевой сканер через SANE ({backend})"
            )
            self.device_address_label.set_text(
                network_ip or "Сетевой адрес определяется автоматически"
            )
        elif connection_kind == "windows-wia":
            self.device_connection_label.set_text(
                "Системный драйвер Windows Image Acquisition (WIA)"
            )
            self.device_address_label.set_text(
                str(profile.get("port") or "Устройство зарегистрировано в Windows")
            )
        elif connection_kind == "usb-hpaio":
            self.device_connection_label.set_text(
                "Прямой USB через HP HPLIP (hpaio)"
            )
            self.device_address_label.set_text("USB-порт этого компьютера")
        elif connection_kind == "usb-sane":
            backend = str(profile.get("backend") or "SANE")
            self.device_connection_label.set_text(
                f"Локальный сканер через SANE ({backend})"
            )
            self.device_address_label.set_text("USB-порт этого компьютера")
        else:
            self.device_connection_label.set_text(
                "Локальный USB через sane-airscan и ipp-usb"
            )
            self.device_address_label.set_text(
                f"{host}:{port}"
                if host and port
                else "Адрес определяется автоматически"
            )

        if state == "ready":
            self._set_device_status(
                "ready",
                "Готов",
                message,
            )
        elif state == "error":
            self._set_device_status(
                "error",
                "Недоступен",
                message,
            )
        else:
            self._set_device_status(
                "checking",
                "Проверка",
                message,
            )

    def initialize_scanner_profile(self) -> None:
        if self.scanner_profile:
            self._show_profile(
                self.scanner_profile,
                (
                    "Сохранённый профиль Windows загружен"
                    if self.scanner_profile.get("backend") == "wia"
                    else "Сохранённый USB-профиль загружен"
                ),
            )
            self._probe_saved_profile_async()
            return

        self.set_busy(
            True,
            (
                "Проверка сканеров Windows WIA…"
                if IS_WINDOWS
                else "Проверка локального USB-подключения…"
            ),
        )
        self._set_device_status(
            "checking",
            "Проверка",
            (
                "Проверяются сканеры Windows WIA…"
                if IS_WINDOWS
                else "Проверяется локальное USB-подключение…"
            ),
        )
        self.set_status(
            "Проверка сканеров Windows WIA…"
            if IS_WINDOWS
            else "Проверка локального USB-подключения…"
        )
        match_filter = self.match_entry.get_text().strip()

        def worker() -> None:
            try:
                profile = default_profile_if_available(match_filter)
                if not profile:
                    profile = discover_scanner_profile(match_filter)
                GLib.idle_add(
                    self._profile_ready,
                    profile,
                    "",
                    (
                        "Профиль WIA создан для этого компьютера."
                        if IS_WINDOWS
                        else "USB-профиль создан для этого ПК."
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._profile_ready,
                    {},
                    friendly_general_error(exc, "найти сканер"),
                    "",
                )

        threading.Thread(target=worker, daemon=True).start()

    def _probe_saved_profile_async(self) -> None:
        profile = dict(self.scanner_profile or {})
        probe_token = object()
        self._profile_probe_token = probe_token
        self.set_busy(True, "Проверка сохранённого сканера…")

        def ready(error: str) -> bool:
            # A delayed startup probe must not replace a subsequent manual
            # selection or change the status of a scan already in progress.
            if self._profile_probe_token is not probe_token:
                return False
            self._profile_probe_token = None
            self.set_busy(False)
            if self.scanner_profile != profile:
                return False
            return self._saved_profile_probe_ready(
                {} if error else profile, error, False
            )

        def worker() -> None:
            try:
                # Probe only the saved endpoint. General discovery can prefer
                # another MFP while the selected one is asleep or disconnected.
                url = profile_url(profile)
                device_id = str(profile.get("device_id") or "")
                if profile.get("backend") == "wia":
                    if not device_id or not probe_wia_device(device_id):
                        raise Naps3Error(
                            "Выбранный сканер больше не зарегистрирован в "
                            "Windows. Проверьте подключение или выберите его заново."
                        )
                elif url:
                    if not probe_escl_url(url):
                        raise Naps3Error(
                            "Выбранный сканер не отвечает. Разбудите МФУ и "
                            "проверьте подключение. При смене адреса выберите "
                            "это устройство заново."
                        )
                elif device_id:
                    env = local_sane_backend_environment(
                        sane_backend_name(device_id)
                    )
                    result = subprocess.run(
                        ["scanimage", "-d", device_id, "--help"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=5,
                        check=False,
                        env=env,
                    )
                    if result.returncode != 0:
                        raise Naps3Error(
                            friendly_scan_error(safe_decode(result.stdout))
                        )
                else:
                    raise Naps3Error(
                        "В сохранённом профиле нет адреса сканера. "
                        "Выберите нужное устройство заново."
                    )
                GLib.idle_add(ready, "")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    ready,
                    friendly_general_error(exc, "проверить выбранный сканер"),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _saved_profile_probe_ready(
        self,
        profile: dict[str, object],
        error: str,
        recovered: bool,
    ) -> bool:
        if error:
            if self.scanner_profile and is_legacy_default_profile(
                self.scanner_profile
            ):
                old_name = profile_name(self.scanner_profile)
                self.scanner_profile = None
                match_entry = getattr(self, "match_entry", None)
                if match_entry is not None:
                    match_entry.set_text("")
                self._set_device_profile(
                    {},
                    (
                        f"Старый автоматический профиль «{old_name}» удалён, "
                        "потому что устройство не найдено. Выберите реально "
                        "подключённый сканер.\n\n"
                        f"{error}"
                    ),
                    "error",
                )
                try:
                    self._save_ui_settings()
                except OSError:
                    pass
                self.set_status("Сканер не выбран")
                return False
            if self.scanner_profile:
                self._set_device_profile(
                    self.scanner_profile,
                    error,
                    "error",
                )
            self.set_status("Выбранный сканер недоступен")
            return False

        self.scanner_profile = dict(profile)
        connection_kind = str(
            self.scanner_profile.get("connection_kind") or "usb"
        )
        self._set_device_profile(
            self.scanner_profile,
            (
                (
                    "Сетевой профиль M428/M429 найден и сохранён."
                    if connection_kind == "network"
                    else "USB-профиль автоматически обновлён для этого ПК."
                )
                if recovered
                else "Профиль загружен. Можно сразу начинать сканирование."
            ),
            "ready",
        )
        try:
            self._save_ui_settings()
        except OSError:
            pass
        self.set_status(
            (
                "Сканер найден на этом ПК, профиль автоматически обновлён"
                if recovered
                else "Сохранённый сканер готов"
            )
        )
        return False

    def _show_profile(
        self,
        profile: dict[str, object],
        status: str,
    ) -> None:
        self._set_device_profile(
            profile,
            "Профиль сохранён. Можно сразу начинать сканирование.",
            "ready",
        )
        self.set_status(status)

    def refresh_device(self) -> None:
        if self.is_busy:
            return

        self.set_busy(True, "Поиск USB-сканера…")
        self._set_device_status(
            "checking",
            "Поиск",
            "Сначала ищется локальный USB, затем доступные сетевые eSCL-устройства…",
        )
        self.set_status("Поиск USB-сканера…")
        match_filter = self.match_entry.get_text().strip()

        def worker() -> None:
            try:
                profile = discover_scanner_profile(match_filter)

                GLib.idle_add(
                    self._profile_ready,
                    profile,
                    "",
                    "Сканер найден, способ подключения сохранён.",
                )
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._profile_ready,
                    {},
                    friendly_general_error(exc, "найти сканер"),
                    "",
                )

        threading.Thread(target=worker, daemon=True).start()

    def _profile_ready(
        self,
        profile: dict[str, object],
        error: str,
        success_status: str,
    ) -> bool:
        self.set_busy(False)
        if error:
            self._set_device_status(
                "error",
                "Ошибка",
                error,
            )
            self.set_status("Сканер не найден")
            return False

        self.scanner_profile = dict(profile)
        self._show_profile(
            self.scanner_profile,
            success_status or "Сканер готов к работе",
        )

        try:
            self._save_ui_settings()
        except OSError:
            pass

        return False

    def connect_usb_scanner(self) -> None:
        if self.is_busy:
            return
        self.set_busy(
            True,
            "Поиск сканеров Windows WIA…"
            if IS_WINDOWS
            else "Поиск локальных сканеров…",
        )
        self._set_device_status(
            "checking",
            "Windows" if IS_WINDOWS else "USB",
            (
                "Читается список установленных устройств WIA…"
                if IS_WINDOWS
                else "Проверяются ipp-usb/eSCL и установленные SANE-драйверы…"
            ),
        )
        match_filter = self.match_entry.get_text().strip()

        def worker() -> None:
            try:
                profiles = discover_usb_scanners(match_filter)
                GLib.idle_add(self._usb_scanners_ready, profiles, "")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._usb_scanners_ready,
                    [],
                    friendly_general_error(
                        exc,
                        "найти сканер Windows" if IS_WINDOWS else "найти локальный сканер",
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _usb_scanners_ready(
        self,
        profiles: list[dict[str, object]],
        error: str,
    ) -> bool:
        self.set_busy(False)
        if error:
            self._set_device_status("error", "Ошибка", error)
            self.show_error(
                "Ошибка подключения Windows" if IS_WINDOWS else "Ошибка USB-подключения",
                error,
            )
            return False
        if not profiles:
            diagnostic = "" if IS_WINDOWS else usb_diagnostic_text()
            message = (
                "Сканер Windows не найден. Убедитесь, что МФУ включено и "
                "подключено, затем установите WIA-драйвер производителя."
                if IS_WINDOWS
                else (
                    "Локальный сканер не найден. Убедитесь, что МФУ включено, "
                    "USB-кабель подключён и установлен подходящий SANE-драйвер."
                )
            )
            if diagnostic:
                message += f"\n\nДиагностика: {diagnostic}"
            self._set_device_status("error", "Не найден", message)
            self.show_error(
                "Сканер Windows не найден" if IS_WINDOWS else "Локальный сканер не найден",
                message,
            )
            self.set_status(
                "Сканер Windows не найден" if IS_WINDOWS else "Локальный сканер не найден"
            )
            return False

        if len(profiles) == 1:
            profile = dict(profiles[0])
        else:
            dialog = USBScannerDialog(self, profiles)
            response = dialog.run()
            profile = dialog.get_profile()
            dialog.destroy()
            if response != Gtk.ResponseType.OK or not profile:
                self.set_status(
                    "Выбор сканера отменён"
                    if IS_WINDOWS
                    else "Подключение сканера отменено"
                )
                return False

        self.scanner_profile = profile
        self.match_entry.set_text(profile_name(profile))
        self._set_device_profile(
            profile,
            (
                "Сканер подключён через Windows WIA."
                if profile.get("backend") == "wia"
                else (
                    "USB-сканер подключён через HPLIP (hpaio)."
                    if profile.get("connection_kind") == "usb-hpaio"
                    else (
                        f"Сканер подключён через SANE ({profile.get('backend')})."
                        if profile.get("connection_kind") == "usb-sane"
                        else "USB-сканер подключён через ipp-usb/eSCL."
                    )
                )
            ),
            "ready",
        )
        try:
            self._save_ui_settings()
        except OSError:
            pass
        self.set_status(
            "Сканер Windows подключён"
            if profile.get("backend") == "wia"
            else "Локальный сканер подключён"
        )
        return False

    def connect_network_scanner(self) -> None:
        if self.is_busy:
            return
        self.set_busy(True, "Поиск сетевых eSCL-сканеров…")
        self._set_device_status(
            "checking",
            "Поиск",
            "Ищутся сетевые устройства sane-airscan…",
        )

        def worker() -> None:
            try:
                devices = discover_network_scanners()
                GLib.idle_add(self._network_scanners_ready, devices, "")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._network_scanners_ready,
                    [],
                    friendly_general_error(exc, "найти сетевые сканеры"),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _network_scanners_ready(
        self,
        devices: list[dict[str, str]],
        error: str,
    ) -> bool:
        self.set_busy(False)
        if error:
            self.set_status("Автоматический поиск сети не выполнен")

        dialog = NetworkScannerDialog(self, devices)
        response = dialog.run()
        name, address, device_id = dialog.get_selection()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            self.set_status("Подключение по сети отменено")
            return False
        if not address:
            self.show_error(
                "Не указан сетевой адрес",
                "Выберите устройство из списка или введите IP/имя хоста.",
            )
            return False

        self.set_busy(True, "Проверка сетевого eSCL-сканера…")

        def worker() -> None:
            try:
                profile = build_network_profile(name, address, device_id)
                GLib.idle_add(
                    self._network_profile_ready,
                    profile,
                    "",
                )
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._network_profile_ready,
                    {},
                    friendly_general_error(exc, "подключить сетевой сканер"),
                )

        threading.Thread(target=worker, daemon=True).start()
        return False

    def _network_profile_ready(
        self,
        profile: dict[str, object],
        error: str,
    ) -> bool:
        self.set_busy(False)
        if error:
            self._set_device_status("error", "Ошибка", error)
            self.set_status("Сетевой сканер не подключён")
            self.show_error("Ошибка сетевого подключения", error)
            return False

        self.scanner_profile = dict(profile)
        self.match_entry.set_text(profile_name(profile))
        self._set_device_profile(
            profile,
            "Сетевой eSCL-профиль подключён. Потоковый АПД готов.",
            "ready",
        )
        try:
            self._save_ui_settings()
        except OSError:
            pass
        self.set_status("Сетевой сканер подключён")
        return False

    # -------------------------- scanning ---------------------------

    def start_scan(self) -> None:
        if self.is_busy:
            return

        if not IS_WINDOWS and shutil.which("scanimage") is None:
            self.show_error(
                "Не удаётся начать сканирование",
                "Не установлен компонент scanimage. "
                "Установите пакет sane-backends.",
            )
            return

        source = self.source_combo.get_active_id() or "ADF"
        mode = self.mode_combo.get_active_id() or "Color"
        dpi = int(self.dpi_combo.get_active_id() or "300")
        paper = self.paper_combo.get_active_id() or "A4"
        match_filter = self.match_entry.get_text().strip()
        cached_profile = (
            dict(self.scanner_profile)
            if self.scanner_profile
            else None
        )
        stream_adf = self.stream_adf_check.get_active()
        adjustment_message = ""

        # Validate against the feeder section of ScannerCapabilities. The M227
        # advertises 600/1200 dpi for the platen but only up to 300 dpi for its
        # ADF. Posting 600 dpi to eSCL returns 409 and previously triggered a
        # silent 2.5-minute SANE fallback.
        if (
            cached_profile
            and source != "Flatbed"
        ):
            simplex_resolutions = [
                int(value)
                for value in (
                    cached_profile.get("adf_simplex_resolutions", []) or []
                )
                if str(value).isdigit()
            ]
            duplex_resolutions = [
                int(value)
                for value in (
                    cached_profile.get("adf_duplex_resolutions", []) or []
                )
                if str(value).isdigit()
            ]
            if not simplex_resolutions and not duplex_resolutions:
                direct_url = profile_scan_url(cached_profile)
                capabilities = (
                    fetch_escl_capabilities(direct_url, 1.0)
                    if direct_url
                    else b""
                )
                parsed_caps = parse_escl_adf_capabilities(capabilities)
                simplex_resolutions = [
                    int(value)
                    for value in parsed_caps["simplex_resolutions"]
                ]
                duplex_resolutions = [
                    int(value)
                    for value in parsed_caps["duplex_resolutions"]
                ]
                if simplex_resolutions or duplex_resolutions:
                    cached_profile.update(adf_profile_fields(parsed_caps))
                    self.scanner_profile = dict(cached_profile)
                    self._update_source_options(cached_profile, source)

            capabilities_known = bool(
                cached_profile.get("adf_capabilities_known", False)
                or simplex_resolutions
                or duplex_resolutions
            )
            if (
                source == "ADF Duplex"
                and capabilities_known
                and not duplex_resolutions
            ):
                self.source_combo.set_active_id("ADF Manual Duplex")
                self._save_ui_settings()
                self.show_error(
                    "Двусторонний АПД недоступен",
                    f"{profile_name(cached_profile)} не поддерживает "
                    "двустороннее сканирование через АПД. "
                    "Источник переключён на «Ручной дуплекс». "
                    "Нажмите «Сканировать» ещё раз.",
                )
                return

            supported_resolutions = (
                duplex_resolutions
                if source == "ADF Duplex"
                else simplex_resolutions
            )
            if stream_adf and supported_resolutions and dpi not in supported_resolutions:
                lower_or_equal = [
                    value for value in supported_resolutions if value <= dpi
                ]
                effective_dpi = (
                    max(lower_or_equal)
                    if lower_or_equal
                    else min(supported_resolutions)
                )
                requested_dpi = dpi
                dpi = effective_dpi
                self.dpi_combo.set_active_id(str(dpi))
                adjustment_message = (
                    f"АПД этого МФУ поддерживает максимум {dpi} dpi; "
                    f"вместо {requested_dpi} dpi включён быстрый {dpi} dpi."
                )

        self._save_ui_settings()
        self.cancel_requested = False
        self.set_busy(
            True,
            adjustment_message or "Подготовка к сканированию…",
            cancellable=True,
        )

        self.scan_sequence += 1
        scan_token = f"{time.strftime('%Y%m%d-%H%M%S')}-{self.scan_sequence:03d}-{uuid.uuid4().hex[:6]}"
        scan_dir = self.session_dir / f"scan-{scan_token}"
        scan_dir.mkdir(parents=True, exist_ok=False)
        append_scan_log(
            "START "
            f"token={scan_token} source={source} mode={mode} dpi={dpi} "
            f"paper={paper} stream={stream_adf} "
            f"profile={profile_name(cached_profile or {})}"
        )
        if adjustment_message:
            append_scan_log(f"ADF capability adjustment: {adjustment_message}")

        def worker() -> None:
            try:
                warning = ""
                if source == "ADF Manual Duplex":
                    front_dir = scan_dir / "fronts"
                    back_dir = scan_dir / "backs"
                    front_dir.mkdir()
                    back_dir.mkdir()
                    append_scan_log("Manual duplex front pass start")
                    front_files, profile = self._scan_worker(
                        front_dir,
                        match_filter,
                        "ADF",
                        mode,
                        dpi,
                        paper,
                        cached_profile,
                        stream_adf,
                    )
                    append_scan_log(
                        f"Manual duplex front pass complete pages={len(front_files)}"
                    )
                    continue_duplex = self._wait_for_manual_duplex_turn(
                        len(front_files)
                    )
                    if not continue_duplex:
                        raise Naps3Error(
                            "Ручной дуплекс отменён перед "
                            "сканированием оборотов."
                        )
                    GLib.idle_add(
                        self.set_status,
                        "Сканируются обороты…",
                    )
                    append_scan_log(
                        "Manual duplex back pass start "
                        "automatic_reverse=True automatic_rotate=180"
                    )
                    back_files, profile = self._scan_worker(
                        back_dir,
                        match_filter,
                        "ADF",
                        mode,
                        dpi,
                        paper,
                        profile,
                        stream_adf,
                    )
                    workers = min(3, max(1, len(back_files)))
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=workers
                    ) as executor:
                        back_files = list(
                            executor.map(rotate_page_file_180, back_files)
                        )
                    files = interleave_manual_duplex_pages(
                        front_files,
                        back_files,
                        reverse_backs=True,
                    )
                    append_scan_log(
                        "Manual duplex merged "
                        f"fronts={len(front_files)} backs={len(back_files)} "
                        f"pages={len(files)} reverse_backs=True rotate_backs=180"
                    )
                    if len(front_files) != len(back_files):
                        warning = (
                            "Число лицевых и оборотных сторон не "
                            f"совпало: {len(front_files)} и {len(back_files)}. "
                            "Все полученные сканы сохранены; непарные "
                            "страницы добавлены в конец."
                        )
                else:
                    files, profile = self._scan_worker(
                        scan_dir,
                        match_filter,
                        source,
                        mode,
                        dpi,
                        paper,
                        cached_profile,
                        stream_adf,
                    )
                GLib.idle_add(
                    self._scan_ready,
                    files,
                    "",
                    profile,
                    warning,
                )
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._scan_ready,
                    [],
                    friendly_general_error(exc, "выполнить сканирование"),
                    {},
                )

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _retryable_device_error(raw: str) -> bool:
        lower = raw.casefold()
        return any(
            marker in lower
            for marker in (
                "invalid argument",
                "no such device",
                "open of device",
                "failed to open",
                "device not found",
                "0x80210003",  # WIA_ERROR_PAPER_EMPTY
                "0x80210005",  # WIA_ERROR_OFFLINE
                "0x80210006",  # WIA_ERROR_BUSY
                "0x80210007",  # WIA_ERROR_WARMING_UP
                "0x80210008",  # WIA_ERROR_USER_INTERVENTION
                "0x8021000a",  # WIA_ERROR_DEVICE_COMMUNICATION
                "0x8021000d",  # WIA_ERROR_DEVICE_LOCKED
                "0x80210015",  # WIA_S_NO_DEVICE_AVAILABLE
            )
        )

    @staticmethod
    def _device_busy_error(raw: str) -> bool:
        lower = raw.casefold()
        return any(
            marker in lower
            for marker in (
                "device busy",
                "resource busy",
                "sane_status_device_busy",
                "status = busy",
            )
        )

    def _scan_windows_wia(
        self,
        scan_dir: Path,
        profile: dict[str, object],
        source: str,
        mode: str,
        dpi: int,
        paper: str,
    ) -> tuple[list[Path], str, dict[str, object]]:
        """Scan the exact selected Windows device through the bundled WIA bridge."""
        device_id = str(profile.get("device_id") or "").strip()
        if not device_id:
            return [], "В профиле WIA нет идентификатора устройства.", profile

        for pattern in ("page-*", "raw-*"):
            for old_file in scan_dir.glob(pattern):
                old_file.unlink(missing_ok=True)

        command = build_wia_scan_command(
            device_id,
            scan_dir,
            source,
            mode,
            dpi,
            paper,
        )
        GLib.idle_add(
            self._set_device_profile,
            profile,
            "Соединение WIA установлено. Идёт сканирование…",
            "ready",
        )
        GLib.idle_add(
            self.set_status,
            (
                "Сканирование со стекла через Windows WIA…"
                if source == "Flatbed"
                else "Сканирование всех листов из автоподатчика через Windows WIA…"
            ),
        )
        append_scan_log(
            f"WIA engine device={device_id} source={source} dpi={dpi} mode={mode}"
        )

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=windows_creation_flags(),
            )
        except OSError as exc:
            return [], f"Не удалось запустить Windows WIA: {exc}", profile

        self.current_process = process
        try:
            stdout, stderr = process.communicate()
        finally:
            if self.current_process is process:
                self.current_process = None

        raw_files = sorted(
            path
            for path in scan_dir.glob("raw-*.bmp")
            if path.is_file() and path.stat().st_size > 0
        )
        if self.cancel_requested:
            for raw_file in raw_files:
                raw_file.unlink(missing_ok=True)
            raise Naps3Error("Сканирование отменено пользователем.")

        if not raw_files:
            error = bridge_error_text(stdout, stderr)
            append_scan_log(f"WIA scan failed: {compact_details(error, 600)}")
            return [], error, profile

        GLib.idle_add(
            self.set_status,
            f"Получено страниц: {len(raw_files)}. Подготовка предпросмотра…",
        )
        targets = [
            scan_dir / f"page-{index:04d}.png"
            for index in range(1, len(raw_files) + 1)
        ]
        workers = 1 if dpi >= 600 else min(2, max(1, len(raw_files)))
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers
            ) as executor:
                futures = [
                    executor.submit(
                        self._convert_stream_document,
                        raw,
                        target,
                        mode == "Lineart",
                    )
                    for raw, target in zip(raw_files, targets)
                ]
                files = [future.result() for future in futures]
        finally:
            for raw_file in raw_files:
                raw_file.unlink(missing_ok=True)

        if process.returncode != 0:
            append_scan_log(
                "WIA preserved partial pages after backend error: "
                f"{compact_details(bridge_error_text(stdout, stderr), 600)}"
            )
        append_scan_log(f"WIA scan complete pages={len(files)}")
        updated = dict(profile)
        updated["scan_engine"] = "windows-wia-v1"
        updated["saved_at"] = int(time.time())
        return files, "", updated

    def _scan_once(
        self,
        scan_dir: Path,
        profile: dict[str, object],
        source: str,
        mode: str,
        dpi: int,
        paper: str,
        force_resolve_device: bool = False,
    ) -> tuple[list[Path], str, dict[str, object]]:
        """Scan through SANE, using uncompressed PNM for fast ADF feeding.

        The previous PNG batch mode compressed every page before requesting the
        next sheet. At 300 dpi this could keep the ADF idle for several seconds.
        PNM is written with almost no CPU work; conversion to preview PNG starts
        only after the feeder has finished.
        """
        page_width_mm, page_height_mm = (
            (210, 297) if paper == "A4" else (216, 279)
        )
        device = str(profile.get("device_id") or "")
        env = {**os.environ, "LC_ALL": "C"}
        connection_kind = str(profile.get("connection_kind") or "")
        if connection_kind in {"usb-hpaio", "usb-sane"} and device:
            env = local_sane_backend_environment(
                sane_backend_name(device)
            )

        scan_url = profile_url(profile)
        scan_host = (
            urllib.parse.urlsplit(scan_url).hostname
            if scan_url
            else ""
        )
        is_local_airscan = (
            str(profile.get("connection_kind") or "") == "usb"
            or scan_host in {"127.0.0.1", "localhost", "::1"}
        )
        if is_local_airscan and scan_url:
            # airscan:eN was allocated inside NAPS3's one-device config.
            # Reusing it with the system config made the first attempt of
            # almost every scan fail after five seconds with Invalid argument.
            env = profile_environment(profile)

        if not device or force_resolve_device:
            GLib.idle_add(
                self.set_status,
                "Быстрая подготовка сохранённого профиля сканера…",
            )
            device, env, profile = resolve_profile_device(profile)

        is_network_profile = str(
            profile.get("connection_kind") or ""
        ) == "network"
        software_lineart = mode == "Lineart" and is_network_profile
        scanner_mode = "Gray" if software_lineart else mode
        scanner_source = sane_source_for_profile(profile, source)

        GLib.idle_add(
            self._set_device_profile,
            profile,
            "Соединение установлено. Идёт сканирование…",
            "ready",
        )

        for pattern in ("page-*", "raw-*"):
            for old_file in scan_dir.glob(pattern):
                old_file.unlink(missing_ok=True)

        base_command = [
            "scanimage",
            "-d",
            device,
            "--source",
            scanner_source,
            "--mode",
            scanner_mode,
            "--resolution",
            str(dpi),
            "-x",
            str(page_width_mm),
            "-y",
            str(page_height_mm),
        ]

        append_scan_log(
            f"SANE engine device={device} source={scanner_source} dpi={dpi} "
            f"mode={scanner_mode} software_lineart={software_lineart}"
        )

        if source == "Flatbed":
            raw_path = scan_dir / "raw-flatbed.pnm"
            output_path = scan_dir / "page-0001.png"
            command = [*base_command, "--format=pnm"]
            GLib.idle_add(self.set_status, "Сканирование со стекла…")

            with raw_path.open("wb") as output_file:
                process = subprocess.Popen(
                    command,
                    stdout=output_file,
                    stderr=subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
                self.current_process = process
                try:
                    stderr = process.communicate()[1]
                finally:
                    if process.stderr is not None:
                        process.stderr.close()
                    if self.current_process is process:
                        self.current_process = None

            return_code = process.returncode

            if self.cancel_requested:
                raw_path.unlink(missing_ok=True)
                output_path.unlink(missing_ok=True)
                raise Naps3Error("Сканирование отменено пользователем.")

            error = safe_decode(stderr).strip()
            if return_code != 0:
                raw_path.unlink(missing_ok=True)
                output_path.unlink(missing_ok=True)
                if not error:
                    error = f"scanimage завершился с кодом {return_code}."
                append_scan_log(
                    "SANE flatbed failed "
                    f"rc={return_code}: {compact_details(error, 500)}"
                )
                return [], error, profile

            if raw_path.exists() and raw_path.stat().st_size:
                try:
                    self._convert_stream_document(
                        raw_path,
                        output_path,
                        lineart=software_lineart,
                    )
                except Exception as exc:  # noqa: BLE001
                    output_path.unlink(missing_ok=True)
                    error = (
                        "Сканер передал неполное изображение: "
                        f"{compact_details(exc, 500)}"
                    )
                    append_scan_log(
                        f"SANE flatbed rejected image: {compact_details(exc, 500)}"
                    )
                    return [], error, profile
                finally:
                    raw_path.unlink(missing_ok=True)
                append_scan_log(
                    f"SANE flatbed complete bytes={output_path.stat().st_size}"
                )
                return [output_path], "", profile

            raw_path.unlink(missing_ok=True)
            append_scan_log(f"SANE flatbed failed: {compact_details(error, 500)}")
            return [], error, profile

        # PNM is intentionally used for the ADF. It is the universally
        # supported scanimage format and avoids per-page PNG compression.
        command = [
            *base_command,
            "--buffer-size=1024",
            "--format=pnm",
            f"--batch={scan_dir / 'raw-%04d.pnm'}",
            "--batch-start=1",
            "--batch-increment=1",
            f"--batch-count={MAX_ADF_PAGES}",
            "--batch-print",
        ]

        GLib.idle_add(
            self.set_status,
            "Быстрое сканирование всех листов из автоподатчика…",
        )

        started = time.monotonic()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        self.current_process = process
        output_lines: deque[str] = deque(maxlen=30)

        try:
            assert process.stdout is not None
            for line in process.stdout:
                line_text = line.rstrip()
                output_lines.append(line_text)
                if line_text:
                    is_progress = any(
                        marker in line_text.casefold()
                        for marker in (
                            "scanning page",
                            "scanned page",
                            "batch terminated",
                            "document feeder out of documents",
                        )
                    )
                    if is_progress:
                        append_scan_log(
                            "SANE ADF progress "
                            f"elapsed={time.monotonic() - started:.2f}s "
                            f"message={compact_details(line_text, 300)}"
                        )
                        GLib.idle_add(
                            self.set_status,
                            translate_scan_status(line_text),
                        )

            return_code = process.wait()
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if self.current_process is process:
                self.current_process = None

        if self.cancel_requested:
            for raw_file in scan_dir.glob("raw-*.pnm"):
                raw_file.unlink(missing_ok=True)
            raise Naps3Error("Сканирование отменено пользователем.")

        raw_files = sorted(scan_dir.glob("raw-*.pnm"))
        if raw_files:
            GLib.idle_add(
                self.set_status,
                f"Получено страниц: {len(raw_files)}. Быстрая подготовка предпросмотра…",
            )
            targets = [
                scan_dir / f"page-{index:04d}.png"
                for index in range(1, len(raw_files) + 1)
            ]
            workers = 1 if dpi >= 600 else min(2, max(1, len(raw_files)))
            try:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers
                ) as executor:
                    futures = [
                        executor.submit(
                            self._convert_stream_document,
                            raw,
                            target,
                            software_lineart,
                        )
                        for raw, target in zip(raw_files, targets)
                    ]
                    files = [future.result() for future in futures]
            finally:
                for raw_file in raw_files:
                    raw_file.unlink(missing_ok=True)

            append_scan_log(
                "SANE ADF complete "
                f"pages={len(files)} rc={return_code} "
                f"seconds={time.monotonic() - started:.2f}"
            )
            return files, "", profile

        error = "\n".join(output_lines)
        append_scan_log(
            f"SANE ADF failed rc={return_code}: {compact_details(error, 800)}"
        )
        return [], error, profile

    def _convert_stream_document(
        self,
        source_path: Path,
        target_path: Path,
        lineart: bool = False,
    ) -> Path:
        temporary = target_path.with_name(
            f".{target_path.name}.{uuid.uuid4().hex}.part"
        )
        image = None
        try:
            with Image.open(source_path) as source_image:
                width, height = source_image.size
                pixel_count = width * height
                if width <= 0 or height <= 0 or pixel_count > MAX_SCAN_PIXELS:
                    raise Naps3Error(
                        "Сканер передал недопустимый размер изображения: "
                        f"{width} × {height} пикс."
                    )
                # Fully decode the stream before GTK sees it. Pillow raises
                # here for a truncated PNM, PNG, JPEG or TIFF page.
                source_image.load()
                image = ImageOps.exif_transpose(source_image).copy()

            if lineart:
                grayscale = ImageOps.grayscale(image)
                image.close()
                image = grayscale.point(
                    [0] * 128 + [255] * 128,
                    mode="1",
                )
                grayscale.close()
            elif image.mode not in {"RGB", "L"}:
                converted = image.convert("RGB")
                image.close()
                image = converted

            # Publish only a complete PNG. If conversion or disk I/O fails,
            # an existing target stays intact and GTK never sees a partial file.
            image.save(temporary, format="PNG", compress_level=2)
            image.close()
            image = None
            if not temporary.exists() or temporary.stat().st_size == 0:
                raise Naps3Error(
                    f"Не удалось подготовить страницу {target_path.name}."
                )
            with Image.open(temporary) as prepared_image:
                prepared_image.load()
                if prepared_image.size != (width, height):
                    raise Naps3Error(
                        f"Не удалось проверить страницу {target_path.name}."
                    )
            os.replace(temporary, target_path)
            return target_path
        finally:
            if image is not None:
                image.close()
            temporary.unlink(missing_ok=True)

    def _scan_direct_escl(
        self,
        scan_dir: Path,
        profile: dict[str, object],
        source: str,
        mode: str,
        dpi: int,
        paper: str,
    ) -> tuple[list[Path], dict[str, object]]:
        """Scan ADF pages directly through eSCL with reliable job handling.

        Important details for HP M428/M429:
        * ScannerStatus may retain JobCompleted from the previous scan. That
          value is never treated as an empty feeder now.
        * ScannerStatus is matched to the exact current JobUri. A completed
          job left by an earlier scan can therefore never terminate this one.
        * ScannerAdfEmpty never ends the current job by itself: the feeder can
          become empty while a previously pulled page is still being encoded.
        * A completely drained network job is released. This HP M227 keeps an
          aborted/completed job as the active one otherwise and answers 409 or
          503 to the next scan, forcing the very slow SANE fallback.
        * If the device closes a job while the feeder still reports paper, a
          continuation ScanJob is created automatically.
        """
        base_url = profile_scan_url(profile)
        if not base_url:
            raise Naps3Error(
                "Для потокового режима не определён eSCL-адрес устройства."
            )

        settings_xml = build_escl_scan_settings(
            source,
            mode,
            dpi,
            paper,
        )

        raw_files: list[Path] = []
        page_number = 1
        maximum_jobs = 12
        scan_started = time.monotonic()
        append_scan_log(
            f"eSCL stream start url={base_url} source={source} dpi={dpi} mode={mode}"
        )

        for job_number in range(1, maximum_jobs + 1):
            if self.cancel_requested:
                raise Naps3Error("Сканирование отменено пользователем.")

            GLib.idle_add(
                self.set_status,
                "Создаётся потоковое задание АПД…"
                if job_number == 1
                else "Автоподатчик ещё содержит листы — продолжаем без ручного запуска…",
            )

            create_started = time.monotonic()
            create_attempt = 0
            cleanup_attempted = False
            while True:
                try:
                    job_url = create_escl_job(base_url, settings_xml)
                    break
                except Naps3Error as exc:
                    retryable = any(
                        marker in str(exc)
                        for marker in (
                            "HTTP 409",
                            "HTTP 423",
                            "HTTP 425",
                            "HTTP 429",
                            "HTTP 503",
                        )
                    )
                    if retryable and not self.cancel_requested:
                        create_attempt += 1
                        if not cleanup_attempted and any(
                            marker in str(exc)
                            for marker in ("HTTP 409", "HTTP 503")
                        ):
                            released = cleanup_terminal_escl_jobs(base_url)
                            cleanup_attempted = True
                            append_scan_log(
                                "eSCL create conflict; "
                                f"released_terminal_jobs={released}"
                            )
                        if time.monotonic() - create_started < 8.0:
                            # Do not hammer the printer with ~130 POSTs/sec.
                            time.sleep(min(0.18 + create_attempt * 0.07, 0.65))
                            continue
                    raise

            self.current_escl_job_url = job_url
            next_url = job_url.rstrip("/") + "/NextDocument"
            pages_before_job = len(raw_files)
            pending_since: Optional[float] = None
            last_scanner_status = ""
            last_status_check = 0.0
            last_pending_log = 0.0
            normal_job_finish = False

            append_scan_log(f"eSCL job#{job_number} created url={job_url}")

            try:
                while True:
                    if self.cancel_requested:
                        raise Naps3Error(
                            "Сканирование отменено пользователем."
                        )

                    request = urllib.request.Request(
                        next_url,
                        headers={
                            "Accept": "image/jpeg,image/png,image/tiff,*/*",
                            "User-Agent": f"NAPS3/{APP_VERSION}",
                            "Connection": "close",
                            "Cache-Control": "no-cache",
                        },
                    )
                    transient_reason = ""
                    transient_exc: Optional[BaseException] = None
                    partial_path: Optional[Path] = None
                    try:
                        page_started = time.monotonic()
                        # A bounded request makes Cancel responsive and avoids
                        # the firmware's several-minute blocked HTTP request.
                        # A timeout before a response is treated like Busy and
                        # NextDocument is retried without abandoning the job.
                        with _urlopen_scanner(request, 30.0) as response:
                            self.current_escl_response = response
                            try:
                                content_type = response.headers.get(
                                    "Content-Type", "image/jpeg"
                                ).split(";", 1)[0].strip().casefold()
                                extension = {
                                    "image/png": ".png",
                                    "image/tiff": ".tiff",
                                }.get(content_type, ".jpg")
                                raw_path = scan_dir / (
                                    f"stream-{page_number:04d}{extension}"
                                )
                                partial_path = raw_path.with_suffix(
                                    raw_path.suffix + ".part"
                                )
                                with partial_path.open("wb") as output_file:
                                    while True:
                                        if self.cancel_requested:
                                            raise Naps3Error(
                                                "Сканирование отменено "
                                                "пользователем."
                                            )
                                        chunk = response.read(1024 * 1024)
                                        if not chunk:
                                            break
                                        output_file.write(chunk)
                            finally:
                                if self.current_escl_response is response:
                                    self.current_escl_response = None

                            if (
                                not partial_path.exists()
                                or partial_path.stat().st_size < 128
                            ):
                                partial_path.unlink(missing_ok=True)
                                raise Naps3Error(
                                    "МФУ вернуло пустую страницу в потоковом режиме."
                                )
                            partial_path.replace(raw_path)
                    except urllib.error.HTTPError as exc:
                        status_code = exc.code
                        if status_code not in {
                            404,
                            409,
                            410,
                            423,
                            425,
                            429,
                            503,
                        }:
                            details = (
                                safe_decode(exc.read())
                                if hasattr(exc, "read")
                                else ""
                            )
                            raise Naps3Error(
                                "Ошибка eSCL при получении страницы: "
                                f"HTTP {status_code}. "
                                f"{compact_details(details, 300)}"
                            ) from exc
                        transient_reason = f"HTTP {status_code}"
                        transient_exc = exc
                    except (TimeoutError, socket.timeout) as exc:
                        transient_reason = "timeout"
                        transient_exc = exc
                    except urllib.error.URLError as exc:
                        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                            transient_reason = "timeout"
                            transient_exc = exc
                        else:
                            raise Naps3Error(
                                "Сетевое соединение со сканером прервано. "
                                f"{compact_details(exc, 300)}"
                            ) from exc
                    finally:
                        if partial_path is not None and partial_path.exists():
                            partial_path.unlink(missing_ok=True)

                    if transient_reason:
                        now = time.monotonic()
                        if pending_since is None:
                            pending_since = now
                        pending_for = now - pending_since

                        # ScannerStatus on this HP is authoritative. GET on
                        # the ScanJob itself reported Completed too early and
                        # caused page 2 to be abandoned. Match the exact JobUri
                        # so an older completed job cannot affect this scan.
                        if (
                            pending_for >= 0.6
                            and now - last_status_check >= 1.2
                        ):
                            last_scanner_status = scanner_status_text(
                                base_url,
                                0.6,
                            )
                            last_status_check = time.monotonic()

                        scanner_state = parse_escl_state(last_scanner_status)
                        current_job = find_escl_job(
                            last_scanner_status,
                            job_url,
                        )
                        images_to_transfer = (
                            current_job.images_to_transfer
                            if current_job is not None
                            else None
                        )
                        images_completed = (
                            current_job.images_completed
                            if current_job is not None
                            else None
                        )
                        received_this_job = len(raw_files) - pages_before_job

                        # A terminal job is drained only when the transfer
                        # counter is zero and every completed image has already
                        # been received. ScannerAdfEmpty alone is never enough.
                        if (
                            escl_job_is_drained(
                                current_job,
                                received_this_job,
                            )
                            and pending_for >= 0.5
                        ):
                            normal_job_finish = True
                            break

                        # Compatibility path for devices without counters.
                        # It runs only after a stable terminal state from the
                        # exact job, and leaves ample time for a final JPEG.
                        if (
                            current_job is not None
                            and current_job.is_terminal
                            and images_to_transfer is None
                            and pending_for >= 12.0
                            and received_this_job > 0
                        ):
                            normal_job_finish = True
                            break

                        if (
                            current_job is None
                            and transient_reason in {"HTTP 404", "HTTP 410"}
                            and scanner_state["adf_empty"]
                            and pending_for >= 12.0
                            and received_this_job > 0
                        ):
                            normal_job_finish = True
                            break

                        if (
                            pending_for >= 1.0
                            and now - last_pending_log >= 2.0
                        ):
                            append_scan_log(
                                "eSCL waiting NextDocument "
                                f"result={transient_reason} "
                                f"pending={pending_for:.1f}s "
                                f"adf_empty={scanner_state['adf_empty']} "
                                f"job_state={current_job.state if current_job else '-'} "
                                f"images_completed={images_completed} "
                                f"images_to_transfer={images_to_transfer} "
                                f"received={received_this_job}"
                            )
                            last_pending_log = now

                        if pending_for > 60.0:
                            raise Naps3Error(
                                "МФУ больше минуты не передаёт следующую "
                                "страницу. Задание остановлено без пятиминутного "
                                "ожидания."
                            ) from transient_exc

                        time.sleep(0.04 if pending_for < 2.0 else 0.10)
                        continue

                    raw_files.append(raw_path)
                    append_scan_log(
                        "eSCL page "
                        f"number={page_number} bytes={raw_path.stat().st_size} "
                        f"seconds={time.monotonic() - page_started:.2f}"
                    )
                    GLib.idle_add(
                        self.set_status,
                        f"Потоковый АПД: страница {page_number} получена; "
                        "следующая запрашивается сразу…",
                    )
                    page_number += 1
                    pending_since = None
                    last_scanner_status = ""
                    last_status_check = 0.0
                    last_pending_log = 0.0
            except Exception:
                cancel_escl_job(job_url)
                raise
            finally:
                self.current_escl_job_url = None

            pages_this_job = len(raw_files) - pages_before_job
            append_scan_log(
                f"eSCL job#{job_number} finished pages={pages_this_job} "
                f"normal={normal_job_finish}"
            )

            if pages_this_job == 0 and not raw_files:
                raise Naps3Error("В автоподатчике нет документов.")

            status_after_job = scanner_status_text(base_url, 0.8)
            state_after_job = parse_escl_state(status_after_job)
            previous_state = parse_escl_state(last_scanner_status)
            if previous_state["adf_loaded"]:
                state_after_job["adf_loaded"] = True
            if previous_state["adf_empty"]:
                state_after_job["adf_empty"] = True

            # Network HP M227 keeps a terminal job active and rejects the next
            # POST with 409/503 unless it is released after full draining.
            # This function is never used for the protected local USB
            # M428/M429 path, which remains on scanimage --batch.
            if normal_job_finish:
                released = cancel_escl_job(job_url)
                append_scan_log(
                    f"eSCL job#{job_number} released={released} after drain"
                )

            if (
                state_after_job["adf_loaded"]
                and not state_after_job["adf_empty"]
                and pages_this_job > 0
                and job_number < maximum_jobs
            ):
                append_scan_log(
                    f"eSCL continuation after job#{job_number}: ADF loaded"
                )
                time.sleep(0.03)
                continue

            break
        else:
            raise Naps3Error(
                "МФУ создало слишком много отдельных заданий для одного пакета."
            )

        if not raw_files:
            raise Naps3Error("МФУ не передало ни одной страницы.")

        GLib.idle_add(
            self.set_status,
            f"Получено страниц: {len(raw_files)}. Подготовка предпросмотра…",
        )
        targets = [
            scan_dir / f"page-{index:04d}.png"
            for index in range(1, len(raw_files) + 1)
        ]
        workers = 1 if dpi >= 600 else min(2, max(1, len(raw_files)))
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers
            ) as executor:
                futures = [
                    executor.submit(
                        self._convert_stream_document,
                        raw,
                        target,
                        mode == "Lineart",
                    )
                    for raw, target in zip(raw_files, targets)
                ]
                files = [future.result() for future in futures]
        finally:
            for raw_file in raw_files:
                raw_file.unlink(missing_ok=True)

        append_scan_log(
            "eSCL stream complete "
            f"pages={len(files)} seconds={time.monotonic() - scan_started:.2f}"
        )

        updated = dict(profile)
        updated["url"] = base_url
        updated["scan_engine"] = "direct-escl-stream-v4"
        updated["saved_at"] = int(time.time())
        return files, updated

    def _scan_worker(
        self,
        scan_dir: Path,
        match_filter: str,
        source: str,
        mode: str,
        dpi: int,
        paper: str,
        cached_profile: Optional[dict[str, object]],
        stream_adf: bool,
    ) -> tuple[list[Path], dict[str, object]]:
        if self.cancel_requested:
            raise Naps3Error("Сканирование отменено пользователем.")

        profile = (
            dict(cached_profile)
            if cached_profile is not None
            else discover_scanner_profile(match_filter)
        )

        if (
            not profile.get("device_id")
            and not profile_url(profile)
        ):
            raise Naps3Error(
                f"В профиле выбранного сканера «{profile_name(profile)}» "
                "нет адреса устройства. Выберите этот сканер заново."
            )

        if profile.get("backend") == "wia":
            error = ""
            for attempt in range(2):
                try:
                    files, error, profile = self._scan_windows_wia(
                        scan_dir,
                        profile,
                        source,
                        mode,
                        dpi,
                        paper,
                    )
                except WindowsBackendError as exc:
                    files, error = [], str(exc)
                if files:
                    return files, profile
                if self.cancel_requested:
                    raise Naps3Error("Сканирование отменено пользователем.")
                if attempt == 0 and self._retryable_device_error(error):
                    GLib.idle_add(
                        self.set_status,
                        "МФУ пробуждается; повтор WIA на выбранном устройстве…",
                    )
                    append_scan_log(
                        "WIA retry for exact selected device after: "
                        f"{compact_details(error, 600)}"
                    )
                    time.sleep(2.0)
                    continue
                break
            raise Naps3Error(friendly_scan_error(error, self.cancel_requested))

        if (
            IS_WINDOWS
            and str(profile.get("connection_kind") or "") == "network"
            and profile_scan_url(profile)
        ):
            # Windows packages do not include SANE. Direct eSCL keeps manual
            # network profiles working for both platen and feeder scans.
            return self._scan_direct_escl(
                scan_dir,
                profile,
                source,
                mode,
                dpi,
                paper,
            )

        if stream_adf and source != "Flatbed":
            direct_url = profile_scan_url(profile)
            connection_kind = str(
                profile.get("connection_kind") or "usb"
            )
            direct_host = ""
            if direct_url:
                direct_host = urllib.parse.urlsplit(direct_url).hostname or ""
            is_local_usb = (
                connection_kind in {"usb", "usb-hpaio", "usb-sane"}
                or direct_host in {"127.0.0.1", "localhost", "::1"}
            )

            if is_local_usb:
                # This HP M428/M429 was verified to physically feed multiple
                # sheets through direct localhost eSCL while returning only
                # one image on repeated scans. The local USB path therefore
                # always uses scanimage --batch. Version 0.5.0 makes that path
                # fast by writing PNM during feeding and converting afterwards.
                GLib.idle_add(
                    self.set_status,
                    "USB-АПД: надёжный быстрый пакетный режим…",
                )
                append_scan_log(
                    "USB profile: direct eSCL bypassed; using fast SANE batch"
                )
            elif direct_url:
                try:
                    return self._scan_direct_escl(
                        scan_dir,
                        profile,
                        source,
                        mode,
                        dpi,
                        paper,
                    )
                except Naps3Error as exc:
                    if any(
                        marker in str(exc)
                        for marker in (
                            "HTTP 409",
                            "HTTP 423",
                            "HTTP 503",
                            "больше минуты не передаёт",
                        )
                    ):
                        append_scan_log(
                            "Network accelerated scan stopped without slow "
                            f"SANE fallback: {compact_details(exc, 600)}"
                        )
                        raise Naps3Error(
                            "Ускоренное сетевое сканирование не "
                            "запустилось. Медленный режим не был "
                            "включён автоматически. Проверьте бумагу в АПД "
                            "и повторите запуск."
                        ) from exc
                    GLib.idle_add(
                        self.set_status,
                        "Потоковый eSCL недоступен; используется совместимый SANE-режим…",
                    )
                    append_scan_log(
                        f"Network direct eSCL fallback: {compact_details(exc, 600)}"
                    )
                    print(f"NAPS3 direct eSCL fallback: {exc}", file=sys.stderr)

        files, error, profile = self._scan_once(
            scan_dir,
            profile,
            source,
            mode,
            dpi,
            paper,
            force_resolve_device=(
                not bool(profile.get("device_id"))
                and bool(profile_url(profile))
            ),
        )
        if files:
            return files, profile

        if self.cancel_requested:
            raise Naps3Error("Сканирование отменено пользователем.")

        connection_kind = str(profile.get("connection_kind") or "")
        if (
            self._device_busy_error(error)
            and connection_kind in {"usb", "usb-hpaio", "usb-sane"}
        ):
            GLib.idle_add(
                self.set_status,
                "Сканер освобождается; повтор на выбранном устройстве…",
            )
            append_scan_log(
                "SANE busy retry for exact selected device "
                f"name={profile_name(profile)} "
                f"device={profile.get('device_id', '')}: "
                f"{compact_details(error, 600)}"
            )
            time.sleep(1.0)
            if self.cancel_requested:
                raise Naps3Error("Сканирование отменено пользователем.")
            files, second_error, profile = self._scan_once(
                scan_dir,
                profile,
                source,
                mode,
                dpi,
                paper,
                force_resolve_device=False,
            )
            if files:
                return files, profile
            error = second_error

        if self.cancel_requested:
            raise Naps3Error("Сканирование отменено пользователем.")

        recovery_attempted = False
        if (
            self._retryable_device_error(error)
            and bool(profile_url(profile))
        ):
            recovery_attempted = True
            GLib.idle_add(
                self.set_status,
                "Обновляется локальный адрес сохранённого сканера…",
            )
            try:
                files, second_error, profile = self._scan_once(
                    scan_dir,
                    profile,
                    source,
                    mode,
                    dpi,
                    paper,
                    force_resolve_device=True,
                )
                if files:
                    return files, profile
                error = second_error
            except Naps3Error as exc:
                error = str(exc)

        if self.cancel_requested:
            raise Naps3Error("Сканирование отменено пользователем.")

        # Keep recovery tied to the selected endpoint. A general discovery
        # here could turn a failed Kyocera scan into an unrelated HP scan.
        # Explicit device selection remains available in the main window.
        if (
            recovery_attempted
            or self._retryable_device_error(error)
            or "профил" in error.casefold()
            or "sane-airscan" in error.casefold()
            or "локальн" in error.casefold()
        ):
            append_scan_log(
                "Recovery stopped for selected scanner "
                f"name={profile_name(profile)} "
                f"device={profile.get('device_id', '')}: "
                f"{compact_details(error, 600)}"
            )
            raise Naps3Error(
                "Не удалось восстановить соединение с выбранным сканером "
                f"«{profile_name(profile)}». Проверьте его готовность и "
                "подключение. При необходимости выберите это устройство "
                "заново.\n\n"
                f"{friendly_scan_error(error, self.cancel_requested)}"
            )

        raise Naps3Error(
            friendly_scan_error(error, self.cancel_requested)
        )

    def _scan_ready(
        self,
        files: list[Path],
        error: str,
        profile: dict[str, object],
        warning: str = "",
    ) -> bool:
        self.set_busy(False)
        self.current_process = None
        self.current_escl_job_url = None
        self.current_escl_response = None

        if self.cancel_requested:
            self.cancel_requested = False
            append_scan_log("READY cancelled; partial scan discarded")
            self.set_status("Сканирование отменено")
            return False

        if error:
            self.set_status("Сканирование не выполнено")
            self.show_error("Ошибка сканирования", error)
            return False

        if profile:
            self.scanner_profile = dict(profile)
            self._update_source_options(self.scanner_profile)
            try:
                self._save_ui_settings()
            except OSError:
                pass

        self.add_pages(files)
        append_scan_log(
            f"READY added_pages={len(files)} project_pages={len(self.pages)}"
        )
        self.set_status(
            f"Сканирование завершено. Добавлено страниц: {len(files)}. "
            "Профиль устройства сохранён для следующего запуска."
        )
        if warning:
            self.show_info("Ручной дуплекс", warning)
        return False

    def cancel_operation(self) -> None:
        if not self.is_busy:
            return

        self.cancel_requested = True
        self.set_status("Операция отменяется…")

        response = self.current_escl_response
        job_url = self.current_escl_job_url
        if response is not None or job_url:
            threading.Thread(
                target=cancel_escl_io,
                args=(response, job_url or ""),
                daemon=True,
            ).start()

        process = self.current_process
        if process and process.poll() is None:
            threading.Thread(
                target=terminate_subprocess,
                args=(process,),
                daemon=True,
            ).start()

    # ------------------------ page project -------------------------

    def add_pages(self, files: list[Path]) -> None:
        start_index = len(self.pages)
        for path in files:
            self.pages.append(Page(path=path, label=path.name))

        if files:
            self.selected_index = start_index
            self.document_revision += 1

        self.rebuild_page_list()
        self.render_preview()
        self._update_page_actions()

    def rebuild_page_list(self) -> None:
        # Removing the selected Gtk.ListBoxRow emits row-selected(None).
        # Preserve the logical selection across rebuilding thumbnails.
        selected_index = self.selected_index
        for child in self.page_list.get_children():
            self.page_list.remove(child)

        for index, page in enumerate(self.pages):
            row = PageRow(page, index)
            self.page_list.add(row)

        self.page_list.show_all()
        self.page_count_label.set_text(str(len(self.pages)))

        self.selected_index = selected_index

        if (
            self.selected_index is not None
            and 0 <= self.selected_index < len(self.pages)
        ):
            row = self.page_list.get_row_at_index(self.selected_index)
            if row:
                self.page_list.select_row(row)

    def _on_page_selected(
        self,
        _list_box: Gtk.ListBox,
        row: Optional[Gtk.ListBoxRow],
    ) -> None:
        if row is None:
            self.selected_index = None
        else:
            self.selected_index = row.get_index()

        self.render_preview()
        self._update_page_actions()

    def _update_page_actions(self) -> None:
        has_pages = bool(self.pages)
        has_selection = (
            self.selected_index is not None
            and 0 <= self.selected_index < len(self.pages)
        )
        self.save_button.set_sensitive(has_pages and not self.is_busy)

        for widget in self.busy_widgets:
            widget.set_sensitive(not self.is_busy)

        if not has_selection:
            self.preview_title.set_markup("<b>Предпросмотр</b>")
            self.preview_info.set_text("")

    def render_preview(self) -> None:
        has_selection = (
            self.selected_index is not None
            and 0 <= self.selected_index < len(self.pages)
        )

        self.empty_preview.set_visible(not has_selection)

        if not has_selection:
            self.preview_image.clear()
            return

        assert self.selected_index is not None
        page = self.pages[self.selected_index]

        if page.path in self._preview_failures:
            self.preview_image.clear()
            self.preview_title.set_markup(
                f"<b>Предпросмотр — страница {self.selected_index + 1}</b>"
            )
            self.preview_info.set_text(
                "Файл страницы повреждён или записан не полностью."
            )
            return

        try:
            width = max(self.preview_event.get_allocated_width() - 50, 180)
            height = max(self.preview_event.get_allocated_height() - 50, 180)

            if self.zoom_factor <= 0:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(page.path),
                    width,
                    height,
                    True,
                )
                self.zoom_label.set_text("По размеру окна")
            else:
                original = GdkPixbuf.Pixbuf.new_from_file(str(page.path))
                target_width = max(
                    100,
                    int(original.get_width() * self.zoom_factor),
                )
                target_height = max(
                    100,
                    int(original.get_height() * self.zoom_factor),
                )
                pixbuf = original.scale_simple(
                    target_width,
                    target_height,
                    GdkPixbuf.InterpType.BILINEAR,
                )
                self.zoom_label.set_text(
                    f"{int(self.zoom_factor * 100)}%"
                )

            self.preview_image.set_from_pixbuf(pixbuf)
            self.preview_title.set_markup(
                f"<b>Предпросмотр — страница {self.selected_index + 1}</b>"
            )

            with Image.open(page.path) as source_image:
                self.preview_info.set_text(
                    f"{source_image.width} × {source_image.height} пикс."
                )
            self._preview_failures.discard(page.path)
        except (GLib.Error, OSError) as exc:
            self.preview_image.clear()
            first_failure = page.path not in self._preview_failures
            self._preview_failures.add(page.path)
            self.preview_title.set_markup(
                f"<b>Предпросмотр — страница {self.selected_index + 1}</b>"
            )
            self.preview_info.set_text(
                "Файл страницы повреждён или записан не полностью."
            )
            if first_failure:
                append_scan_log(
                    "Preview rejected damaged page "
                    f"path={page.path.name}: {compact_details(exc, 400)}"
                )
                self.set_status(
                    "Повреждённая страница не показана. Повторите сканирование."
                )

    @property
    def zoom_factor(self) -> float:
        return getattr(self, "_zoom_factor", 0.0)

    @zoom_factor.setter
    def zoom_factor(self, value: float) -> None:
        self._zoom_factor = value

    def change_zoom(self, delta: float) -> None:
        if self.selected_index is None:
            return

        if self.zoom_factor <= 0:
            self.zoom_factor = 0.5 if delta > 0 else 0.25
        else:
            self.zoom_factor = min(
                2.0,
                max(0.1, self.zoom_factor + delta),
            )

        self.render_preview()

    def _on_preview_size_allocate(
        self,
        _widget: Gtk.Widget,
        _allocation: Gdk.Rectangle,
    ) -> None:
        if self.zoom_factor <= 0:
            GLib.idle_add(self.render_preview)

    def rotate_selected(self, degrees: int) -> None:
        if self.is_busy or self.selected_index is None:
            return

        page = self.pages[self.selected_index]

        try:
            with Image.open(page.path) as source_image:
                image = ImageOps.exif_transpose(source_image).copy()
            image = image.rotate(-degrees, expand=True)
            image.save(page.path, format="PNG")
            image.close()
            self.document_revision += 1

            self.rebuild_page_list()
            self.render_preview()
            self.set_status("Страница повернута")
        except Exception as exc:  # noqa: BLE001
            self.show_error(
                "Ошибка поворота",
                friendly_general_error(exc, "повернуть страницу"),
            )

    def move_selected(self, delta: int) -> None:
        if self.is_busy or self.selected_index is None:
            return

        new_index = self.selected_index + delta
        if not 0 <= new_index < len(self.pages):
            return

        self.pages[self.selected_index], self.pages[new_index] = (
            self.pages[new_index],
            self.pages[self.selected_index],
        )
        self.selected_index = new_index
        self.document_revision += 1
        self.rebuild_page_list()
        self.render_preview()

    def delete_selected(self) -> None:
        if self.is_busy or self.selected_index is None:
            return

        index = self.selected_index
        self.pages.pop(index)
        self.document_revision += 1

        if not self.pages:
            self.selected_index = None
        else:
            self.selected_index = min(index, len(self.pages) - 1)

        self.rebuild_page_list()
        self.render_preview()
        self._update_page_actions()
        self.set_status("Страница удалена из проекта")

    def clear_pages(self) -> None:
        if self.is_busy or not self.pages:
            return

        if not self.ask_yes_no(
            "Очистить проект",
            "Удалить все страницы из текущего проекта?",
        ):
            return

        self.pages.clear()
        self.document_revision += 1
        self.selected_index = None
        self.rebuild_page_list()
        self.render_preview()
        self._update_page_actions()
        self.set_status("Проект очищен")

    # --------------------------- import ----------------------------

    def import_images(self) -> None:
        if self.is_busy:
            return

        dialog = Gtk.FileChooserDialog(
            title="Импорт изображений",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            "Отмена",
            Gtk.ResponseType.CANCEL,
            "Импортировать",
            Gtk.ResponseType.OK,
        )
        dialog.set_select_multiple(True)

        image_filter = Gtk.FileFilter()
        image_filter.set_name("Изображения")
        for pattern in (
            "*.png",
            "*.jpg",
            "*.jpeg",
            "*.tif",
            "*.tiff",
            "*.bmp",
            "*.webp",
        ):
            image_filter.add_pattern(pattern)
        dialog.add_filter(image_filter)

        all_filter = Gtk.FileFilter()
        all_filter.set_name("Все файлы")
        all_filter.add_pattern("*")
        dialog.add_filter(all_filter)

        response = dialog.run()
        filenames = dialog.get_filenames()
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not filenames:
            return

        imported: list[Path] = []
        warnings: list[str] = []

        for filename in filenames:
            source = Path(filename)
            try:
                with Image.open(source) as source_image:
                    image = ImageOps.exif_transpose(source_image).copy()

                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")

                target = self.session_dir / f"import-{uuid.uuid4().hex}.png"
                image.save(target, format="PNG")
                image.close()
                imported.append(target)
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"{source.name}: "
                    f"{friendly_general_error(exc, 'импортировать изображение')}"
                )

        self.add_pages(imported)

        if imported:
            self.set_status(
                f"Импортировано изображений: {len(imported)}"
            )

        if warnings:
            self.show_error(
                "Некоторые файлы не импортированы",
                "\n\n".join(warnings[:5]),
            )

    def import_pdf(self) -> None:
        if self.is_busy:
            return

        pdf_converter = find_runtime_executable("pdftoppm")
        if pdf_converter is None:
            self.show_error(
                "Импорт PDF недоступен",
                (
                    "Переустановите NAPS3 для Windows: в пакете отсутствует pdftoppm."
                    if IS_WINDOWS
                    else "Не установлен компонент pdftoppm. Установите пакет poppler-utils."
                ),
            )
            return

        dialog = Gtk.FileChooserDialog(
            title="Импорт PDF",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            "Отмена",
            Gtk.ResponseType.CANCEL,
            "Импортировать",
            Gtk.ResponseType.OK,
        )

        pdf_filter = Gtk.FileFilter()
        pdf_filter.set_name("Документы PDF")
        pdf_filter.add_pattern("*.pdf")
        dialog.add_filter(pdf_filter)

        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()

        if response != Gtk.ResponseType.OK or not filename:
            return

        import_dir = self.session_dir / f"pdf-{uuid.uuid4().hex}"
        import_dir.mkdir(parents=True, exist_ok=True)
        prefix = import_dir / "page"

        self.set_busy(True, "Импорт PDF…")
        converter_env = None
        if IS_WINDOWS:
            converter_env = {
                **os.environ,
                "FONTCONFIG_PATH": str(resource_path("etc", "fonts")),
            }

        def worker() -> None:
            try:
                result = subprocess.run(
                    [
                        pdf_converter,
                        "-png",
                        "-r",
                        "160",
                        filename,
                        str(prefix),
                    ],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=300,
                    check=False,
                    env=converter_env,
                )
                files = sorted(import_dir.glob("page-*.png"))
                if not files:
                    raise Naps3Error(
                        "PDF не удалось преобразовать в изображения.\n\n"
                        f"Технические сведения: "
                        f"{compact_details(result.stderr)}"
                    )
                GLib.idle_add(self._import_pdf_ready, files, "")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._import_pdf_ready,
                    [],
                    friendly_general_error(exc, "импортировать PDF"),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _import_pdf_ready(
        self,
        files: list[Path],
        error: str,
    ) -> bool:
        self.set_busy(False)

        if error:
            self.set_status("PDF не импортирован")
            self.show_error("Ошибка импорта PDF", error)
            return False

        self.add_pages(files)
        self.set_status(
            f"PDF импортирован. Добавлено страниц: {len(files)}"
        )
        return False

    # --------------------------- export ----------------------------

    def export_pages(self, close_after_save: bool = False) -> None:
        if self.is_busy:
            return

        if not self.pages:
            self.show_info(
                "Сохранение",
                "В проекте нет страниц для сохранения.",
            )
            return

        dialog = ExportDialog(
            self,
            len(self.pages),
            self.selected_index is not None,
        )
        response = dialog.run()
        options = dialog.get_result()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return

        mode = str(options["mode"])
        image_format = str(options["format"])
        quality = int(options["quality"])
        dpi = int(self.dpi_combo.get_active_id() or "300")
        stamp = time.strftime("%Y%m%d-%H%M%S")

        job: dict[str, object] = {
            "mode": mode,
            "format": image_format,
            "quality": quality,
            "dpi": dpi,
        }

        if mode == "pdf":
            output = self.choose_save_file(
                "Сохранить PDF",
                f"Скан-{stamp}.pdf",
                ".pdf",
                "Документ PDF",
            )
            if not output:
                return
            job["output"] = output

        elif mode == "tiff_multi":
            output = self.choose_save_file(
                "Сохранить многостраничный TIFF",
                f"Скан-{stamp}.tiff",
                ".tiff",
                "Изображение TIFF",
            )
            if not output:
                return
            job["output"] = output

        elif mode == "selected_image":
            if self.selected_index is None:
                self.show_info(
                    "Сохранение",
                    "Сначала выберите страницу.",
                )
                return

            info = FORMAT_INFO[image_format]
            output = self.choose_save_file(
                "Сохранить выбранную страницу",
                f"Страница-{self.selected_index + 1}{info['extension']}",
                str(info["extension"]),
                str(info["label"]),
            )
            if not output:
                return

            job["output"] = output
            job["page_index"] = self.selected_index

        elif mode == "images_all":
            output_dir = self.choose_folder(
                "Выберите папку для отдельных файлов"
            )
            if not output_dir:
                return

            base_name = self.ask_text(
                "Имена файлов",
                "Введите основу имени. Файлы получат номера 001, 002 и далее:",
                f"Скан-{stamp}",
            )
            if base_name is None:
                return

            safe_name = re.sub(
                r'[\\/:*?"<>|]+',
                "_",
                base_name,
            ).strip(" .")

            if not safe_name:
                self.show_error(
                    "Некорректное имя",
                    "Введите непустое имя без служебных символов.",
                )
                return

            info = FORMAT_INFO[image_format]
            outputs = [
                output_dir
                / f"{safe_name}-{index:03d}{info['extension']}"
                for index in range(1, len(self.pages) + 1)
            ]
            existing = [path for path in outputs if path.exists()]

            if existing and not self.ask_yes_no(
                "Файлы уже существуют",
                "В выбранной папке уже существуют файлы с такими именами: "
                f"{len(existing)}. Перезаписать их?",
            ):
                return

            job["output_dir"] = output_dir
            job["outputs"] = outputs

        else:
            self.show_error(
                "Сохранение",
                "Выбран неизвестный режим сохранения.",
            )
            return

        outputs = (
            [Path(item) for item in job["outputs"]]
            if mode == "images_all" else [Path(job["output"])]
        )
        try:
            job["target_states"] = {
                path: export_target_state(path) for path in outputs
            }
        except (OSError, Naps3Error) as exc:
            self.show_error(
                "Ошибка сохранения", friendly_general_error(exc, "сохранить сканы")
            )
            return
        # The worker never depends on the live list or its current selection.
        job["pages"] = tuple(Page(page.path, page.label) for page in self.pages)
        job["document_revision"] = self.document_revision
        job["close_after_save"] = close_after_save
        self.is_exporting = True
        self.set_busy(True, "Сохранение страниц…")

        def worker() -> None:
            try:
                result = self._export_worker(job)
                GLib.idle_add(self._export_ready, result, "")
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(
                    self._export_ready,
                    {},
                    friendly_general_error(exc, "сохранить сканы"),
                )

        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError as exc:
            self._export_ready({}, friendly_general_error(exc, "начать сохранение"))

    def choose_save_file(
        self,
        title: str,
        default_name: str,
        extension: str,
        label: str,
    ) -> Optional[Path]:
        with FileChooserSortGuard():
            dialog = Gtk.FileChooserDialog(
                title=title,
                transient_for=self,
                action=Gtk.FileChooserAction.SAVE,
            )
            dialog.add_buttons(
                "Отмена",
                Gtk.ResponseType.CANCEL,
                "Сохранить",
                Gtk.ResponseType.OK,
            )
            dialog.set_do_overwrite_confirmation(True)
            dialog.set_current_name(default_name)

            file_filter = Gtk.FileFilter()
            file_filter.set_name(label)
            file_filter.add_pattern(f"*{extension}")
            dialog.add_filter(file_filter)

            response = dialog.run()
            filename = dialog.get_filename()
            dialog.destroy()

        if response != Gtk.ResponseType.OK or not filename:
            return None

        output = Path(filename)
        if output.suffix.casefold() != extension.casefold():
            output = output.with_suffix(extension)
            if output.exists() and not self.ask_yes_no(
                "Файл уже существует",
                f"После добавления расширения выбран файл:\n{output}\n\n"
                "Перезаписать его?",
            ):
                return None
        return output

    def choose_folder(self, title: str) -> Optional[Path]:
        with FileChooserSortGuard():
            dialog = Gtk.FileChooserDialog(
                title=title,
                transient_for=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            dialog.add_buttons(
                "Отмена",
                Gtk.ResponseType.CANCEL,
                "Выбрать",
                Gtk.ResponseType.OK,
            )
            response = dialog.run()
            filename = dialog.get_filename()
            dialog.destroy()

        if response != Gtk.ResponseType.OK or not filename:
            return None
        return Path(filename)

    def ask_text(
        self,
        title: str,
        prompt: str,
        initial: str,
    ) -> Optional[str]:
        dialog = Gtk.Dialog(
            title=title,
            transient_for=self,
            modal=True,
        )
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Продолжить", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_border_width(16)

        label = Gtk.Label(label=prompt)
        label.set_line_wrap(True)
        label.set_xalign(0)
        box.pack_start(label, False, False, 0)

        entry = Gtk.Entry()
        entry.set_text(initial)
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        value = entry.get_text().strip()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return None
        return value

    def _load_export_image(
        self,
        page: Page,
        target_format: str,
    ) -> Image.Image:
        with Image.open(page.path) as source_image:
            image = ImageOps.exif_transpose(source_image).copy()

        if (
            target_format in {"PDF", "JPEG", "BMP", "TIFF"}
            and image.mode not in {"RGB", "L"}
        ):
            image = image.convert("RGB")

        if target_format == "WEBP" and image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")

        return image

    def _save_image(
        self,
        page: Page,
        output: Path,
        image_format: str,
        quality: int,
        dpi: int,
        title: Optional[str] = None,
    ) -> None:
        info = FORMAT_INFO[image_format]
        pillow_format = str(info["pillow"])
        image = self._load_export_image(page, pillow_format)

        try:
            options: dict[str, object]
            if image_format == "PDF":
                # Each page becomes an independent one-page PDF. Pillow uses
                # ``resolution`` for the physical page size of PDF output.
                options = {
                    "resolution": float(dpi),
                    "title": title or output.stem,
                    "quality": 95,
                    "optimize": True,
                }
            else:
                options = {"dpi": (dpi, dpi)}
                if image_format in {"JPEG", "WEBP"}:
                    options["quality"] = quality
                if image_format == "PNG":
                    options["optimize"] = True
                if image_format == "TIFF":
                    options["compression"] = "tiff_deflate"

            image.save(
                output,
                format=pillow_format,
                **options,
            )
        finally:
            image.close()

    def _export_worker(
        self,
        job: dict[str, object],
    ) -> dict[str, object]:
        mode = str(job["mode"])
        quality = int(job["quality"])
        dpi = int(job["dpi"])
        pages = tuple(job["pages"]) if "pages" in job else tuple(self.pages)
        if not pages:
            raise Naps3Error("В проекте нет страниц для сохранения.")
        if mode not in {"pdf", "tiff_multi", "selected_image", "images_all"}:
            raise Naps3Error("Неизвестный режим сохранения.")
        outputs = (
            [Path(item) for item in job["outputs"]]
            if mode == "images_all" else [Path(job["output"])]
        )
        if mode == "images_all" and len(outputs) != len(pages):
            raise Naps3Error("Количество файлов не совпадает с числом страниц.")
        source_paths = {page.path.resolve() for page in pages}
        source_files = set()
        for page in pages:
            try:
                value = page.path.stat()
            except FileNotFoundError:
                # A missing unselected page must not prevent exporting a
                # different page that is still available.
                continue
            source_files.add((value.st_dev, value.st_ino))
        for output in outputs:
            target = output.stat() if output.exists() else None
            if output.resolve() in source_paths or (
                target is not None and (target.st_dev, target.st_ino) in source_files
            ):
                raise Naps3Error(
                    "Нельзя сохранить результат поверх рабочей страницы "
                    f"«{output}». Выберите другое имя или папку."
                )

        with staged_export_files(outputs, job.get("target_states")) as staged:
            if mode in {"pdf", "tiff_multi"}:
                pillow_format = "PDF" if mode == "pdf" else "TIFF"
                images: list[Image.Image] = []
                try:
                    for page in pages:
                        images.append(self._load_export_image(page, pillow_format))
                    first, *rest = images
                    options = (
                        {
                            "resolution": float(dpi), "quality": 95,
                            "optimize": True, "title": outputs[0].stem,
                        }
                        if mode == "pdf"
                        else {"compression": "tiff_deflate", "dpi": (dpi, dpi)}
                    )
                    first.save(
                        staged[0], pillow_format, save_all=True,
                        append_images=rest, **options,
                    )
                finally:
                    for image in images:
                        image.close()
            elif mode == "selected_image":
                self._save_image(
                    pages[int(job["page_index"])], staged[0],
                    str(job["format"]), quality, dpi, title=outputs[0].stem,
                )
            else:
                for page, output, destination in zip(pages, staged, outputs):
                    self._save_image(
                        page, output, str(job["format"]), quality, dpi,
                        title=destination.stem,
                    )

        messages = {
            "pdf": "PDF успешно сохранён.",
            "tiff_multi": "Многостраничный TIFF успешно сохранён.",
            "selected_image": "Изображение успешно сохранено.",
            "images_all": f"Сохранено отдельных файлов: {len(outputs)}.",
        }
        return {
            "kind": "folder" if mode == "images_all" else "file",
            "path": Path(job["output_dir"]) if mode == "images_all" else outputs[0],
            "message": messages[mode],
            "document_revision": job.get("document_revision"),
            "saves_document": mode != "selected_image" or len(pages) == 1,
            "close_after_save": bool(job.get("close_after_save", False)),
        }

    def _export_ready(
        self,
        result: dict[str, object],
        error: str,
    ) -> bool:
        self.is_exporting = False
        self.set_busy(False)

        if error:
            self.set_status("Сканы не сохранены")
            self.show_error("Ошибка сохранения", error)
            return False

        if result.get("saves_document") and result.get("document_revision") is not None:
            self.saved_revision = int(result["document_revision"])
        if result.get("close_after_save") and not self.has_unsaved_pages():
            self.close()
            return False

        path = Path(result["path"])
        message = str(result["message"])
        object_name = (
            "папку"
            if result["kind"] == "folder"
            else "файл"
        )

        self.set_status("Сохранение завершено")

        if self.ask_yes_no(
            "Сохранение завершено",
            f"{message}\n\n{path}\n\nОткрыть {object_name}?",
        ):
            try:
                if IS_WINDOWS:
                    os.startfile(str(path))
                else:
                    subprocess.Popen(
                        ["xdg-open", str(path)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            except OSError as exc:
                self.show_error(
                    "Не удалось открыть результат",
                    friendly_general_error(exc, "открыть сохранённый результат"),
                )

        return False

    # -------------------------- state ------------------------------

    def set_status(self, message: str) -> bool:
        self.status_label.set_text(message)
        return False

    def set_busy(
        self,
        busy: bool,
        status: str = "",
        cancellable: bool = False,
    ) -> None:
        self.is_busy = busy
        self.scan_button.set_sensitive(not busy)
        self.cancel_button.set_visible(busy and cancellable)

        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

        for widget in self.busy_widgets:
            widget.set_sensitive(not busy)

        self.save_button.set_sensitive(
            bool(self.pages) and not busy
        )

        if status:
            self.set_status(status)

    def has_unsaved_pages(self) -> bool:
        return bool(self.pages) and self.document_revision != self.saved_revision

    def _confirm_unsaved_close(self) -> int:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Сохранить изменения перед закрытием?",
        )
        dialog.format_secondary_text(
            "В документе есть несохранённые страницы или изменения. "
            "Сохраните весь документ, чтобы сохранить также порядок страниц."
        )
        dialog.add_buttons(
            "Отмена", Gtk.ResponseType.CANCEL,
            "Закрыть без сохранения", Gtk.ResponseType.REJECT,
            "Сохранить…", Gtk.ResponseType.ACCEPT,
        )
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        return response

    def do_delete_event(self, _event: Gdk.Event) -> bool:
        if self.is_exporting:
            self.show_info(
                "Сохранение выполняется",
                "Дождитесь завершения сохранения, затем закройте приложение.",
            )
            return True

        if self.is_busy:
            message = "Операция ещё выполняется. Остановить её и закрыть приложение?"
            if self.has_unsaved_pages():
                message += "\n\nВ документе есть несохранённые страницы или изменения."
            if not self.ask_yes_no("Закрытие NAPS3", message):
                return True
        elif self.has_unsaved_pages():
            response = self._confirm_unsaved_close()
            if response == Gtk.ResponseType.ACCEPT:
                self.export_pages(close_after_save=True)
                return True
            if response != Gtk.ResponseType.REJECT:
                return True

        self.cancel_operation()

        try:
            self._save_ui_settings()
        except OSError:
            pass

        return False


class Naps3Application(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.window: Optional[MainWindow] = None
        self._settings_lock: Optional[object] = None

    def do_startup(self) -> None:
        try:
            self._settings_lock = acquire_settings_lock(blocking=True)
        except OSError as exc:
            # A stale root-owned config must not prevent the scanner UI from
            # opening. Saving will still report the underlying permissions error.
            append_scan_log(
                "Settings lock unavailable for GUI: "
                f"{compact_details(exc, 400)}"
            )
        Gtk.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname("naps3")
        Gtk.Window.set_default_icon_name("naps3")

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)

        self.window.show_all()
        self.window.cancel_button.hide()
        self.window.present()

    def do_shutdown(self) -> None:
        try:
            release_settings_lock(self._settings_lock)
            self._settings_lock = None
        finally:
            Gtk.Application.do_shutdown(self)


def check_runtime() -> Optional[str]:
    missing: list[str] = []

    if IS_WINDOWS:
        try:
            result = run_wia_bridge("selftest", timeout=8.0)
            if not isinstance(result, dict) or not result.get("ok"):
                missing.append("компонент Windows WIA")
        except WindowsBackendError as exc:
            missing.append(f"компонент Windows WIA ({exc})")
        if find_runtime_executable("pdftoppm") is None:
            missing.append("pdftoppm для импорта PDF")
    elif shutil.which("scanimage") is None:
        missing.append("scanimage из пакета sane-backends")

    if missing:
        return "Не найдены обязательные компоненты:\n• " + "\n• ".join(missing)

    return None


def emit_registration_result(
    payload: dict[str, object],
    *,
    json_output: bool,
    error: bool = False,
) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    stream = sys.stderr if error else sys.stdout
    if error:
        print(f"NAPS3: {payload['message']}", file=stream)
        return
    profile = dict(payload["profile"])
    print(f"Профиль NAPS3 сохранён: {profile.get('name', 'Сканер')}")
    print(f"Подключение: {profile.get('transport', 'не определено')}")
    if payload.get("backup"):
        print(f"Резервная копия: {payload['backup']}")


def run_registration_cli(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="naps3 --register-scanner",
        description="Проверить сканер и сохранить его рабочим устройством NAPS3.",
    )
    parser.add_argument("--register-scanner", action="store_true", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--device-id", default="", metavar="SANE_ID")
    target.add_argument("--address", default="", metavar="IP_OR_URL")
    parser.add_argument("--name", default="", metavar="NAME")
    parser.add_argument(
        "--connection",
        choices=("usb", "network"),
        default="",
        help="Тип подключения для профиля по SANE ID.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    parsed = parser.parse_args(arguments)

    if (
        not IS_WINDOWS
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
    ):
        emit_registration_result(
            {
                "schema": 1,
                "ok": False,
                "error": "ROOT_NOT_ALLOWED",
                "message": (
                    "регистрацию профиля нельзя выполнять от root. "
                    "Запустите команду от имени пользователя рабочего стола."
                ),
            },
            json_output=parsed.json_output,
            error=True,
        )
        return 3

    lock: Optional[object] = None
    try:
        lock = acquire_settings_lock(blocking=False)
        profile, backup = register_scanner_profile(
            device_id=parsed.device_id,
            address=parsed.address,
            name=parsed.name,
            connection=parsed.connection,
        )
    except SettingsBusyError as exc:
        emit_registration_result(
            {
                "schema": 1,
                "ok": False,
                "error": "NAPS3_RUNNING",
                "message": str(exc),
            },
            json_output=parsed.json_output,
            error=True,
        )
        return 4
    except Naps3Error as exc:
        emit_registration_result(
            {
                "schema": 1,
                "ok": False,
                "error": "SCANNER_PROBE_FAILED",
                "message": str(exc),
            },
            json_output=parsed.json_output,
            error=True,
        )
        return 3
    except OSError as exc:
        emit_registration_result(
            {
                "schema": 1,
                "ok": False,
                "error": "SETTINGS_WRITE_FAILED",
                "message": friendly_general_error(exc, "сохранение профиля"),
            },
            json_output=parsed.json_output,
            error=True,
        )
        return 5
    finally:
        release_settings_lock(lock)

    payload: dict[str, object] = {
        "schema": 1,
        "ok": True,
        "version": APP_VERSION,
        "profile": {
            "name": profile.get("name", ""),
            "device_id": profile.get("device_id", ""),
            "backend": profile.get("backend", ""),
            "connection_kind": profile.get("connection_kind", ""),
            "transport": profile.get("transport", ""),
            "url": profile.get("url", ""),
            "adf_present": profile.get("adf_present", False),
            "adf_duplex_supported": profile.get(
                "adf_duplex_supported", False
            ),
        },
        "backup": str(backup) if backup else "",
    }
    emit_registration_result(payload, json_output=parsed.json_output)
    return 0


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")

    if "--registration-api-version" in sys.argv[1:]:
        print("1")
        return 0

    if "--register-scanner" in sys.argv[1:]:
        return run_registration_cli(sys.argv[1:])

    runtime_error = check_runtime()
    if runtime_error:
        print(runtime_error, file=sys.stderr)
        return 2

    if "--self-test" in sys.argv:
        print(f"NAPS3 {APP_VERSION}: runtime OK")
        return 0

    app = Naps3Application()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
