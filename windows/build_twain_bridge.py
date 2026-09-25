#!/usr/bin/env python3
"""Compile the isolated 32-bit TWAIN bridge against the pinned NTwain DLL."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess


NTWAIN_SHA256 = "42249a43fcc5d0943904b800528957c09b6b00ac0bd26ec78dc19e0523bb538d"


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    compiler = windows_dir / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe"
    if not compiler.is_file():
        raise SystemExit("32-bit .NET Framework compiler was not found.")

    library = root / "windows" / "vendor" / "NTwain.dll"
    if hashlib.sha256(library.read_bytes()).hexdigest() != NTWAIN_SHA256:
        raise SystemExit("Pinned NTwain.dll hash mismatch.")
    output_dir = root / "build" / "windows-twain"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "NAPS3.TwainBridge.exe"

    def windows_path(path: Path) -> str:
        return str(path).replace("/", "\\")

    subprocess.run(
        [
            windows_path(compiler),
            "/nologo",
            "/target:exe",
            "/optimize+",
            "/platform:x86",
            f"/out:{windows_path(output)}",
            f"/reference:{windows_path(library)}",
            windows_path(root / "windows" / "twain_bridge.cs"),
        ],
        check=True,
    )
    shutil.copy2(library, output_dir / "NTwain.dll")
    shutil.copy2(root / "windows" / "vendor" / "NTwain.LICENSE.txt", output_dir)
    print(f"Built: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
