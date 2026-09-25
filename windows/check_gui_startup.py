#!/usr/bin/env python3
"""Check that the packaged Windows application opens its real GTK window."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def has_main_window(process_id: int) -> bool:
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    found = False

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def inspect(hwnd: int, _data: int) -> bool:
        nonlocal found
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == process_id and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if title.value == "NAPS3":
                found = True
        return True

    user32.EnumWindows(inspect, 0)
    return found


def main() -> int:
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    with tempfile.TemporaryDirectory(prefix="naps3-gui-check-") as name:
        root = Path(name)
        roaming = root / "Roaming"
        local = root / "Local"
        roaming.mkdir()
        local.mkdir()
        env = dict(os.environ, APPDATA=str(roaming), LOCALAPPDATA=str(local))
        process = subprocess.Popen(
            [str(executable)],
            cwd=executable.parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        visible = False
        alive = False
        try:
            started = time.monotonic()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and process.poll() is None:
                visible = has_main_window(process.pid) or visible
                if visible and time.monotonic() - started >= 4:
                    break
                time.sleep(0.2)
            alive = process.poll() is None
        finally:
            if process.poll() is None:
                process.kill()
            _stdout, stderr = process.communicate(timeout=5)

        startup_log = local / "NAPS3" / "startup.log"
        log_text = (
            startup_log.read_text(encoding="utf-8", errors="replace")
            if startup_log.is_file()
            else ""
        )
        if not visible or not alive or "Fatal Python error" in log_text:
            raise RuntimeError(
                f"NAPS3 window did not open (exit code {process.returncode}).\n"
                f"stderr: {stderr.decode(errors='replace')}\n"
                f"startup.log: {log_text[-4000:]}"
            )
        print("NAPS3 GUI opened: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
