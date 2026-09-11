"""Capture failures that happen before the GTK window can be shown."""

from __future__ import annotations

import faulthandler
import os
from pathlib import Path
import platform
import sys
import time
import traceback


def _open_startup_log():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        log_directory = Path(local_app_data) / "NAPS3"
    else:
        log_directory = Path.home() / "AppData" / "Local" / "NAPS3"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "startup.log"
    previous_path = log_directory / "startup.previous.log"
    if log_path.is_file() and log_path.stat().st_size > 1_000_000:
        try:
            log_path.replace(previous_path)
        except OSError:
            pass
    return log_path.open("a", encoding="utf-8", buffering=1)


try:
    _startup_log = _open_startup_log()
except OSError:
    _startup_log = None

if _startup_log is not None:
    if sys.stdout is None:
        sys.stdout = _startup_log
    if sys.stderr is None:
        sys.stderr = _startup_log
    try:
        print(
            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"NAPS3 start; Windows={platform.platform()}; "
            f"Python={platform.python_version()}",
            file=_startup_log,
        )
    except Exception:
        pass
    try:
        faulthandler.enable(_startup_log)
    except Exception:
        pass

    def _report_unhandled_exception(exc_type, exc_value, exc_traceback):
        try:
            print("Необработанная ошибка запуска NAPS3:", file=_startup_log)
            traceback.print_exception(
                exc_type,
                exc_value,
                exc_traceback,
                file=_startup_log,
            )
            _startup_log.flush()
        except Exception:
            pass
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "NAPS3 не удалось запустить.\n\n"
                "Подробности записаны в:\n"
                "%LOCALAPPDATA%\\NAPS3\\startup.log",
                "Ошибка запуска NAPS3",
                0x10,
            )
        except Exception:
            pass

    sys.excepthook = _report_unhandled_exception
