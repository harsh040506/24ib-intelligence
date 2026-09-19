"""HTML → PDF export.

Uses Playwright/Chromium when available (best fidelity for the report's CSS).
Degrades gracefully: if Playwright isn't installed the caller is told to use the
browser's "Print → Save as PDF" on the standalone report page, which is styled
with an @media print stylesheet for exactly that purpose.
"""
from __future__ import annotations

from pathlib import Path


class PdfUnavailable(RuntimeError):
    """Raised when no server-side PDF backend is installed."""


def is_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def html_to_pdf(html: str, out_path: Path) -> Path:
    """Render a full HTML document string to a PDF file."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover
        raise PdfUnavailable(
            "Server-side PDF needs Playwright. Install it with "
            "`pip install playwright && playwright install chromium`, "
            "or use the browser's Print → Save as PDF on the report page."
        ) from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.emulate_media(media="print")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
        )
        browser.close()
    return out_path
