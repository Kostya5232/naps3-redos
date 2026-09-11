#!/usr/bin/env python3
"""Compile the native WIA helper with the .NET Framework compiler."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    windows_directory = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows_directory
        / "Microsoft.NET"
        / "Framework64"
        / "v4.0.30319"
        / "csc.exe",
        windows_directory
        / "Microsoft.NET"
        / "Framework"
        / "v4.0.30319"
        / "csc.exe",
    )
    compiler = next((path for path in candidates if path.is_file()), None)
    if compiler is None:
        raise SystemExit(
            "Не найден компилятор .NET Framework csc.exe для сборки WIA-моста."
        )

    output_directory = root / "build" / "windows-wia"
    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / "NAPS3.WiaBridge.exe"
    source = root / "windows" / "wia_bridge.cs"

    def windows_path(path: Path) -> str:
        # MSYS2 Python exposes drive paths with forward slashes. The .NET
        # Framework compiler treats a segment such as /windows as an option,
        # so every filesystem argument must use native Windows separators.
        return str(path).replace("/", "\\")

    subprocess.run(
        [
            windows_path(compiler),
            "/nologo",
            "/target:exe",
            "/optimize+",
            "/platform:anycpu",
            f"/out:{windows_path(output)}",
            "/reference:Microsoft.CSharp.dll",
            windows_path(source),
        ],
        check=True,
    )
    if not output.is_file():
        raise SystemExit("Компилятор не создал WIA-мост.")
    print(f"Готово: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
