"""Windows helpers for the NAPS3 WIA backend.

The GTK application stays platform-neutral.  This module contains the small
boundary that starts the bundled native bridge, validates its JSON output,
and turns installed WIA devices into the scanner-profile format used by NAPS3.
It deliberately has no third-party imports, so its parsing and selection logic
can be tested on Linux as well as Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional


class WindowsBackendError(RuntimeError):
    """A failure reported by the local Windows scanning bridge."""


def application_root() -> Path:
    """Return the source or PyInstaller bundle root."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    return application_root().joinpath(*parts)


def wia_bridge_executable() -> Path:
    return resource_path("windows", "NAPS3.WiaBridge.exe")


def windows_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def build_wia_command(action: str, **values: object) -> list[str]:
    bridge = wia_bridge_executable()
    if not bridge.is_file():
        raise WindowsBackendError(
            f"Не найден компонент сканирования Windows: {bridge}"
        )
    command = [
        str(bridge),
        "-Action",
        action,
    ]
    for key, value in values.items():
        if value is None:
            continue
        command.extend((f"-{key}", str(value)))
    return command


def parse_bridge_json(raw: str) -> Any:
    """Decode the last JSON line, ignoring harmless helper diagnostics."""
    for line in reversed(raw.replace("\ufeff", "").splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise WindowsBackendError("Компонент WIA вернул ответ в неизвестном формате.")


def bridge_error_text(stdout: str, stderr: str) -> str:
    combined = "\n".join(part for part in (stderr, stdout) if part).strip()
    try:
        payload = parse_bridge_json(combined)
    except WindowsBackendError:
        return combined or "Windows WIA завершил операцию без описания ошибки."
    if not isinstance(payload, dict):
        return combined
    message = str(payload.get("error") or "Ошибка Windows WIA").strip()
    hresult = str(payload.get("hresult") or "").strip()
    return f"WIA {hresult}: {message}" if hresult else message


def run_wia_bridge(
    action: str,
    *,
    timeout: float = 15.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    **values: object,
) -> Any:
    command = build_wia_command(action, **values)
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=windows_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WindowsBackendError(f"Не удалось запустить Windows WIA: {exc}") from exc
    if result.returncode != 0:
        raise WindowsBackendError(bridge_error_text(result.stdout, result.stderr))
    return parse_bridge_json(result.stdout)


def wia_device_profile(item: dict[str, Any]) -> dict[str, object]:
    device_id = str(item.get("device_id") or "").strip()
    if not device_id:
        raise WindowsBackendError("Windows WIA вернул устройство без идентификатора.")
    name = str(item.get("name") or item.get("description") or "Сканер WIA").strip()
    port = str(item.get("port") or "").strip()
    capabilities_known = bool(item.get("capabilities_known", False))
    has_adf = bool(item.get("has_adf", False))
    has_duplex = bool(item.get("has_duplex", False))
    resolutions = [
        int(value)
        for value in item.get("resolutions", []) or []
        if str(value).isdigit()
    ]
    return {
        "name": name,
        "url": "",
        "device_id": device_id,
        "transport": "Windows Image Acquisition (WIA)",
        "connection_kind": "windows-wia",
        "backend": "wia",
        "ip": "",
        "port": port,
        "saved_at": int(time.time()),
        "profile_source": "windows-wia",
        "adf_capabilities_known": capabilities_known,
        "adf_present": has_adf,
        "adf_duplex_supported": has_duplex,
        "adf_simplex_resolutions": resolutions if has_adf else [],
        "adf_duplex_resolutions": resolutions if has_duplex else [],
    }


def discover_wia_scanners(match_filter: str = "") -> list[dict[str, object]]:
    payload = run_wia_bridge("list", timeout=20.0)
    if not isinstance(payload, list):
        raise WindowsBackendError("Windows WIA вернул некорректный список устройств.")
    profiles: list[dict[str, object]] = []
    filter_text = match_filter.casefold().strip()
    for item in payload:
        if not isinstance(item, dict):
            continue
        profile = wia_device_profile(item)
        haystack = " ".join(
            str(profile.get(key) or "")
            for key in ("name", "device_id", "port", "transport")
        ).casefold()
        if filter_text and filter_text not in haystack:
            continue
        profiles.append(profile)
    return profiles


def probe_wia_device(device_id: str) -> bool:
    payload = run_wia_bridge("probe", timeout=10.0, DeviceId=device_id)
    return bool(isinstance(payload, dict) and payload.get("present"))


def build_wia_scan_command(
    device_id: str,
    output_dir: Path,
    source: str,
    mode: str,
    dpi: int,
    paper: str,
) -> list[str]:
    return build_wia_command(
        "scan",
        DeviceId=device_id,
        OutputDirectory=output_dir,
        Source=source,
        Mode=mode,
        Dpi=dpi,
        Paper=paper,
    )


def find_runtime_executable(name: str) -> Optional[str]:
    """Find a bundled helper first and then fall back to PATH."""
    suffix = ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else ""
    bundled = resource_path(f"{name}{suffix}")
    if bundled.is_file():
        return str(bundled)
    return shutil.which(f"{name}{suffix}") or shutil.which(name)
