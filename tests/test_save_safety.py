from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import naps3
from test_naps3 import ExportHarness


class SafeExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pages = []
        for index, color in enumerate(("red", "green", "blue")):
            path = self.root / f"source-{index}.png"
            with Image.new("RGB", (12, 18), color) as image:
                image.save(path)
            self.pages.append(naps3.Page(path, path.name))
        self.owner = ExportHarness(self.pages)
        self.outputs = [self.root / f"saved-{index}.png" for index in range(3)]

    def job(self, mode="images_all", **overrides):
        return {
            "mode": mode, "quality": 90, "dpi": 300, "format": "PNG",
            "outputs": self.outputs, "output_dir": self.root,
            "output": self.outputs[0], "page_index": 0, **overrides,
        }

    def existing_outputs(self):
        for index, path in enumerate(self.outputs):
            path.write_bytes(f"previous file {index}".encode())
        return [path.read_bytes() for path in self.outputs]

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.glob(".naps3-*.tmp")), [])

    def test_encoding_failure_preserves_existing_file_for_every_mode(self):
        def fail_save(_image, filename, *_args, **_kwargs):
            Path(filename).write_bytes(b"incomplete output")
            raise OSError("disk full")

        for mode in ("pdf", "tiff_multi", "selected_image", "images_all"):
            with self.subTest(mode=mode):
                before = self.existing_outputs()
                with mock.patch.object(Image.Image, "save", fail_save):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        self.owner._export_worker(self.job(mode))
                self.assertEqual([path.read_bytes() for path in self.outputs], before)
                self.assert_no_temporary_files()

    def test_second_page_failure_leaves_entire_existing_set_unchanged(self):
        before = self.existing_outputs()
        original = self.owner._save_image
        def save(page, output, *args, **kwargs):
            if page == self.pages[1]:
                raise OSError("cannot encode second page")
            original(page, output, *args, **kwargs)
        with mock.patch.object(self.owner, "_save_image", side_effect=save):
            with self.assertRaises(OSError):
                self.owner._export_worker(self.job())
        self.assertEqual([path.read_bytes() for path in self.outputs], before)
        self.assert_no_temporary_files()

    def test_failed_commit_restores_replaced_files(self):
        before = self.existing_outputs()
        replace = os.replace
        def fail_second(source, target):
            if target == self.outputs[1]:
                raise PermissionError("destination locked")
            return replace(source, target)
        with mock.patch.object(naps3.os, "replace", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                self.owner._export_worker(self.job())
        self.assertEqual([path.read_bytes() for path in self.outputs], before)
        self.assert_no_temporary_files()

    def test_failed_commit_removes_new_files_already_published(self):
        replace = os.replace
        def fail_second(source, target):
            if target == self.outputs[1]:
                raise PermissionError("destination locked")
            return replace(source, target)
        with mock.patch.object(naps3.os, "replace", side_effect=fail_second):
            with self.assertRaises(PermissionError):
                self.owner._export_worker(self.job())
        self.assertFalse(any(path.exists() for path in self.outputs))
        self.assert_no_temporary_files()

    def test_failed_rollback_retains_old_file_and_reports_its_location(self):
        before = self.existing_outputs()
        replace = os.replace
        def fail_commit_and_rollback(source, target):
            if target == self.outputs[1] or Path(source).name.startswith(".naps3-backup-"):
                raise PermissionError("destination locked")
            return replace(source, target)
        with mock.patch.object(naps3.os, "replace", side_effect=fail_commit_and_rollback):
            with self.assertRaises(naps3.Naps3Error) as raised:
                self.owner._export_worker(self.job())
        backups = list(self.root.glob(".naps3-backup-*.tmp"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before[0])
        self.assertIn(str(backups[0]), str(raised.exception))
        self.assertEqual(self.outputs[1].read_bytes(), before[1])

    def test_sync_and_backup_failures_preserve_originals(self):
        for function in ("fsync", "copy2"):
            with self.subTest(function=function):
                before = self.existing_outputs()
                target = naps3.os if function == "fsync" else naps3.shutil
                with mock.patch.object(target, function, side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        self.owner._export_worker(self.job())
                self.assertEqual([path.read_bytes() for path in self.outputs], before)
                self.assert_no_temporary_files()

    def test_file_changed_or_created_during_encoding_is_not_overwritten(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                for path in self.outputs:
                    path.unlink(missing_ok=True)
                if existing:
                    self.existing_outputs()
                original = self.owner._save_image
                def save(page, output, *args, **kwargs):
                    original(page, output, *args, **kwargs)
                    if page == self.pages[-1]:
                        self.outputs[0].write_bytes(b"written by another application")
                with mock.patch.object(self.owner, "_save_image", side_effect=save):
                    with self.assertRaisesRegex(naps3.Naps3Error, "изменился"):
                        self.owner._export_worker(self.job())
                self.assertEqual(self.outputs[0].read_bytes(), b"written by another application")
                self.assert_no_temporary_files()

    def test_file_changed_since_user_confirmation_is_preserved(self):
        self.existing_outputs()
        states = {path: naps3.export_target_state(path) for path in self.outputs}
        self.outputs[0].write_bytes(b"new external version")
        with self.assertRaisesRegex(naps3.Naps3Error, "изменился"):
            self.owner._export_worker(self.job(target_states=states))
        self.assertEqual(self.outputs[0].read_bytes(), b"new external version")
        self.assert_no_temporary_files()

    def test_export_cannot_overwrite_source_or_its_hard_link(self):
        source = self.pages[0].path
        alias = self.root / "source-alias.png"
        os.link(source, alias)
        before = source.read_bytes()
        for output in (source, alias):
            with self.subTest(output=output):
                with self.assertRaisesRegex(naps3.Naps3Error, "рабочей страницы"):
                    self.owner._export_worker(self.job("pdf", output=output))
        self.assertEqual(source.read_bytes(), before)
        self.assert_no_temporary_files()

    def test_output_count_and_duplicate_paths_are_rejected_before_writing(self):
        for outputs in (self.outputs[:2], [self.outputs[0]] * 3):
            with self.subTest(outputs=outputs):
                with self.assertRaises(naps3.Naps3Error):
                    self.owner._export_worker(self.job(outputs=outputs))
        self.assertFalse(any(path.exists() for path in self.outputs))
        self.assert_no_temporary_files()

    def test_snapshot_preserves_page_order_when_live_list_changes(self):
        snapshot = tuple(self.pages)
        self.owner.pages = list(reversed(self.pages))
        self.owner._export_worker(self.job("tiff_multi", pages=snapshot))
        with Image.open(self.outputs[0]) as image:
            self.assertEqual(image.n_frames, 3)
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
            image.seek(2)
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))

    def test_loading_failure_closes_images_already_loaded(self):
        self.outputs[0].write_bytes(b"previous PDF")
        image = Image.new("RGB", (12, 18))
        with mock.patch.object(self.owner, "_load_export_image", side_effect=[image, OSError("bad image")]):
            with self.assertRaises(OSError):
                self.owner._export_worker(self.job("pdf"))
        with self.assertRaises(ValueError):
            image.getpixel((0, 0))
        self.assertEqual(self.outputs[0].read_bytes(), b"previous PDF")
        self.assert_no_temporary_files()

    def test_successful_export_replaces_existing_file_in_every_format(self):
        for image_format in naps3.FORMAT_INFO:
            with self.subTest(image_format=image_format):
                self.outputs[0].write_bytes(b"previous output")
                self.owner._export_worker(self.job("selected_image", format=image_format))
                if image_format == "PDF":
                    data = self.outputs[0].read_bytes()
                    self.assertTrue(data.startswith(b"%PDF-"))
                    self.assertEqual(len(re.findall(rb"/Type /Page\b", data)), 1)
                    self.assertIn(self.outputs[0].stem.encode("utf-16-be"), data)
                else:
                    with Image.open(self.outputs[0]) as image:
                        self.assertEqual(image.size, (12, 18))
                        image.verify()
                self.assert_no_temporary_files()

    def test_multipage_and_numbered_pdf_keep_page_counts(self):
        self.owner._export_worker(self.job("pdf"))
        self.assertEqual(len(re.findall(rb"/Type /Page\b", self.outputs[0].read_bytes())), 3)
        self.owner._export_worker(self.job(format="PDF"))
        for path in self.outputs:
            self.assertEqual(len(re.findall(rb"/Type /Page\b", path.read_bytes())), 1)
        self.assert_no_temporary_files()

    def test_selected_page_can_be_saved_when_another_source_is_missing(self):
        self.pages[1].path.unlink()
        result = self.owner._export_worker(self.job("selected_image"))
        self.assertFalse(result["saves_document"])
        with Image.open(self.outputs[0]) as image:
            image.verify()


class DocumentHarness(ExportHarness):
    has_unsaved_pages = naps3.MainWindow.has_unsaved_pages
    do_delete_event = naps3.MainWindow.do_delete_event
    _export_ready = naps3.MainWindow._export_ready
    add_pages = naps3.MainWindow.add_pages
    rotate_selected = naps3.MainWindow.rotate_selected
    move_selected = naps3.MainWindow.move_selected
    delete_selected = naps3.MainWindow.delete_selected
    clear_pages = naps3.MainWindow.clear_pages

    def __init__(self):
        self.pages = []
        self.document_revision = self.saved_revision = 0
        self.is_busy = self.is_exporting = False
        self.selected_index = None
        for name in (
            "rebuild_page_list", "render_preview", "_update_page_actions",
            "set_status", "show_error", "show_info", "cancel_operation",
            "_save_ui_settings", "close", "export_pages", "_confirm_unsaved_close",
        ):
            setattr(self, name, mock.Mock())
        self.ask_yes_no = mock.Mock(return_value=False)

    def set_busy(self, value, *_args):
        self.is_busy = value


class DocumentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.owner = DocumentHarness()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = [self.root / f"page-{index}.png" for index in range(2)]
        for path in self.files:
            with Image.new("RGB", (12, 18), "white") as image:
                image.save(path)
        self.responses = SimpleNamespace(ACCEPT=1, REJECT=2, CANCEL=3, OK=4)
        patcher = mock.patch.object(naps3.Gtk, "ResponseType", self.responses, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def full_result(self, **overrides):
        return {
            "kind": "file", "path": self.root / "saved.pdf", "message": "saved",
            "document_revision": self.owner.document_revision,
            "saves_document": True, **overrides,
        }

    def test_empty_or_fully_saved_document_closes_without_warning(self):
        self.assertFalse(self.owner.do_delete_event(None))
        self.owner.add_pages(self.files)
        self.owner._export_ready(self.full_result(), "")
        self.assertFalse(self.owner.has_unsaved_pages())
        self.assertFalse(self.owner.do_delete_event(None))
        self.owner._confirm_unsaved_close.assert_not_called()

    def test_editing_handlers_and_shortcuts_cannot_change_busy_document(self):
        self.owner.add_pages(self.files)
        before = [path.read_bytes() for path in self.files]
        revision = self.owner.document_revision
        self.owner.is_busy = True
        self.owner.rotate_selected(90)
        self.owner.move_selected(1)
        self.owner.delete_selected()
        self.owner.clear_pages()
        self.assertEqual([page.path for page in self.owner.pages], self.files)
        self.assertEqual([path.read_bytes() for path in self.files], before)
        self.assertEqual(self.owner.document_revision, revision)
        self.owner.ask_yes_no.assert_not_called()

    def test_rotation_reordering_and_deletion_make_saved_document_dirty(self):
        self.owner.add_pages(self.files)
        for action in (
            lambda: self.owner.rotate_selected(90),
            lambda: self.owner.move_selected(1),
            self.owner.delete_selected,
        ):
            self.owner.saved_revision = self.owner.document_revision
            action()
            self.assertTrue(self.owner.has_unsaved_pages())

    def test_clear_or_delete_last_page_leaves_empty_document_without_warning(self):
        self.owner.add_pages(self.files[:1])
        self.owner.delete_selected()
        self.assertFalse(self.owner.has_unsaved_pages())
        self.owner.add_pages(self.files)
        self.owner.ask_yes_no.return_value = True
        self.owner.clear_pages()
        self.assertFalse(self.owner.has_unsaved_pages())

    def test_partial_or_failed_export_does_not_mark_document_saved(self):
        self.owner.add_pages(self.files)
        self.owner._export_ready(self.full_result(saves_document=False), "")
        self.assertTrue(self.owner.has_unsaved_pages())
        self.owner.is_exporting = self.owner.is_busy = True
        self.owner._export_ready({}, "disk full")
        self.assertTrue(self.owner.has_unsaved_pages())
        self.assertFalse(self.owner.is_exporting)
        self.assertFalse(self.owner.is_busy)
        self.owner.show_error.assert_called_once()
        self.owner.close.assert_not_called()

    def test_old_export_result_does_not_mark_new_changes_saved(self):
        self.owner.add_pages(self.files[:1])
        result = self.full_result()
        self.owner.add_pages(self.files[1:])
        self.owner._export_ready(result, "")
        self.assertTrue(self.owner.has_unsaved_pages())

    def test_cancel_close_keeps_unsaved_pages_and_does_not_cancel_operation(self):
        self.owner.add_pages(self.files)
        self.owner._confirm_unsaved_close.return_value = self.responses.CANCEL
        self.assertTrue(self.owner.do_delete_event(None))
        self.assertEqual(len(self.owner.pages), 2)
        self.owner.cancel_operation.assert_not_called()
        self.owner._save_ui_settings.assert_not_called()

    def test_explicit_discard_allows_closing(self):
        self.owner.add_pages(self.files)
        self.owner._confirm_unsaved_close.return_value = self.responses.REJECT
        self.assertFalse(self.owner.do_delete_event(None))
        self.owner._save_ui_settings.assert_called_once()

    def test_save_on_close_waits_for_successful_full_export(self):
        self.owner.add_pages(self.files)
        self.owner._confirm_unsaved_close.return_value = self.responses.ACCEPT
        self.assertTrue(self.owner.do_delete_event(None))
        self.owner.export_pages.assert_called_once_with(close_after_save=True)
        self.owner.close.assert_not_called()
        self.owner._export_ready(self.full_result(close_after_save=True), "")
        self.owner.close.assert_called_once()
        self.assertFalse(self.owner.has_unsaved_pages())

    def test_partial_export_after_close_prompt_keeps_window_open(self):
        self.owner.add_pages(self.files)
        self.owner._export_ready(self.full_result(saves_document=False, close_after_save=True), "")
        self.owner.close.assert_not_called()
        self.assertTrue(self.owner.has_unsaved_pages())

    def test_close_during_export_does_not_interrupt_writer(self):
        self.owner.add_pages(self.files)
        self.owner.is_exporting = self.owner.is_busy = True
        self.assertTrue(self.owner.do_delete_event(None))
        self.owner.show_info.assert_called_once()
        self.owner.cancel_operation.assert_not_called()
        self.owner._confirm_unsaved_close.assert_not_called()

    def test_close_during_other_operation_mentions_unsaved_document(self):
        self.owner.add_pages(self.files)
        self.owner.is_busy = True
        self.assertTrue(self.owner.do_delete_event(None))
        self.assertIn("несохранённые", self.owner.ask_yes_no.call_args.args[1])

    def test_appended_extension_requires_confirmation_for_actual_destination(self):
        target = self.root / "document.pdf"
        target.write_bytes(b"existing PDF")
        dialog = mock.Mock()
        dialog.run.return_value = self.responses.OK
        dialog.get_filename.return_value = str(self.root / "document")
        with mock.patch.object(naps3, "FileChooserSortGuard"), mock.patch.object(
            naps3.Gtk, "FileChooserDialog", return_value=dialog, create=True
        ), mock.patch.object(naps3.Gtk, "FileChooserAction", SimpleNamespace(SAVE=1), create=True), mock.patch.object(
            naps3.Gtk, "FileFilter", create=True
        ):
            result = naps3.MainWindow.choose_save_file(self.owner, "save", "document", ".pdf", "PDF")
        self.assertIsNone(result)
        self.assertIn(str(target), self.owner.ask_yes_no.call_args.args[1])
        self.assertEqual(target.read_bytes(), b"existing PDF")

    def run_export_dialog(self, mode, *, start_failure=False, output_cancelled=False):
        dialog = mock.Mock()
        dialog.run.return_value = self.responses.OK
        dialog.get_result.return_value = {"mode": mode, "format": "PNG", "quality": 90}
        self.owner.dpi_combo = SimpleNamespace(get_active_id=lambda: "300")
        self.owner.choose_save_file = mock.Mock(
            return_value=None if output_cancelled else self.root / "result.pdf"
        )
        def thread(*, target, daemon):
            return SimpleNamespace(start=(
                mock.Mock(side_effect=RuntimeError("cannot start thread"))
                if start_failure else target
            ))
        with mock.patch.object(naps3, "ExportDialog", return_value=dialog), mock.patch.object(
            naps3.threading, "Thread", side_effect=thread
        ), mock.patch.object(naps3.GLib, "idle_add", side_effect=lambda callback, *args: callback(*args)):
            naps3.MainWindow.export_pages(self.owner, close_after_save=True)

    def test_save_on_close_runs_real_export_and_respects_selected_page_scope(self):
        for mode, count, closes in (("pdf", 2, True), ("selected_image", 2, False), ("selected_image", 1, True)):
            with self.subTest(mode=mode, count=count):
                self.owner = DocumentHarness()
                self.owner.add_pages(self.files[:count])
                self.run_export_dialog(mode)
                self.owner.show_error.assert_not_called()
                self.assertFalse(self.owner.is_busy)
                self.assertFalse(self.owner.is_exporting)
                self.assertEqual(self.owner.close.called, closes)
                self.assertEqual(self.owner.has_unsaved_pages(), not closes)

    def test_cancelled_destination_or_thread_failure_keeps_unsaved_window_usable(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                self.owner = DocumentHarness()
                self.owner.add_pages(self.files)
                self.run_export_dialog("pdf", start_failure=not cancelled, output_cancelled=cancelled)
                self.assertFalse(self.owner.is_busy)
                self.assertFalse(self.owner.is_exporting)
                self.assertTrue(self.owner.has_unsaved_pages())
                self.owner.close.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
