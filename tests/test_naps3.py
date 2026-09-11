from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import naps3


class RuntimeCheckTests(unittest.TestCase):
    def test_windows_gui_start_does_not_require_wia_helper(self) -> None:
        with mock.patch.object(naps3, "IS_WINDOWS", True), mock.patch.object(
            naps3,
            "run_wia_bridge",
            side_effect=AssertionError("GUI startup must not start the WIA helper"),
        ):
            self.assertIsNone(naps3.check_runtime())

    def test_windows_self_test_still_checks_wia_and_pdf_runtime(self) -> None:
        with mock.patch.object(naps3, "IS_WINDOWS", True), mock.patch.object(
            naps3,
            "run_wia_bridge",
            side_effect=naps3.WindowsBackendError("WIA-мост недоступен"),
        ), mock.patch.object(naps3, "find_runtime_executable", return_value=None):
            error = naps3.check_runtime(strict_windows=True)

        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("WIA-мост недоступен", error)
        self.assertIn("pdftoppm", error)


class EsclParsingTests(unittest.TestCase):
    def test_manual_duplex_reverses_backs_and_interleaves(self) -> None:
        fronts = [Path("f1"), Path("f2"), Path("f3")]
        backs = [Path("b3"), Path("b2"), Path("b1")]
        self.assertEqual(
            naps3.interleave_manual_duplex_pages(
                fronts, backs, reverse_backs=True
            ),
            [Path("f1"), Path("b1"), Path("f2"), Path("b2"), Path("f3"), Path("b3")],
        )

    def test_manual_duplex_preserves_unpaired_pages(self) -> None:
        result = naps3.interleave_manual_duplex_pages(
            [Path("f1"), Path("f2"), Path("f3")],
            [Path("b2"), Path("b1")],
            reverse_backs=True,
        )
        self.assertEqual(
            result,
            [Path("f1"), Path("b1"), Path("f2"), Path("b2"), Path("f3")],
        )

    def test_scanner_status_is_bound_to_exact_job(self) -> None:
        xml = """<?xml version='1.0'?>
        <ScannerStatus xmlns='http://schemas.hp.com/imaging/escl/2011/05/03'
          xmlns:pwg='http://www.pwg.org/schemas/2010/12/sm'>
          <Jobs>
            <JobInfo><pwg:JobUri>/eSCL/ScanJobs/old</pwg:JobUri>
              <pwg:JobState>Completed</pwg:JobState>
              <ImagesCompleted>2</ImagesCompleted><ImagesToTransfer>0</ImagesToTransfer>
            </JobInfo>
            <JobInfo><pwg:JobUri>/eSCL/ScanJobs/current</pwg:JobUri>
              <pwg:JobState>Processing</pwg:JobState>
              <ImagesCompleted>1</ImagesCompleted><ImagesToTransfer>1</ImagesToTransfer>
            </JobInfo>
          </Jobs>
        </ScannerStatus>"""
        current = naps3.find_escl_job(
            xml, "http://scanner/eSCL/ScanJobs/current"
        )
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.state, "Processing")
        self.assertFalse(current.is_terminal)
        self.assertFalse(naps3.escl_job_is_drained(current, 1))

    def test_terminal_job_is_drained_only_after_all_images_received(self) -> None:
        snapshot = naps3.EsclJobSnapshot(
            uri="job", state="Completed", images_completed=3,
            images_to_transfer=0, age=0,
        )
        self.assertFalse(naps3.escl_job_is_drained(snapshot, 2))
        self.assertTrue(naps3.escl_job_is_drained(snapshot, 3))

    def test_scan_settings_select_duplex_and_lineart_fallback(self) -> None:
        xml = naps3.build_escl_scan_settings(
            "ADF Duplex", "Lineart", 300, "A4"
        ).decode()
        self.assertIn("<scan:Duplex>true</scan:Duplex>", xml)
        self.assertIn("<scan:ColorMode>Grayscale8</scan:ColorMode>", xml)
        self.assertIn("<pwg:Width>2480</pwg:Width>", xml)

    def test_error_messages_cover_common_scanner_failures(self) -> None:
        self.assertIn("автоподатчике нет", naps3.friendly_scan_error("No documents"))
        self.assertIn("занят", naps3.friendly_scan_error("Device busy"))
        self.assertIn("отменено", naps3.friendly_scan_error("", True))


class LocalScannerDiscoveryTests(unittest.TestCase):
    def test_local_sane_busy_retries_same_selected_device_once(self) -> None:
        profile = {
            "name": "Canon MF4410",
            "device_id": "pixma:04A92737_000000000000",
            "connection_kind": "usb-sane",
        }
        page = Path("page-0001.png")
        scan_once = mock.Mock(
            side_effect=[
                ([], "scanimage: open of device failed: Device busy", profile),
                ([page], "", profile),
            ]
        )
        owner = SimpleNamespace(
            cancel_requested=False,
            _scan_once=scan_once,
            _device_busy_error=naps3.MainWindow._device_busy_error,
            set_status=lambda *_args: None,
        )

        with mock.patch.object(naps3.time, "sleep") as sleep, mock.patch.object(
            naps3, "append_scan_log"
        ), mock.patch.object(
            naps3.GLib, "idle_add",
            side_effect=lambda function, *args: function(*args),
        ):
            files, returned_profile = naps3.MainWindow._scan_worker(
                owner,
                Path("scan"),
                "",
                "Flatbed",
                "Color",
                150,
                "A4",
                profile,
                False,
            )

        self.assertEqual(files, [page])
        self.assertEqual(returned_profile, profile)
        self.assertEqual(scan_once.call_count, 2)
        self.assertFalse(scan_once.call_args_list[1].kwargs["force_resolve_device"])
        sleep.assert_called_once_with(1.0)

    def test_legacy_hp_filter_is_removed(self) -> None:
        self.assertEqual(naps3.normalized_match_filter("MFP-YUR"), "")
        self.assertEqual(
            naps3.normalized_match_filter("Canon MF4410"),
            "Canon MF4410",
        )

    def test_pixma_source_names_are_preserved(self) -> None:
        capabilities = naps3.parse_sane_source_capabilities(
            "  --source Flatbed|Automatic Document Feeder [Flatbed]\n"
        )
        self.assertTrue(capabilities["has_adf"])
        self.assertFalse(capabilities["supports_duplex"])
        self.assertEqual(
            capabilities["source_map"],
            {
                "Flatbed": "Flatbed",
                "ADF": "Automatic Document Feeder",
            },
        )

    def test_canon_pixma_profile_uses_exact_sane_source(self) -> None:
        profile = naps3.build_sane_profile(
            {
                "id": "pixma:04A92737_000000000000",
                "name": "CANON Canon imageCLASS MF4410 multi-function peripheral",
            },
            "  --source Flatbed|Automatic Document Feeder [Flatbed]\n",
        )
        self.assertEqual(profile["backend"], "pixma")
        self.assertEqual(profile["connection_kind"], "usb-sane")
        self.assertEqual(
            naps3.sane_source_for_profile(profile, "ADF"),
            "Automatic Document Feeder",
        )

    def test_linux_discovery_includes_accessible_pixma_device(self) -> None:
        listing = (
            "device `pixma:04A92737_000000000000' is a CANON Canon "
            "imageCLASS MF4410 multi-function peripheral\n"
        )
        help_output = (
            b"  --source Flatbed|Automatic Document Feeder [Flatbed]\n"
        )
        result = SimpleNamespace(returncode=0, stdout=help_output)
        with mock.patch.object(naps3, "IS_WINDOWS", False), mock.patch.object(
            naps3, "discover_loopback_escl_url", return_value=""
        ), mock.patch.object(
            naps3, "list_system_sane_devices", return_value=listing
        ), mock.patch.object(
            naps3, "list_backend_devices", return_value=""
        ), mock.patch.object(naps3.subprocess, "run", return_value=result):
            profiles = naps3.discover_usb_scanners()

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["backend"], "pixma")
        self.assertEqual(
            profiles[0]["device_id"],
            "pixma:04A92737_000000000000",
        )

    def test_multiple_devices_do_not_prefer_hp(self) -> None:
        profiles = [
            {"name": "Canon MF4410"},
            {"name": "HP LaserJet Pro M428f"},
        ]
        self.assertIsNone(naps3.choose_usb_profile(profiles, ""))
        self.assertIs(
            naps3.choose_usb_profile(profiles, "Canon"),
            profiles[0],
        )


class NetworkScannerDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _dns_name(name: str) -> bytes:
        return b"".join(
            bytes([len(label.encode())]) + label.encode()
            for label in name.split(".")
        ) + b"\0"

    @classmethod
    def _mdns_response(cls) -> bytes:
        service = "_uscan._tcp.local"
        instance = "Office MFP._uscan._tcp.local"
        target = "office-printer.local"

        def record(name: str, record_type: int, data: bytes) -> bytes:
            return (
                cls._dns_name(name)
                + struct.pack("!HHIH", record_type, 1, 120, len(data))
                + data
            )

        text_values = (b"ty=Office MFP", b"rs=eSCL")
        text = b"".join(bytes([len(value)]) + value for value in text_values)
        records = [
            record(service, 12, cls._dns_name(instance)),
            record(
                instance,
                33,
                struct.pack("!HHH", 0, 0, 8080) + cls._dns_name(target),
            ),
            record(instance, 16, text),
            record(target, 1, socket.inet_aton("192.168.10.45")),
        ]
        return struct.pack("!6H", 0, 0x8400, 0, len(records), 0, 0) + b"".join(records)

    def test_mdns_escl_records_create_selectable_network_device(self) -> None:
        candidates = naps3.mdns_escl_candidates(
            [(self._mdns_response(), "192.168.10.45")]
        )
        self.assertEqual(
            candidates,
            [{
                "name": "Office MFP",
                "ip": "192.168.10.45",
                "id": "",
                "url": "http://192.168.10.45:8080/eSCL",
            }],
        )

    def test_windows_network_discovery_probes_advertised_escl_url(self) -> None:
        capabilities = (
            b"<scan:ScannerCapabilities xmlns:scan='http://schemas.hp.com/"
            b"imaging/escl/2011/05/03'><scan:MakeAndModel>HP Network MFP"
            b"</scan:MakeAndModel></scan:ScannerCapabilities>"
        )
        with mock.patch.object(
            naps3, "_collect_mdns_packets",
            return_value=[(self._mdns_response(), "192.168.10.45")],
        ), mock.patch.object(
            naps3, "fetch_escl_capabilities", return_value=capabilities
        ) as probe:
            devices = naps3.discover_windows_escl_scanners(0.01)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "HP Network MFP")
        probe.assert_called_once_with("http://192.168.10.45:8080/eSCL", 1.5)

    def test_windows_network_search_no_longer_returns_immediately_empty(self) -> None:
        expected = [{"name": "Scanner", "ip": "192.168.10.45"}]
        with mock.patch.object(naps3, "IS_WINDOWS", True), mock.patch.object(
            naps3, "discover_windows_escl_scanners", return_value=expected
        ) as discovery:
            self.assertEqual(naps3.discover_network_scanners(), expected)
        discovery.assert_called_once_with()

    def test_dialog_keeps_discovered_escl_port_and_path(self) -> None:
        self.assertEqual(
            naps3.network_candidate_address({
                "ip": "192.168.10.45",
                "url": "http://192.168.10.45:8080/eSCL",
            }),
            "http://192.168.10.45:8080/eSCL",
        )

    def test_windows_profile_does_not_repeat_network_discovery(self) -> None:
        capabilities = (
            b"<ScannerCapabilities><MakeAndModel>Office MFP</MakeAndModel>"
            b"</ScannerCapabilities>"
        )
        with mock.patch.object(naps3, "IS_WINDOWS", True), mock.patch.object(
            naps3, "network_escl_url",
            return_value="http://192.168.10.45:8080/eSCL",
        ), mock.patch.object(
            naps3, "fetch_escl_capabilities", return_value=capabilities
        ), mock.patch.object(naps3, "discover_network_scanners") as discovery:
            profile = naps3.build_network_profile("", "192.168.10.45")

        self.assertEqual(profile["name"], "Office MFP")
        discovery.assert_not_called()

    def test_damaged_mdns_packet_is_ignored(self) -> None:
        self.assertEqual(naps3.parse_mdns_packet(b"\x00\x01"), [])

    def test_compressed_dns_name_is_decoded(self) -> None:
        packet = b"\x03www\x05local\x00\xc0\x00"
        self.assertEqual(naps3._dns_read_name(packet, 11), ("www.local", 13))


class ImageProjectTests(unittest.TestCase):
    def test_atomic_180_rotation_changes_corners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.png"
            image = Image.new("RGB", (2, 3), "white")
            image.putpixel((0, 0), (255, 0, 0))
            image.putpixel((1, 2), (0, 0, 255))
            image.save(path)
            image.close()

            naps3.rotate_page_file_180(path)
            with Image.open(path) as rotated:
                self.assertEqual(rotated.getpixel((1, 2)), (255, 0, 0))
                self.assertEqual(rotated.getpixel((0, 0)), (0, 0, 255))

    def test_rebuild_preserves_selection_when_gtk_emits_none(self) -> None:
        owner = SimpleNamespace(selected_index=1)

        class FakeRow:
            def __init__(self, _page, index: int) -> None:
                self.index = index

        class FakeListBox:
            def __init__(self) -> None:
                self.rows = [FakeRow(None, 0), FakeRow(None, 1)]
                self.selected = None

            def get_children(self):
                return list(self.rows)

            def remove(self, row):
                self.rows.remove(row)
                owner.selected_index = None  # row-selected(None)

            def add(self, row):
                self.rows.append(row)

            def show_all(self):
                return None

            def get_row_at_index(self, index):
                return self.rows[index]

            def select_row(self, row):
                self.selected = row

        owner.pages = [SimpleNamespace(), SimpleNamespace(), SimpleNamespace()]
        owner.page_list = FakeListBox()
        owner.page_count_label = SimpleNamespace(set_text=lambda _value: None)

        with mock.patch.object(naps3, "PageRow", FakeRow):
            naps3.MainWindow.rebuild_page_list(owner)

        self.assertEqual(owner.selected_index, 1)
        self.assertIs(owner.page_list.selected, owner.page_list.rows[1])

    def test_scan_conversion_does_not_publish_truncated_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "broken.pnm"
            target = root / "page.png"
            source.write_bytes(b"P6\n8 8\n255\n" + b"\x00" * 12)
            target.write_bytes(b"previous complete page")

            with self.assertRaises((OSError, naps3.Naps3Error)):
                naps3.MainWindow._convert_stream_document(
                    SimpleNamespace(), source, target
                )

            self.assertEqual(target.read_bytes(), b"previous complete page")
            self.assertFalse(any(root.glob("*.part")))

    def test_flatbed_rejects_incomplete_scan_before_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scan_dir = Path(directory)
            commands: list[list[str]] = []

            class FakeProcess:
                returncode = 0
                stderr = SimpleNamespace(close=lambda: None)

                def communicate(self):
                    return None, b""

            def fake_popen(command, *, stdout, **_kwargs):
                commands.append(command)
                stdout.write(b"P6\n8 8\n255\n" + b"\x00" * 12)
                stdout.flush()
                return FakeProcess()

            owner = SimpleNamespace(
                current_process=None,
                cancel_requested=False,
                set_status=lambda *_args: None,
                _set_device_profile=lambda *_args: None,
            )
            owner._convert_stream_document = lambda source, target, lineart=False: (
                naps3.MainWindow._convert_stream_document(
                    owner, source, target, lineart
                )
            )

            with mock.patch.object(naps3.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(
                naps3.GLib, "idle_add", side_effect=lambda function, *args: function(*args)
            ):
                files, error, _profile = naps3.MainWindow._scan_once(
                    owner,
                    scan_dir,
                    {"device_id": "test:scanner", "connection_kind": "network"},
                    "Flatbed",
                    "Color",
                    150,
                    "A4",
                )

            self.assertEqual(files, [])
            self.assertIn("неполное изображение", error)
            self.assertFalse((scan_dir / "page-0001.png").exists())
            self.assertIn("--format=pnm", commands[0])


class ExportHarness:
    _load_export_image = naps3.MainWindow._load_export_image
    _save_image = naps3.MainWindow._save_image
    _export_worker = naps3.MainWindow._export_worker

    def __init__(self, pages: list[naps3.Page]) -> None:
        self.pages = pages


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.pages: list[naps3.Page] = []
        for index, color in enumerate(("red", "green", "blue"), 1):
            path = self.directory / f"page-{index}.png"
            with Image.new("RGB", (24 + index, 30 + index), color) as image:
                image.save(path)
            self.pages.append(naps3.Page(path, path.name))
        self.harness = ExportHarness(self.pages)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exports_multipage_pdf(self) -> None:
        output = self.directory / "result.pdf"
        result = self.harness._export_worker({
            "mode": "pdf", "quality": 90, "dpi": 300, "output": output,
        })
        self.assertEqual(result["path"], output)
        self.assertTrue(output.read_bytes().startswith(b"%PDF-"))
        metadata = subprocess.run(
            ["pdfinfo", str(output)], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
        self.assertRegex(metadata, r"(?m)^Pages:\s+3$")

    def test_exports_multipage_tiff(self) -> None:
        output = self.directory / "result.tiff"
        self.harness._export_worker({
            "mode": "tiff_multi", "quality": 90, "dpi": 300,
            "output": output,
        })
        with Image.open(output) as tiff:
            self.assertEqual(getattr(tiff, "n_frames", 1), 3)

    def test_exports_each_supported_single_file_format(self) -> None:
        for file_format, info in naps3.FORMAT_INFO.items():
            with self.subTest(file_format=file_format):
                output = self.directory / f"single{info['extension']}"
                self.harness._export_worker({
                    "mode": "selected_image", "quality": 85, "dpi": 300,
                    "format": file_format, "output": output, "page_index": 0,
                })
                self.assertGreater(output.stat().st_size, 0)
                if file_format == "PDF":
                    self.assertTrue(output.read_bytes().startswith(b"%PDF-"))
                    metadata = subprocess.run(
                        ["pdfinfo", str(output)], check=True, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    ).stdout
                    self.assertRegex(metadata, r"(?m)^Pages:\s+1$")
                else:
                    with Image.open(output) as exported:
                        exported.verify()

    def test_exports_numbered_image_set(self) -> None:
        outputs = [self.directory / f"scan-{index:03d}.jpg" for index in range(1, 4)]
        result = self.harness._export_worker({
            "mode": "images_all", "quality": 80, "dpi": 200,
            "format": "JPEG", "outputs": outputs,
            "output_dir": self.directory,
        })
        self.assertEqual(result["kind"], "folder")
        self.assertTrue(all(path.stat().st_size > 0 for path in outputs))

    def test_exports_numbered_single_page_pdf_set(self) -> None:
        outputs = [
            self.directory / f"scan-{index:03d}.pdf"
            for index in range(1, 4)
        ]
        result = self.harness._export_worker({
            "mode": "images_all", "quality": 90, "dpi": 300,
            "format": "PDF", "outputs": outputs,
            "output_dir": self.directory,
        })
        self.assertEqual(result["kind"], "folder")
        self.assertEqual(len(outputs), 3)
        for output in outputs:
            self.assertTrue(output.read_bytes().startswith(b"%PDF-"))
            metadata = subprocess.run(
                ["pdfinfo", str(output)], check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout
            self.assertRegex(metadata, r"(?m)^Pages:\s+1$")

    def test_pdf_is_default_export_format(self) -> None:
        self.assertEqual(naps3.DEFAULT_EXPORT_FORMAT, "PDF")


class CancellationTests(unittest.TestCase):
    def test_cancel_ready_resets_ui_without_error_dialog(self) -> None:
        calls: list[tuple[str, object]] = []
        owner = SimpleNamespace(
            cancel_requested=True,
            current_process=object(),
            current_escl_job_url="http://scanner/job",
            current_escl_response=object(),
            set_busy=lambda value: calls.append(("busy", value)),
            set_status=lambda value: calls.append(("status", value)),
            show_error=lambda *_args: calls.append(("error", True)),
        )
        result = naps3.MainWindow._scan_ready(owner, [], "backend error", {})
        self.assertFalse(result)
        self.assertFalse(owner.cancel_requested)
        self.assertIsNone(owner.current_process)
        self.assertNotIn(("error", True), calls)
        self.assertIn(("busy", False), calls)
        self.assertIn(("status", "Сканирование отменено"), calls)

    def test_network_response_is_closed_off_the_ui_thread(self) -> None:
        closed = threading.Event()

        class FakeResponse:
            def close(self) -> None:
                closed.set()

        statuses: list[str] = []
        owner = SimpleNamespace(
            is_busy=True,
            cancel_requested=False,
            current_process=None,
            current_escl_job_url=None,
            current_escl_response=FakeResponse(),
            set_status=statuses.append,
        )
        started = time.monotonic()
        naps3.MainWindow.cancel_operation(owner)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(closed.wait(1.0))
        self.assertTrue(owner.cancel_requested)

    def test_stubborn_process_is_force_killed(self) -> None:
        code = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM, lambda *_: None);"
            "time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], start_new_session=True
        )
        try:
            time.sleep(0.1)
            started = time.monotonic()
            naps3.terminate_subprocess(process, grace_seconds=0.15)
            self.assertIsNotNone(process.poll())
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def test_cancel_operation_returns_ui_without_waiting_for_process(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        statuses: list[str] = []
        owner = SimpleNamespace(
            is_busy=True,
            cancel_requested=False,
            current_process=process,
            current_escl_job_url=None,
            current_escl_response=None,
            set_status=statuses.append,
        )
        try:
            started = time.monotonic()
            naps3.MainWindow.cancel_operation(owner)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(owner.cancel_requested)
            process.wait(timeout=3)
            self.assertIn("Операция отменяется…", statuses)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait(timeout=2)

    @unittest.skipIf(os.name == "nt", "SANE subprocess test is Linux-specific")
    def test_sane_adf_worker_exits_after_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_scanimage = root / "scanimage"
            fake_scanimage.write_text(
                "#!/usr/bin/env python3\n"
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, lambda *_: None)\n"
                "print('Scanning page 1', flush=True)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            fake_scanimage.chmod(0o755)

            statuses: list[str] = []
            owner = SimpleNamespace(
                current_process=None,
                current_escl_job_url=None,
                current_escl_response=None,
                cancel_requested=False,
                is_busy=True,
                set_status=statuses.append,
                _set_device_profile=lambda *_args: None,
                _convert_stream_document=lambda *_args: None,
            )
            result: dict[str, object] = {}

            def run_scan() -> None:
                try:
                    naps3.MainWindow._scan_once(
                        owner,
                        root / "output",
                        {
                            "device_id": "test:device",
                            "connection_kind": "network",
                        },
                        "ADF",
                        "Color",
                        300,
                        "A4",
                    )
                except BaseException as exc:  # stored for the test thread
                    result["exception"] = exc

            (root / "output").mkdir()
            environment = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}"}
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
                naps3.GLib, "idle_add", side_effect=lambda function, *args: function(*args)
            ):
                worker = threading.Thread(target=run_scan, daemon=True)
                worker.start()
                deadline = time.monotonic() + 2.0
                while owner.current_process is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNotNone(owner.current_process)
                naps3.MainWindow.cancel_operation(owner)
                worker.join(timeout=4.0)

            self.assertFalse(worker.is_alive())
            self.assertIsInstance(result.get("exception"), naps3.Naps3Error)
            self.assertIn("отменено", str(result["exception"]))
            self.assertIsNone(owner.current_process)


if __name__ == "__main__":
    unittest.main(verbosity=2)
