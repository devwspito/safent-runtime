"""Tests T804: DownloadHandler — MIME whitelist.

Phase 8 / US6 / T804.

Tests verifican que:
  - MIME en whitelist → captura bytes.
  - MIME fuera → DownloadRejected sin abortar flow (caller decide).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.browser.application.download_handler import (
    DownloadRejected,
    capture_download,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockDownloadEvent:
    """Duck-type de Playwright Download para tests."""

    def __init__(
        self,
        filename: str,
        content: bytes,
    ) -> None:
        self.suggested_filename = filename
        self.content = content

    async def save_as(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.content)  # noqa: ASYNC240


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mime_in_whitelist_captures_file(tmp_path: Path) -> None:
    """MIME en whitelist (application/pdf) → captura binario, retorna path."""
    pdf_content = b"%PDF-1.4\n%%EOF\n"
    download = MockDownloadEvent(filename="documento.pdf", content=pdf_content)

    whitelist = ("application/pdf",)

    # Sin python-magic el fallback usa extensión (.pdf → application/pdf)
    result = await capture_download(
        download,
        mime_whitelist=whitelist,
        save_dir=tmp_path,
    )

    assert result is not None
    assert result.exists()
    assert result.read_bytes() == pdf_content


@pytest.mark.asyncio
async def test_mime_outside_whitelist_raises_download_rejected(tmp_path: Path) -> None:
    """MIME fuera de whitelist → DownloadRejected(reason='mime') sin abortar flow."""
    html_content = b"<html><body>Not a PDF</body></html>"
    download = MockDownloadEvent(filename="page.html", content=html_content)

    # Whitelist solo acepta PDF; HTML no está permitido
    whitelist = ("application/pdf",)

    with pytest.raises(DownloadRejected) as exc_info:
        await capture_download(
            download,
            mime_whitelist=whitelist,
            save_dir=tmp_path,
        )

    assert exc_info.value.reason == "mime"
    # El caller puede capturar la excepción y decidir cómo manejarla
    # sin que el flow completo aborte
