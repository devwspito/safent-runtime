"""Tests T803: UploadHandler — path validation NC-1 / CWE-22.

Phase 8 / US6 / T803.

Security review (T815 inline):
  - NC-1: el path viene del consumer, nunca del LLM.
  - CWE-22: path traversal mitigado via resolve(strict=True) + is_relative_to.
  - Symlink following: rechazado via resolve(strict=True).
  - MIME mismatch: rechazado via python-magic o extensión fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.browser.application.upload_handler import (
    UploadPathRejected,
    validate_upload_path,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_dir(tmp_path: Path) -> Path:
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    return upload_dir


@pytest.fixture()
def valid_pdf(base_dir: Path) -> Path:
    pdf = base_dir / "documento.pdf"
    # Escribimos un PDF mínimo con header correcto y magic bytes de PDF
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n")
    return pdf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_outside_base_dir_rejected(tmp_path: Path, base_dir: Path) -> None:
    """Path absoluto fuera de upload_base_dir → UploadPathRejected(reason='outside_base')."""
    outside_file = tmp_path / "outside.pdf"
    outside_file.write_bytes(b"%PDF-1.4\n%%EOF\n")

    with pytest.raises(UploadPathRejected) as exc_info:
        await validate_upload_path(outside_file, upload_base_dir=base_dir)

    assert exc_info.value.reason == "outside_base"


@pytest.mark.asyncio
async def test_symlink_escaping_base_rejected(tmp_path: Path, base_dir: Path) -> None:
    """Symlink que apunta fuera de base → UploadPathRejected(reason='symlink_escape')."""
    # Fichero real fuera del base_dir
    outside_file = tmp_path / "secret.pdf"
    outside_file.write_bytes(b"%PDF-1.4\n%%EOF\n")

    # Symlink dentro de base_dir apuntando fuera
    symlink = base_dir / "linked.pdf"
    symlink.symlink_to(outside_file)

    # resolve(strict=True) en el symlink resuelve al target (outside_file)
    # Luego is_relative_to(base_dir) falla → outside_base
    with pytest.raises(UploadPathRejected) as exc_info:
        await validate_upload_path(symlink, upload_base_dir=base_dir)

    # El motivo puede ser outside_base (symlink resuelto al target fuera)
    assert exc_info.value.reason in ("symlink_escape", "outside_base")


@pytest.mark.asyncio
async def test_mime_mismatch_rejected(base_dir: Path) -> None:
    """MIME-sniff mismatch: extensión .pdf pero contenido PNG.

    Con python-magic instalado: UploadPathRejected(reason='mime_mismatch').
    Sin python-magic: el fallback de extensión pasa sin verificar contenido.
    """
    fake_pdf = base_dir / "imagen.pdf"
    # Magic bytes de PNG, no de PDF
    png_magic = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    fake_pdf.write_bytes(png_magic)

    try:
        import magic  # noqa: F401 PLC0415
        magic_available = True
    except ImportError:
        magic_available = False

    if magic_available:
        with pytest.raises(UploadPathRejected) as exc_info:
            await validate_upload_path(fake_pdf, upload_base_dir=base_dir)
        assert exc_info.value.reason == "mime_mismatch"
    else:
        # Sin python-magic, la validación pasa (extensión fallback)
        result = await validate_upload_path(fake_pdf, upload_base_dir=base_dir)
        assert result is not None  # no crashea


@pytest.mark.asyncio
async def test_valid_pdf_within_base_accepted(valid_pdf: Path, base_dir: Path) -> None:
    """Happy path: PDF dentro de base con MIME correcto → upload OK."""
    result = await validate_upload_path(valid_pdf, upload_base_dir=base_dir)

    assert result is not None
    assert result.is_absolute()
    assert str(base_dir) in str(result)
