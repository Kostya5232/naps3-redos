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
    subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/optimize+",
            "/platform:anycpu",
            f"/out:{output}",
            "/reference:Microsoft.CSharp.dll",
            str(source),
        ],
        check=True,
    )
    if not output.is_file():
        raise SystemExit("Компилятор не создал WIA-мост.")
    print(f"Готово: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
