#!/usr/bin/env python3
"""Bundle the tested MSYS2 Cairo DLL until the newer Windows build is fixed."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request


PACKAGE_NAME = "mingw-w64-ucrt-x86_64-cairo-1.18.4-4-any.pkg.tar.zst"
PACKAGE_URL = f"https://repo.msys2.org/mingw/ucrt64/{PACKAGE_NAME}"
PACKAGE_SHA256 = "05acf11b3dcb32467ca5b79a881aae8e7d8a0fba60dd2431462c46366d667506"
DLL_SHA256 = "10cb3ddfb67ef352365d5cdf1fce0376693d2d6e90aefefee41b3c0c5ce363e4"
PACKAGE_DLL_PATH = "ucrt64/bin/libcairo-2.dll"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_package(archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=archive.parent, suffix=".download")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with urllib.request.urlopen(PACKAGE_URL, timeout=60) as response:
                while block := response.read(1024 * 1024):
                    output.write(block)
        if sha256(temporary) != PACKAGE_SHA256:
            raise RuntimeError("Cairo archive SHA-256 mismatch")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)


def pin_cairo(bundle: Path, archive: Path) -> None:
    dll = bundle / "_internal" / "libcairo-2.dll"
    if not dll.is_file():
        raise FileNotFoundError(f"Windows bundle is missing {dll}")
    if sha256(dll) == DLL_SHA256:
        print("Cairo 1.18.4-4 is already included in the Windows bundle")
        return

    if not archive.is_file() or sha256(archive) != PACKAGE_SHA256:
        download_package(archive)

    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    tar = system_root / "System32" / "tar.exe"
    if not tar.is_file():
        raise FileNotFoundError(f"System tar executable was not found: {tar}")
    descriptor, temporary_name = tempfile.mkstemp(dir=dll.parent, suffix=".dll")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            result = subprocess.run(
                [str(tar), "-xOf", str(archive), PACKAGE_DLL_PATH],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode:
            raise RuntimeError(
                f"Failed to extract Cairo: {result.stderr.decode(errors='replace')}"
            )
        if sha256(temporary) != DLL_SHA256:
            raise RuntimeError("Cairo DLL SHA-256 mismatch")
        temporary.replace(dll)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Cairo 1.18.4-4 included in Windows bundle: {dll}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    archive = args.archive or root / "build" / "windows-dependencies" / PACKAGE_NAME
    pin_cairo(args.bundle.resolve(), archive.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
