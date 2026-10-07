"""Safety checks for the optional, user-local MA4000x driver setup."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kyocera_driver  # noqa: E402


class KyoceraDriverTests(unittest.TestCase):
    def test_usb_detection_requires_exact_vendor_and_product(self):
        with tempfile.TemporaryDirectory() as temporary:
            devices = Path(temporary)
            other = devices / "1-1"
            other.mkdir()
            (other / "idVendor").write_text("0482\n", encoding="ascii")
            (other / "idProduct").write_text("0de1\n", encoding="ascii")
            self.assertFalse(kyocera_driver.usb_device_present(devices))

            matching = devices / "1-2"
            matching.mkdir()
            (matching / "idVendor").write_text("0482\n", encoding="ascii")
            (matching / "idProduct").write_text("0de0\n", encoding="ascii")
            self.assertTrue(kyocera_driver.usb_device_present(devices))

    def test_unverified_archive_does_not_create_driver_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "driver"
            with self.assertRaises(kyocera_driver.KyoceraDriverError):
                kyocera_driver.install_verified_archive(b"not an official driver", destination)
            self.assertFalse(destination.exists())

    def test_missing_installation_does_not_override_sane(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(kyocera_driver.sane_environment(Path(temporary)))


if __name__ == "__main__":
    unittest.main()
