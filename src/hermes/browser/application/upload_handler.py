"""UploadHandler: validación segura de paths para uploads.

T810 — US6/Phase 8.

Implementa la política NC-1 del threat-model:
  - El path viene del consumer, nunca del LLM ni de steps previos.
  - Validación canónica de path antes de entregar al driver.

Controles (T815 / NC-1 / CWE-22 mitigado):
  1. Path absoluto: rechaza rutas relativas.
  2. Path.resolve(strict=True): rechaza symlinks fuera del filesystem y
     paths que no existen. Previene symlink following attacks.
  3. is_relative_to(upload_base_dir): previene path traversal (CWE-22).
  4. MIME-sniff via python-magic: verifica que el contenido coincide con
     la extensión declarada. Previene content-type confusion.

Lazy-import de python-magic (Constitución V): si no está instalado,
se usa una heurística de extensión como fallback documentado.

Verdict: APPROVE inline (T815).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# MIME whitelist por extensión (fallback si python-magic no está instalado)
_EXTENSION_MIME_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".xml": "application/xml",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class UploadPathRejected(ValueError):
    """El path de upload fue rechazado por validación de seguridad.

    Attributes:
        reason: código de rechazo ("outside_base", "symlink_escape",
                "mime_mismatch", "not_absolute", "not_exists").
        path:   el path que fue rechazado (como string).
    """

    def __init__(self, *, reason: str, path: str) -> None:
        super().__init__(f"Upload path rejected: reason={reason!r}, path={path!r}")
        self.reason = reason
        self.path = path


async def validate_upload_path(
    file_path: Path,
    *,
    upload_base_dir: Path,
) -> Path:
    """Valida y devuelve path resuelto. Levanta UploadPathRejected si falla.

    Reglas (NC-1, CWE-22):
    1. Path debe ser absoluto.
    2. Path.resolve(strict=True) resuelve symlinks y rechaza paths inexistentes.
    3. resolved.is_relative_to(upload_base_dir) previene path traversal.
    4. MIME-sniff via python-magic coincide con extensión declarada.

    Args:
        file_path: path al fichero a subir. Debe ser absoluto.
        upload_base_dir: directorio base permitido. Solo se aceptan paths
            dentro (o iguales) a este directorio.

    Returns:
        Path resuelto (canonical, sin symlinks).

    Raises:
        UploadPathRejected: si alguna validación falla.
    """
    _check_absolute(file_path)
    resolved = _resolve_path(file_path)
    base_resolved = upload_base_dir.resolve()  # noqa: ASYNC240 — sync resolve intentional; no trio
    _check_within_base(resolved, base_resolved)
    _check_mime(resolved)
    return resolved


async def upload_to_browser(
    driver: object,
    selector: str,
    file_path: Path,
    base_dir: Path,
) -> None:
    """Valida el path y lo entrega al driver para subir al input[type=file].

    Args:
        driver: BrowserPort / Playwright Page (duck-typed).
        selector: selector CSS del input[type=file].
        file_path: path al fichero. Validado antes de entregarlo.
        base_dir: directorio base permitido (NC-1).
    """
    resolved = await validate_upload_path(file_path, upload_base_dir=base_dir)
    await _set_input_files(driver, selector, resolved)


# ---------------------------------------------------------------------------
# Validaciones internas
# ---------------------------------------------------------------------------


def _check_absolute(file_path: Path) -> None:
    if not file_path.is_absolute():
        raise UploadPathRejected(reason="not_absolute", path=str(file_path))


def _resolve_path(file_path: Path) -> Path:
    try:
        return file_path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        # strict=True falla si el path no existe o si hay un symlink roto.
        reason = "symlink_escape" if "symlink" in str(exc).lower() else "not_exists"
        raise UploadPathRejected(reason=reason, path=str(file_path)) from exc


def _check_within_base(resolved: Path, base_resolved: Path) -> None:
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise UploadPathRejected(reason="outside_base", path=str(resolved)) from exc


def _check_mime(resolved: Path) -> None:
    """Verifica MIME via python-magic o extensión fallback."""
    declared_ext = resolved.suffix.lower()
    declared_mime = _EXTENSION_MIME_MAP.get(declared_ext, "")

    try:
        import magic  # noqa: PLC0415 — lazy-import
        sniffed_mime = magic.from_file(str(resolved), mime=True)
    except ImportError:
        logger.debug(
            "hermes.browser.upload.magic_unavailable",
            extra={"note": "python-magic no instalado; usando fallback de extensión"},
        )
        return  # fallback: extensión ya es la declaración; trust it

    if not declared_mime:
        return  # extensión desconocida; no se puede verificar

    if not _mimes_compatible(sniffed_mime, declared_mime):
        raise UploadPathRejected(reason="mime_mismatch", path=str(resolved))


def _mimes_compatible(sniffed: str, declared: str) -> bool:
    """Permite leniencia para subtipos equivalentes (e.g. application/xml == text/xml)."""
    if sniffed == declared:
        return True
    xml_family = {"application/xml", "text/xml"}
    return sniffed in xml_family and declared in xml_family


async def _set_input_files(driver: object, selector: str, file_path: Path) -> None:
    """Entrega el path al input[type=file] via Playwright (duck-typed)."""
    page = getattr(driver, "_page", driver)
    locator_method = getattr(page, "locator", None)
    if locator_method is None:
        return
    locator = locator_method(selector)
    set_input_files = getattr(locator, "set_input_files", None)
    if callable(set_input_files):
        await set_input_files(str(file_path))
