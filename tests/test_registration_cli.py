from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import naps3


class RegistrationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.patches = [
            mock.patch.object(naps3, "IS_WINDOWS", False),
            mock.patch.object(naps3, "CONFIG_DIR", self.root),
            mock.patch.object(naps3, "CONFIG_FILE", self.root / "settings.json"),
            mock.patch.object(
                naps3, "CONFIG_LOCK_FILE", self.root / "settings.lock"
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_local_registration_probes_exact_id_and_preserves_settings(self):
        naps3.CONFIG_FILE.write_text(
            json.dumps({"export_format": "PDF", "theme": "system"}),
            encoding="utf-8",
        )
        probe = subprocess.CompletedProcess(
            [],
            0,
            stdout=b"  --source Flatbed|ADF|ADF Duplex\n",
        )
        with (
            mock.patch.object(naps3.subprocess, "run", return_value=probe) as run,
            mock.patch.object(
                naps3,
                "local_sane_backend_environment",
                return_value={"LC_ALL": "C"},
            ),
        ):
            profile, backup = naps3.register_scanner_profile(
                device_id="pixma:04A92737_1M66352840",
                name="Canon MF4410",
            )
            network_profile = naps3.build_registered_sane_profile(
                "airscan:e0:Office Scanner",
                "Office Scanner",
                "network",
            )

        self.assertEqual(
            run.call_args_list[0].args[0],
            ["scanimage", "-d", "pixma:04A92737_1M66352840", "--help"],
        )
        stored = json.loads(naps3.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored["export_format"], "PDF")
        self.assertEqual(stored["theme"], "system")
        self.assertEqual(stored[naps3.PROFILE_KEY], profile)
        self.assertEqual(profile["profile_source"], "printer-doctor-cli")
        self.assertEqual(network_profile["connection_kind"], "network")
        self.assertIn("сетевой сканер", network_profile["transport"])
        self.assertIsNotNone(backup)
        assert backup is not None
        self.assertEqual(
            json.loads(backup.read_text(encoding="utf-8"))["theme"],
            "system",
        )

    def test_network_registration_uses_only_requested_address(self):
        expected = {
            "name": "Kyocera",
            "url": "http://192.0.2.15/eSCL",
            "device_id": "",
            "connection_kind": "network",
        }
        with mock.patch.object(
            naps3, "build_network_profile", return_value=expected.copy()
        ) as build:
            profile, _backup = naps3.register_scanner_profile(
                address="192.0.2.15",
                name="Kyocera",
            )

        build.assert_called_once_with(
            "Kyocera",
            "192.0.2.15",
            discover_device_id=False,
        )
        self.assertEqual(profile["profile_source"], "printer-doctor-cli")

    def test_atomic_settings_write_keeps_previous_file_on_replace_failure(self):
        naps3.CONFIG_FILE.write_text("old settings", encoding="utf-8")
        with mock.patch.object(
            naps3.os, "replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(OSError):
                naps3.save_settings({"new": True})

        self.assertEqual(
            naps3.CONFIG_FILE.read_text(encoding="utf-8"),
            "old settings",
        )
        self.assertEqual(list(self.root.glob(".settings-*.tmp")), [])

    def test_registration_cli_refuses_root_and_returns_json_error(self):
        output = io.StringIO()
        with (
            mock.patch.object(naps3.os, "geteuid", return_value=0, create=True),
            mock.patch.object(naps3, "register_scanner_profile") as register,
            contextlib.redirect_stdout(output),
        ):
            result = naps3.run_registration_cli(
                [
                    "--register-scanner",
                    "--device-id",
                    "pixma:test",
                    "--json",
                ]
            )

        self.assertEqual(result, 3)
        self.assertEqual(json.loads(output.getvalue())["error"], "ROOT_NOT_ALLOWED")
        register.assert_not_called()

    def test_registration_cli_reports_open_gui_without_writing(self):
        output = io.StringIO()
        with (
            mock.patch.object(naps3.os, "geteuid", return_value=1000, create=True),
            mock.patch.object(
                naps3,
                "acquire_settings_lock",
                side_effect=naps3.SettingsBusyError("NAPS3 открыт"),
            ),
            mock.patch.object(naps3, "register_scanner_profile") as register,
            contextlib.redirect_stdout(output),
        ):
            result = naps3.run_registration_cli(
                [
                    "--register-scanner",
                    "--address",
                    "192.0.2.20",
                    "--json",
                ]
            )

        self.assertEqual(result, 4)
        self.assertEqual(json.loads(output.getvalue())["error"], "NAPS3_RUNNING")
        register.assert_not_called()


if __name__ == "__main__":
    unittest.main()
