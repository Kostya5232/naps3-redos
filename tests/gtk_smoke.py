#!/usr/bin/env python3
"""GTK smoke test. Run under Xvfb; it never opens on the user's desktop."""

from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import naps3
from gi.repository import Gtk


def combo_ids(combo: Gtk.ComboBoxText) -> list[str]:
    model = combo.get_model()
    id_column = combo.get_id_column()
    return [str(row[id_column]) for row in model]


def drain_events() -> None:
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


def main() -> int:
    initialized, _arguments = Gtk.init_check([])
    if not initialized:
        raise RuntimeError("GTK did not connect to the virtual display")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        naps3.CONFIG_DIR = root / "config"
        naps3.CACHE_DIR = root / "cache"
        naps3.CONFIG_FILE = naps3.CONFIG_DIR / "settings.json"
        naps3.FAST_SANE_DIR = naps3.CACHE_DIR / "sane-fast"
        naps3.USB_SANE_DIR = naps3.CACHE_DIR / "sane-usb-hp"
        naps3.SCAN_LOG_FILE = naps3.CACHE_DIR / "scan.log"

        application = Gtk.Application(application_id="ru.redos.NAPS3.SmokeTest")
        application.register(None)
        with mock.patch.object(
            naps3.MainWindow, "initialize_scanner_profile", lambda _self: None
        ):
            window = naps3.MainWindow(application)
        window.show_all()
        window.cancel_button.hide()
        drain_events()

        first = root / "first.png"
        second = root / "second.png"
        with Image.new("RGB", (120, 180), "white") as image:
            image.save(first)
        with Image.new("RGB", (160, 100), "gray") as image:
            image.save(second)
        window.add_pages([first, second])
        drain_events()

        assert window.selected_index == 0
        assert window.page_list.get_selected_row().get_index() == 0
        assert window.has_unsaved_pages()
        window.rotate_selected(90)
        drain_events()
        assert window.selected_index == 0
        assert window.page_list.get_selected_row().get_index() == 0

        window.set_busy(True, "scan", cancellable=True)
        drain_events()
        assert window.cancel_button.get_visible()
        assert not window.scan_button.get_sensitive()
        assert not window.save_button.get_sensitive()
        assert all(not widget.get_sensitive() for widget in window.busy_widgets)
        revision = window.document_revision
        page_paths = [page.path for page in window.pages]
        window.rotate_selected(90)
        window.move_selected(1)
        window.delete_selected()
        assert window.document_revision == revision
        assert [page.path for page in window.pages] == page_paths
        window.set_busy(False)
        drain_events()
        assert not window.cancel_button.get_visible()
        assert window.scan_button.get_sensitive()
        assert window.save_button.get_sensitive()

        window._update_source_options({
            "adf_capabilities_known": True,
            "adf_present": True,
            "adf_duplex_supported": False,
        })
        assert combo_ids(window.source_combo) == [
            "ADF", "ADF Manual Duplex", "Flatbed"
        ]
        window._update_source_options({
            "adf_capabilities_known": True,
            "adf_present": True,
            "adf_duplex_supported": True,
        })
        assert combo_ids(window.source_combo) == [
            "ADF", "ADF Duplex", "Flatbed"
        ]

        window.destroy()
        drain_events()

    print("GTK smoke test: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
