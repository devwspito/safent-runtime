"""DownloadHandler: captura y validación de descargas del browser.

T811 — US6/Phase 8.

Implementa FR-020: captura binario via Playwright download event, valida
MIME contra una whitelist por flow, rechaza tipos no esperados sin abortar
el flow completo (el caller decide cómo manejar el rechazo).

La whitelist de MIME es responsabilidad del consumer (FlowSpec). El
runtime valida; el consumer define qué tipos son aceptables.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_MIME_WHITELIST = (
    "application/pdf",
    "application/xml",
    "text/xml",
    "text/csv",
    "application/zip",
)


class DownloadRejected(RuntimeError):
    """La descarga fue rechazada por la whitelist de MIME.

    Attributes:
        reason: código de rechazo (actualmente siempre "mime").
        mime:   MIME detectado del fichero descargado.
    """

    def __init__(self, *, reason: str, mime: str | None = None) -> None:
        super().__init__(
            f"Download rejected: reason={reason!r}, mime={mime!r}"
        )
        self.reason = reason
        self.mime = mime


async def capture_download(
    download_event: object,
    *,
    mime_whitelist: tuple[str, ...] = _DEFAULT_MIME_WHITELIST,
    save_dir: Path,
) -> Path:
    """Captura una descarga y valida el MIME contra la whitelist.

    Args:
        download_event: objeto Download de Playwright (duck-typed para tests).
        mime_whitelist: tipos MIME permitidos. El caller provee la lista del
            FlowSpec.
        save_dir: directorio donde se guarda el binario descargado.

    Returns:
        Path al fichero guardado.

    Raises:
        DownloadRejected: si el MIME no está en la whitelist. El caller
            decide si abortar el flow o continuar.
    """
    suggested_filename = _get_suggested_filename(download_event)
    save_path = save_dir / suggested_filename

    await _save_download(download_event, save_path)

    mime = _detect_mime(save_path)
    if not _mime_in_whitelist(mime, mime_whitelist):
        logger.warning(
            "hermes.browser.download.rejected",
            extra={
                "mime": mime,
                "whitelist": list(mime_whitelist),
                "suggested_filename": suggested_filename,
            },
        )
        raise DownloadRejected(reason="mime", mime=mime)

    logger.info(
        "hermes.browser.download.captured",
        extra={
            "mime": mime,
            "suggested_filename": suggested_filename,
            "save_path": str(save_path),
        },
    )
    return save_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_suggested_filename(download_event: object) -> str:
    name = getattr(download_event, "suggested_filename", None)
    if name:
        return str(name)
    name = getattr(download_event, "filename", None)
    return str(name) if name else "download.bin"


async def _save_download(download_event: object, save_path: Path) -> None:
    """Guarda el binario via Playwright Download.save_as o bytes fallback."""
    save_as = getattr(download_event, "save_as", None)
    if callable(save_as):
        await save_as(str(save_path))
        return

    # Fallback para tests: download_event puede tener .content bytes
    content = getattr(download_event, "content", None)
    if isinstance(content, bytes):
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(content)  # noqa: ASYNC240 — small stub write, not blocking in practice
        return

    # Fallback via path attribute
    path = getattr(download_event, "path", None)
    if path:
        import shutil  # noqa: PLC0415
        shutil.copy(str(path), str(save_path))


def _detect_mime(path: Path) -> str:
    """Detecta MIME via python-magic o extensión fallback."""
    try:
        import magic  # noqa: PLC0415 — lazy-import
        return magic.from_file(str(path), mime=True)
    except ImportError:
        pass

    ext = path.suffix.lower()
    fallback_map = {
        ".pdf": "application/pdf",
        ".xml": "application/xml",
        ".csv": "text/csv",
        ".txt": "text/plain",
        ".zip": "application/zip",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".png": "image/png",
        ".jpg": "image/jpeg",
    }
    return fallback_map.get(ext, "application/octet-stream")


def _mime_in_whitelist(mime: str, whitelist: tuple[str, ...]) -> bool:
    if mime in whitelist:
        return True
    xml_family = {"application/xml", "text/xml"}
    return mime in xml_family and any(m in xml_family for m in whitelist)
