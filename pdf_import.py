"""PDF rendering fallback for Windows bundles without Poppler."""

from __future__ import annotations

from pathlib import Path


def pdfium_available() -> bool:
    try:
        from pypdfium2._helpers import PdfDocument  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def render_pdf_to_png(
    source: Path,
    prefix: Path,
    *,
    dpi: int = 160,
    max_pages: int = 200,
) -> list[Path]:
    from pypdfium2._helpers import PdfDocument

    document = PdfDocument(str(source))
    outputs: list[Path] = []
    try:
        page_count = len(document)
        if page_count < 1 or page_count > max_pages:
            raise ValueError(
                f"PDF содержит {page_count} страниц; предел импорта — {max_pages}."
            )
        for index in range(page_count):
            page = document[index]
            try:
                image = page.render_topil(scale=dpi / 72)
                try:
                    output = prefix.parent / f"{prefix.name}-{index + 1}.png"
                    try:
                        image.save(output, format="PNG")
                    except Exception:
                        output.unlink(missing_ok=True)
                        raise
                    outputs.append(output)
                finally:
                    image.close()
            finally:
                page.close()
        return outputs
    except Exception:
        for output in outputs:
            output.unlink(missing_ok=True)
        raise
    finally:
        document.close()
