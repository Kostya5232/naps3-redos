from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import naps3


class PatchedTestCase(unittest.TestCase):
    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value


class ScannerRecoveryTests(PatchedTestCase):
    def setUp(self) -> None:
        # Existing recovery cases exercise the Linux SANE path even when the
        # suite itself runs on a Windows build runner.
        self.patch(naps3, "IS_WINDOWS", new=False)
        self.selected = {
            "name": "Kyocera ECOSYS MA4000x",
            "device_id": "airscan:e0:Kyocera ECOSYS MA4000x",
            "url": "http://127.0.0.1:60000/eSCL",
            "connection_kind": "usb",
        }
        self.other = {
            "name": "HP LaserJet Pro M428f-M429f",
            "device_id": "airscan:w5:HP LaserJet Pro M428f-M429f",
            "url": "http://192.0.2.10/eSCL",
            "connection_kind": "network",
        }
        self.discovery = self.patch(
            naps3, "discover_scanner_profile", return_value=self.other
        )
        self.patch(naps3, "append_scan_log")
        self.patch(naps3.GLib, "idle_add")
        self.owner = SimpleNamespace(
            cancel_requested=False,
            set_status=mock.Mock(),
            _scan_once=mock.Mock(return_value=([Path("other.png")], "", self.other)),
            _scan_windows_wia=mock.Mock(),
            _scan_direct_escl=mock.Mock(),
            _retryable_device_error=naps3.MainWindow._retryable_device_error,
        )
        self.open_error = (
            "scanimage: open of device airscan:e0:Kyocera ECOSYS MA4000x "
            "failed: Error during device I/O"
        )

    def scan(self, profile, *, stream_adf=False):
        return naps3.MainWindow._scan_worker(
            self.owner, Path("unused-scan-directory"), "MFP-YUR",
            "ADF Duplex", "Color", 300, "A4", profile, stream_adf,
        )

    def test_recovery_never_scans_available_hp_instead_of_selected_kyocera(self):
        attempted_devices = []

        def scan_once(_directory, profile, *_args, **_kwargs):
            attempted_devices.append(profile["device_id"])
            if profile["device_id"] == self.other["device_id"]:
                return [Path("wrong-scanner.png")], "", profile
            return [], self.open_error, profile

        self.owner._scan_once.side_effect = scan_once
        with self.assertRaises(naps3.Naps3Error) as raised:
            self.scan(self.selected)
        self.assertIn(self.selected["name"], str(raised.exception))
        self.assertEqual(attempted_devices, [self.selected["device_id"]] * 2)
        self.discovery.assert_not_called()

    def test_saved_address_still_recovers_changed_sane_identifier(self):
        recovered = {**self.selected, "device_id": "airscan:e1:Kyocera ECOSYS MA4000x"}
        pages = [Path("front.png"), Path("back.png")]
        self.owner._scan_once.side_effect = [
            ([], self.open_error, self.selected),
            (pages, "", recovered),
        ]
        self.assertEqual(self.scan(self.selected), (pages, recovered))
        attempts = self.owner._scan_once.call_args_list
        self.assertFalse(attempts[0].kwargs["force_resolve_device"])
        self.assertTrue(attempts[1].kwargs["force_resolve_device"])
        self.assertEqual(attempts[1].args[1]["url"], self.selected["url"])
        self.discovery.assert_not_called()

    def test_failed_address_refresh_does_not_trigger_other_device_search(self):
        self.owner._scan_once.side_effect = [
            ([], self.open_error, self.selected),
            naps3.Naps3Error("Сохранённый локальный адрес больше не доступен."),
            ([Path("other.png")], "", self.other),
        ]
        with self.assertRaises(naps3.Naps3Error) as raised:
            self.scan(self.selected)
        self.assertIn(self.selected["name"], str(raised.exception))
        self.discovery.assert_not_called()
        self.assertEqual(self.owner._scan_once.call_count, 2)

    def test_profile_without_url_does_not_fall_back_to_another_scanner(self):
        selected = {**self.selected, "url": ""}
        self.owner._scan_once.return_value = ([], self.open_error, selected)
        with self.assertRaises(naps3.Naps3Error):
            self.scan(selected)
        self.discovery.assert_not_called()
        self.owner._scan_once.assert_called_once()

    def test_invalid_saved_profile_is_not_replaced_by_default_device(self):
        for selected in ({"name": self.selected["name"]}, {}):
            with self.subTest(profile=selected):
                with self.assertRaises(naps3.Naps3Error):
                    self.scan(selected)
        self.discovery.assert_not_called()
        self.owner._scan_once.assert_not_called()

    def test_initial_discovery_remains_available_without_selected_profile(self):
        pages = [Path("page.png")]
        self.owner._scan_once.return_value = (pages, "", self.other)
        self.assertEqual(self.scan(None), (pages, self.other))
        self.discovery.assert_called_once_with("MFP-YUR")
        self.owner._scan_once.assert_called_once()

    def test_successful_usb_adf_keeps_existing_sane_path(self):
        pages = [Path("front.png"), Path("back.png")]
        self.owner._scan_once.return_value = (pages, "", self.selected)
        self.assertEqual(self.scan(self.selected, stream_adf=True), (pages, self.selected))
        self.owner._scan_once.assert_called_once()
        self.owner._scan_direct_escl.assert_not_called()
        self.discovery.assert_not_called()

    def test_successful_network_adf_keeps_direct_escl_path(self):
        pages = [Path("page.png")]
        self.owner._scan_direct_escl.return_value = (pages, self.other)
        self.assertEqual(self.scan(self.other, stream_adf=True), (pages, self.other))
        self.owner._scan_once.assert_not_called()
        self.owner._scan_direct_escl.assert_called_once()
        self.discovery.assert_not_called()

    def test_network_busy_does_not_start_sane_or_discover_other_device(self):
        self.owner._scan_direct_escl.side_effect = naps3.Naps3Error("HTTP 503")
        with self.assertRaises(naps3.Naps3Error):
            self.scan(self.other, stream_adf=True)
        self.owner._scan_once.assert_not_called()
        self.discovery.assert_not_called()

    def test_no_documents_does_not_retry_or_change_device(self):
        self.owner._scan_once.return_value = (
            [], "scanimage: sane_start: Document feeder out of documents", self.selected
        )
        with self.assertRaisesRegex(naps3.Naps3Error, "автоподатчике нет документов"):
            self.scan(self.selected)
        self.owner._scan_once.assert_called_once()
        self.discovery.assert_not_called()

    def test_cancel_before_scan_does_not_start_discovery(self):
        self.owner.cancel_requested = True
        with self.assertRaisesRegex(naps3.Naps3Error, "отменено"):
            self.scan(None)
        self.discovery.assert_not_called()
        self.owner._scan_once.assert_not_called()

    def test_cancel_during_failed_scan_prevents_recovery(self):
        def scan_once(*_args, **_kwargs):
            self.owner.cancel_requested = True
            return [], self.open_error, self.selected

        self.owner._scan_once.side_effect = scan_once
        with self.assertRaisesRegex(naps3.Naps3Error, "отменено"):
            self.scan(self.selected)
        self.owner._scan_once.assert_called_once()
        self.discovery.assert_not_called()

    def test_wia_scan_uses_exact_selected_device_without_discovery(self):
        selected = {
            "name": "Kyocera ECOSYS MA4000x",
            "device_id": r"WIA\\selected-kyocera",
            "backend": "wia",
            "connection_kind": "windows-wia",
        }
        pages = [Path("front.png"), Path("back.png")]
        self.owner._scan_windows_wia.return_value = (pages, "", selected)

        self.assertEqual(self.scan(selected), (pages, selected))
        self.owner._scan_windows_wia.assert_called_once()
        self.assertEqual(
            self.owner._scan_windows_wia.call_args.args[1]["device_id"],
            selected["device_id"],
        )
        self.owner._scan_once.assert_not_called()
        self.discovery.assert_not_called()

    def test_sleeping_wia_device_retries_same_device_once(self):
        selected = {
            "name": "Kyocera ECOSYS MA4000x",
            "device_id": r"WIA\\selected-kyocera",
            "backend": "wia",
            "connection_kind": "windows-wia",
        }
        pages = [Path("page.png")]
        self.owner._scan_windows_wia.side_effect = [
            ([], "WIA 0x80210005: Offline", selected),
            (pages, "", selected),
        ]
        sleep = self.patch(naps3.time, "sleep")

        self.assertEqual(self.scan(selected), (pages, selected))
        self.assertEqual(self.owner._scan_windows_wia.call_count, 2)
        self.assertEqual(
            [call.args[1]["device_id"] for call in self.owner._scan_windows_wia.call_args_list],
            [selected["device_id"], selected["device_id"]],
        )
        sleep.assert_called_once_with(2.0)
        self.discovery.assert_not_called()


class StartupProfileTests(PatchedTestCase):
    def setUp(self):
        self.selected = {
            "name": "Kyocera ECOSYS MA4000x",
            "device_id": "airscan:e0:Kyocera ECOSYS MA4000x",
            "url": "http://127.0.0.1:60000/eSCL",
            "connection_kind": "usb",
        }
        self.other = {
            "name": "HP LaserJet Pro M428f-M429f",
            "device_id": "airscan:e2:HP LaserJet Pro M428f-M429f",
            "url": "http://127.0.0.1:60001/eSCL",
            "connection_kind": "usb",
        }
        self.discovery = self.patch(
            naps3, "discover_scanner_profile", return_value=self.other
        )
        self.usb_discovery = self.patch(
            naps3, "discover_usb_scanners", return_value=[self.other]
        )
        self.probe = self.patch(naps3, "probe_escl_url", return_value=True)
        self.wia_probe = self.patch(naps3, "probe_wia_device", return_value=True)
        self.process = self.patch(
            naps3.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout=b""),
        )
        self.sane_config = self.patch(
            naps3, "prepare_limited_sane_config", return_value={"LC_ALL": "C"}
        )
        self.idle = self.patch(naps3.GLib, "idle_add")
        self.patch(
            naps3.threading, "Thread",
            side_effect=lambda *, target, daemon: SimpleNamespace(start=target),
        )
        self.owner = SimpleNamespace(
            scanner_profile=dict(self.selected),
            is_busy=False,
            _set_device_profile=mock.Mock(),
            _save_ui_settings=mock.Mock(),
            set_status=mock.Mock(),
        )
        self.owner._saved_profile_probe_ready = (
            lambda *args: naps3.MainWindow._saved_profile_probe_ready(self.owner, *args)
        )

    def finish_probe(self):
        self.idle.assert_called_once()
        callback, *arguments = self.idle.call_args.args
        callback(*arguments)

    def test_sleeping_saved_usb_scanner_is_not_replaced_by_available_hp(self):
        self.probe.return_value = False
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.finish_probe()
        self.assertEqual(self.owner.scanner_profile, self.selected)
        self.probe.assert_called_once_with(self.selected["url"])
        self.discovery.assert_not_called()
        self.usb_discovery.assert_not_called()
        self.owner._save_ui_settings.assert_not_called()
        self.assertEqual(self.owner._set_device_profile.call_args.args[2], "error")

    def test_saved_network_scanner_keeps_priority_over_unrelated_usb_device(self):
        selected = {
            **self.selected, "url": "http://192.0.2.20:80/eSCL",
            "connection_kind": "network",
        }
        self.owner.scanner_profile = selected
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.finish_probe()
        self.assertEqual(self.owner.scanner_profile, selected)
        self.probe.assert_called_once_with(selected["url"])
        self.discovery.assert_not_called()
        self.usb_discovery.assert_not_called()
        self.owner._save_ui_settings.assert_called_once()

    def test_probe_exception_keeps_selected_device(self):
        self.probe.side_effect = TimeoutError("scanner still waking")
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.finish_probe()
        self.assertEqual(self.owner.scanner_profile, self.selected)
        self.discovery.assert_not_called()
        self.usb_discovery.assert_not_called()
        self.assertEqual(self.owner._set_device_profile.call_args.args[2], "error")

    def test_hpaio_probe_opens_only_saved_device_with_its_backend(self):
        selected = {
            "name": "HP LaserJet M227",
            "device_id": "hpaio:/usb/HP_LaserJet_M227?serial=selected-device",
            "connection_kind": "usb-hpaio", "url": "",
        }
        self.owner.scanner_profile = selected
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.finish_probe()
        self.assertEqual(self.owner.scanner_profile, selected)
        self.process.assert_called_once()
        self.assertEqual(self.process.call_args.args[0], [
            "scanimage", "-d", selected["device_id"], "--help"
        ])
        self.sane_config.assert_called_once_with(naps3.USB_SANE_DIR, ["hpaio"])
        self.assertEqual(self.process.call_args.kwargs["env"], {"LC_ALL": "C"})
        self.discovery.assert_not_called()
        self.usb_discovery.assert_not_called()
        self.probe.assert_not_called()

    def test_sane_probe_failure_keeps_profile_without_address(self):
        selected = {**self.selected, "url": ""}
        self.owner.scanner_profile = selected
        self.process.return_value = SimpleNamespace(returncode=1, stdout=b"I/O error")
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.finish_probe()
        self.assertEqual(self.owner.scanner_profile, selected)
        self.discovery.assert_not_called()
        self.usb_discovery.assert_not_called()
        self.assertEqual(self.owner._set_device_profile.call_args.args[2], "error")

    def test_invalid_profile_does_not_trigger_initial_discovery(self):
        selected = {"name": self.selected["name"]}
        self.owner.scanner_profile = selected
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.finish_probe()
        self.assertEqual(self.owner.scanner_profile, selected)
        self.discovery.assert_not_called()
        self.usb_discovery.assert_not_called()
        self.probe.assert_not_called()
        self.process.assert_not_called()

    def test_saved_wia_profile_probes_only_exact_device(self):
        selected = {
            "name": "Kyocera ECOSYS MA4000x",
            "device_id": r"WIA\\selected-kyocera",
            "backend": "wia",
            "connection_kind": "windows-wia",
        }
        self.owner.scanner_profile = selected
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.finish_probe()

        self.wia_probe.assert_called_once_with(selected["device_id"])
        self.probe.assert_not_called()
        self.process.assert_not_called()
        self.discovery.assert_not_called()
        self.usb_discovery.assert_not_called()

    def test_late_probe_does_not_overwrite_manual_selection(self):
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.owner.scanner_profile = dict(self.other)
        self.finish_probe()
        self.assertEqual(self.owner.scanner_profile, self.other)
        self.owner._set_device_profile.assert_not_called()
        self.owner._save_ui_settings.assert_not_called()
        self.owner.set_status.assert_not_called()

    def test_late_probe_does_not_update_ui_during_scan(self):
        self.probe.return_value = False
        naps3.MainWindow._probe_saved_profile_async(self.owner)
        self.owner.is_busy = True
        self.finish_probe()
        self.assertEqual(self.owner.scanner_profile, self.selected)
        self.owner._set_device_profile.assert_not_called()
        self.owner.set_status.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
