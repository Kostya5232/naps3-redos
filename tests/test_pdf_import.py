"""PDF fallback used by the Windows 7 x86 bundle."""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from pdf_import import pdfium_available, render_pdf_to_png


@unittest.skipUnless(pdfium_available(), "pypdfium2 is not installed")
class PdfiumImportTests(unittest.TestCase):
    def test_renders_each_pdf_page_without_leaving_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "two-pages.pdf"
            first = Image.new("RGB", (100, 100), "red")
            second = Image.new("RGB", (100, 100), "blue")
            first.save(source, format="PDF", save_all=True, append_images=[second])
            first.close()
            second.close()

            prefix = root / "page"
            files = render_pdf_to_png(source, prefix)
            self.assertEqual([file.name for file in files], ["page-1.png", "page-2.png"])
            with Image.open(files[0]) as image:
                self.assertGreater(image.getpixel((50, 50))[0], 200)
            with Image.open(files[1]) as image:
                self.assertGreater(image.getpixel((50, 50))[2], 200)

            for file in files:
                file.unlink()
            with self.assertRaises(ValueError):
                render_pdf_to_png(source, prefix, max_pages=1)
            self.assertEqual(list(root.glob("page-*.png")), [])


if __name__ == "__main__":
    unittest.main()
