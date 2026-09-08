from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import windows_backend


class WindowsBackendTests(unittest.TestCase):
    def test_bridge_json_uses_last_valid_line(self) -> None:
        payload = windows_backend.parse_bridge_json(
            "PowerShell host message\n{\"present\":true}\n"
        )
        self.assertEqual(payload, {"present": True})

    def test_bridge_error_keeps_hresult(self) -> None:
        error = windows_backend.bridge_error_text(
            "", '{"error":"No paper","hresult":"0x80210003"}'
        )
        self.assertEqual(error, "WIA 0x80210003: No paper")

    def test_wia_profile_maps_capabilities(self) -> None:
        profile = windows_backend.wia_device_profile({
            "device_id": "wia:kyocera",
            "name": "Kyocera ECOSYS MA4000x",
            "port": "USB001",
            "capabilities_known": True,
            "has_adf": True,
            "has_duplex": True,
            "resolutions": [200, "300"],
        })
        self.assertEqual(profile["backend"], "wia")
        self.assertEqual(profile["connection_kind"], "windows-wia")
        self.assertTrue(profile["adf_duplex_supported"])
        self.assertEqual(profile["adf_duplex_resolutions"], [200, 300])

    def test_discovery_filters_without_changing_selected_device_ids(self) -> None:
        payload = [
            {"device_id": "wia:hp", "name": "HP M428", "port": "USB001"},
            {"device_id": "wia:kyocera", "name": "Kyocera MA4000x", "port": "USB002"},
        ]
        with mock.patch.object(
            windows_backend, "run_wia_bridge", return_value=payload
        ):
            profiles = windows_backend.discover_wia_scanners("kyocera")
        self.assertEqual([item["device_id"] for item in profiles], ["wia:kyocera"])

    def test_scan_command_passes_exact_device_and_options(self) -> None:
        with mock.patch.object(
            windows_backend, "powershell_executable", return_value="powershell.exe"
        ), mock.patch.object(
            windows_backend, "wia_bridge_path", return_value=Path("bridge.ps1")
        ), mock.patch.object(Path, "is_file", return_value=True):
            command = windows_backend.build_wia_scan_command(
                "wia:device", Path("pages"), "ADF Duplex", "Gray", 300, "A4"
            )
        joined = "\n".join(command)
        self.assertIn("wia:device", joined)
        self.assertIn("ADF Duplex", joined)
        self.assertIn("Gray", joined)
        self.assertIn("300", joined)
        self.assertIn("A4", joined)

    def test_nonzero_bridge_result_is_translated(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=json.dumps({"error": "Offline", "hresult": "0x80210005"}),
        )
        with mock.patch.object(
            windows_backend, "build_wia_command", return_value=["powershell"]
        ):
            with self.assertRaisesRegex(
                windows_backend.WindowsBackendError, "0x80210005"
            ):
                windows_backend.run_wia_bridge(
                    "probe", runner=lambda *_args, **_kwargs: completed
                )


if __name__ == "__main__":
    unittest.main()
