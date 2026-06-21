"""T060 — LibreOfficeUnoSurfaceAdapter: operar LibreOffice via UNO (determinista).

Implementa SurfaceAdapterPort para surface_kind=DESKTOP_APP con backend UNO.
UNO (Universal Network Objects) es la API de automatización nativa de LibreOffice
— determinista, sin coordenadas, sin AT-SPI. Preferido sobre clicks AT-SPI per
DESIGN.md Decisión 3 ("IA solo donde no hay determinismo").

Controles de seguridad:
  - Fail-closed: cualquier error UNO → executed_failed (sin estado a medias).
  - Contexto aislado: opera una instancia headless propia en /tmp/hermes-lo-<id>,
    NUNCA la ventana del humano (INV-2 del threat-model).
  - Spawn propio: CADA operación lanza su propio proceso soffice --headless con
    perfil único derivado de action_id (UUID). El proceso muere en el finally.
    NUNCA se conecta a una instancia preexistente del humano (INV-2).
  - Path allowlist: document_path se valida contra allowed_prefixes antes de
    pasarse a UNO (misma lógica que FilesystemSurfaceAdapter, constitución IV).
  - Solo entra al replay() si surface_kind == DESKTOP_APP (fail-closed por
    mismatch). El broker ya hizo kill-switch/consent/HITL antes de llegar aquí.
  - Operaciones soportadas: open_document, write_text, save_document.
    Toda escritura de fichero externo es HIGH en el registry (T061).
  - Si python3-uno o soffice no está disponible: import lazy + degradación honesta —
    el adapter existe pero replay() devuelve EXECUTED_FAILED con mensaje claro.

Importación UNO es lazy (sin try/except en module scope) para que el adapter
sea importable en CI sin python3-uno instalado. La disponibilidad se chequea
en replay() via _check_uno_available(). Esto sigue el patrón de agent_browser_cli
(que hace lazy import de Playwright).

El dispatcher registra este adapter como candidato para DESKTOP_APP. El broker
lo alcanza SOLO via SurfaceAdapterDispatcher.replay() → nunca directamente.

Capa: infrastructure (adapta UNO al contrato de dominio). Sin framework.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger("hermes.agents_os.libreoffice_uno")

# Nombre de display virtual para LibreOffice headless (sin DISPLAY real necesario).
_LO_HEADLESS_FLAG = "--headless"

# Prefijo de perfil temporal aislado (nunca en HOME del humano — INV-2).
_LO_PROFILE_DIR_PREFIX = "/tmp/hermes-lo-"

# Operaciones permitidas server-side (bindings en T061 apuntan a estas).
_ALLOWED_OPS: frozenset[str] = frozenset(
    {"open_document", "write_text", "save_document"}
)

# Candidate binary names for LibreOffice (distro differences).
_SOFFICE_CANDIDATES = ("soffice", "libreoffice")

# Pipe-ready poll interval and per-attempt timeout (constitución IV).
_PIPE_POLL_INTERVAL_S = 0.25


def _check_uno_available() -> bool:
    """True si python3-uno está disponible en runtime.

    Lazy check: no importamos UNO en module scope para no romper CI.
    """
    try:
        import importlib.util  # noqa: PLC0415
        return importlib.util.find_spec("uno") is not None
    except Exception:  # noqa: BLE001
        return False


def _find_soffice_binary() -> str | None:
    """Localiza el binario soffice/libreoffice en PATH. Retorna None si no existe."""
    import shutil  # noqa: PLC0415
    for candidate in _SOFFICE_CANDIDATES:
        path = shutil.which(candidate)
        if path is not None:
            return path
    return None


class LibreOfficeUnoSurfaceAdapter:
    """Adapter DESKTOP_APP para LibreOffice via UNO.

    AISLAMIENTO (INV-2): cada operación lanza su propio proceso soffice --headless
    con perfil único en /tmp/hermes-lo-<action_id>. El proceso es matado en el
    finally de _dispatch_uno_operation. NUNCA comparte instancia con el humano.

    PATH ALLOWLIST (constitución IV): document_path se valida contra
    allowed_prefixes antes de pasarse a UNO, igual que FilesystemSurfaceAdapter.

    Args:
        profile_base: directorio base para perfiles aislados.
            Default /tmp/hermes-lo-. Nunca bajo $HOME del humano.
        connect_timeout_s: timeout total para que el proceso LO escuche en el
            pipe y para conectar UNO (segundos). Abarca spawn + poll + connect.
        allowed_prefixes: paths de FS permitidos. Si None → sin restricción de
            allowlist (solo para tests sin acceso a FS real). Producción DEBE
            inyectar prefixes explícitos (constitución IV).
    """

    surface_kind: SurfaceKind = SurfaceKind.DESKTOP_APP

    def __init__(
        self,
        *,
        profile_base: str = _LO_PROFILE_DIR_PREFIX,
        connect_timeout_s: float = 15.0,
        allowed_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self._profile_base = profile_base
        self._connect_timeout_s = connect_timeout_s
        # Normalize to resolved absolute paths to prevent traversal (constitución IV).
        self._allowed: tuple[str, ...] | None = (
            tuple(str(Path(p).expanduser().resolve()) for p in allowed_prefixes)
            if allowed_prefixes is not None
            else None
        )

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """Captura pasiva de una acción (no ejecuta). Usado en modo teaching."""
        return CapturedAction(
            surface_kind=self.surface_kind,
            intent_desc=intent_desc,
            payload=dict(params),
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Ejecuta una operación UNO sobre LibreOffice headless aislado.

        Fail-closed en todos los ejes:
          - surface_kind mismatch → REJECTED_BY_POLICY (no el adapter correcto).
          - op no permitida → REJECTED_BY_POLICY (binding server-side ya lo acotó).
          - UNO no disponible → EXECUTED_FAILED con mensaje diagnóstico.
          - Cualquier excepción UNO → EXECUTED_FAILED (sin estado a medias).
        """
        if action.surface_kind != SurfaceKind.DESKTOP_APP:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=(
                    f"surface_kind mismatch: esperado DESKTOP_APP, "
                    f"recibido {action.surface_kind!r} — fail-closed"
                ),
            )

        op = action.payload.get("op") or action.payload.get("operation")
        if op not in _ALLOWED_OPS:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=(
                    f"operación UNO no permitida: {op!r}. "
                    f"Permitidas: {sorted(_ALLOWED_OPS)}"
                ),
            )

        if not _check_uno_available():
            return ReplayOutcome.failed(
                action.action_id,
                error=(
                    "python3-uno no disponible en este entorno. "
                    "Instalar: dnf install python3-libreoffice (ya horneado en la imagen). "
                    "Si estás en CI sin LO, este adapter reporta no-disponible "
                    "(degradación honesta, no fallo silencioso)."
                ),
            )

        if _find_soffice_binary() is None:
            return ReplayOutcome.failed(
                action.action_id,
                error=(
                    "soffice/libreoffice no encontrado en PATH. "
                    "Instalar: dnf install libreoffice (ya horneado en la imagen). "
                    "Degradación honesta — no fallo silencioso."
                ),
            )

        return await self._dispatch_uno_operation(action, op)

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        """Serialización canónica determinista para HMAC cross-surface."""
        canonical = {
            "surface_kind": action.surface_kind.value,
            "op": action.payload.get("op") or action.payload.get("operation"),
            "document_path": action.payload.get("document_path", ""),
            "intent_desc": action.intent_desc,
        }
        return json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode("utf-8")

    # ------------------------------------------------------------------
    # Dispatch interno por operación
    # ------------------------------------------------------------------

    async def _dispatch_uno_operation(
        self, action: CapturedAction, op: str
    ) -> ReplayOutcome:
        """Despacha la operación UNO con proceso LO efímero propio.

        Lifecycle por operación:
          1. Crear profile_dir aislado (/tmp/hermes-lo-<action_id>).
          2. Lanzar soffice --headless ligado a pipe único derivado de profile_dir.
          3. Conectar UNO al pipe (poll con timeout).
          4. Ejecutar la operación.
          5. finally: matar el proceso + limpiar profile_dir.

        Fail-closed: cualquier excepción en cualquier paso → EXECUTED_FAILED.
        NUNCA se ejecuta la operación sin proceso propio (INV-2).
        """
        start = time.monotonic()
        profile_dir = _make_profile_dir(self._profile_base, action.action_id)
        lo_process: subprocess.Popen[bytes] | None = None

        try:
            lo_process = _launch_lo_process(profile_dir)
            if op == "open_document":
                result = await self._open_document(
                    action.payload, profile_dir, lo_process
                )
            elif op == "write_text":
                result = await self._write_text(
                    action.payload, profile_dir, lo_process
                )
            elif op == "save_document":
                result = await self._save_document(
                    action.payload, profile_dir, lo_process
                )
            else:
                # Defensive — ya filtrado arriba.
                return ReplayOutcome.rejected_by_policy(
                    action.action_id, reason=f"op no implementada: {op!r}"
                )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - start) * 1000)
            logger.error(
                "hermes.libreoffice_uno.replay_failed op=%s action_id=%s error=%s",
                op,
                action.action_id,
                str(exc),
                exc_info=False,
            )
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"UNO operation failed: {type(exc).__name__}: {exc}",
                duration_ms=elapsed,
            )
        finally:
            _kill_lo_process(lo_process)
            _cleanup_profile_dir(profile_dir)

        elapsed = int((time.monotonic() - start) * 1000)
        logger.info(
            "hermes.libreoffice_uno.replay_ok op=%s action_id=%s duration_ms=%d",
            op,
            action.action_id,
            elapsed,
        )
        return ReplayOutcome.ok(action.action_id, duration_ms=elapsed, result=result)

    def _assert_path_allowed(self, path: str) -> None:
        """Valida path contra allowed_prefixes (constitución IV, I-2).

        Misma lógica que FilesystemSurfaceAdapter._assert_path_allowed.
        Si allowed_prefixes no fue inyectado (None), no hay restricción de allowlist
        — solo para tests; producción DEBE inyectar prefixes.
        """
        if self._allowed is None:
            return
        if not path:
            raise PermissionError("document_path vacío — fail-closed (constitución IV)")
        resolved = str(Path(path).expanduser().resolve())
        for allowed in self._allowed:
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return
        raise PermissionError(
            f"document_path {resolved!r} fuera de allowlist {self._allowed} "
            "(constitución IV fail-closed)"
        )

    async def _open_document(
        self,
        payload: dict[str, Any],
        profile_dir: str,
        lo_process: subprocess.Popen[bytes],
    ) -> dict[str, Any]:
        """Abre un documento en LibreOffice headless vía UNO.

        Validación de path contra allowed_prefixes antes de pasar a UNO (I-2).
        MacroExecutionMode=NEVER_EXECUTE ya está en _make_open_props().
        """
        document_path = _require_str(payload, "document_path")
        self._assert_path_allowed(document_path)
        _assert_path_exists(document_path)

        desktop = _connect_uno_desktop(profile_dir, self._connect_timeout_s, lo_process)
        doc_url = _path_to_url(document_path)
        props = _make_open_props()
        doc = desktop.loadComponentFromURL(doc_url, "_blank", 0, props)
        if doc is None:
            raise RuntimeError(f"LO no pudo abrir: {document_path!r}")

        return {"opened": True, "document_path": document_path}

    async def _write_text(
        self,
        payload: dict[str, Any],
        profile_dir: str,
        lo_process: subprocess.Popen[bytes],
    ) -> dict[str, Any]:
        """Escribe texto en el documento activo (celda/párrafo según tipo doc).

        HIGH en el registry → el broker ya exigió HITL antes de llegar aquí.
        Path validado contra allowed_prefixes (I-2).
        """
        document_path = _require_str(payload, "document_path")
        text_content = _require_str(payload, "text")
        self._assert_path_allowed(document_path)
        _assert_path_exists(document_path)

        desktop = _connect_uno_desktop(profile_dir, self._connect_timeout_s, lo_process)
        doc_url = _path_to_url(document_path)
        props = _make_open_props()
        doc = desktop.loadComponentFromURL(doc_url, "_blank", 0, props)
        if doc is None:
            raise RuntimeError(f"LO no pudo abrir para escritura: {document_path!r}")

        target = payload.get("target", "cursor")
        if target == "cell":
            _write_to_cell(doc, payload, text_content)
        else:
            _write_to_text_cursor(doc, text_content)

        return {"written": True, "document_path": document_path, "length": len(text_content)}

    async def _save_document(
        self,
        payload: dict[str, Any],
        profile_dir: str,
        lo_process: subprocess.Popen[bytes],
    ) -> dict[str, Any]:
        """Guarda el documento. HIGH → HITL ya verificado por broker.

        Path validado contra allowed_prefixes (I-2).
        """
        document_path = _require_str(payload, "document_path")
        self._assert_path_allowed(document_path)
        _assert_path_exists(document_path)

        desktop = _connect_uno_desktop(profile_dir, self._connect_timeout_s, lo_process)
        doc_url = _path_to_url(document_path)
        props = _make_open_props()
        doc = desktop.loadComponentFromURL(doc_url, "_blank", 0, props)
        if doc is None:
            raise RuntimeError(f"LO no pudo abrir para guardar: {document_path!r}")

        doc.store()

        return {"saved": True, "document_path": document_path}


# ------------------------------------------------------------------
# Helpers UNO (módulo-privados — no forman parte del contrato público)
# ------------------------------------------------------------------


def _launch_lo_process(profile_dir: str) -> subprocess.Popen[bytes]:
    """Lanza soffice --headless ligado al pipe derivado de profile_dir.

    INV-2: CADA operación tiene su propio proceso con perfil único en
    /tmp/hermes-lo-<action_id>. El nombre del pipe es hermes-lo-<sha1[:16]>
    derivado deterministamente del profile_dir, que a su vez incorpora el
    action_id (UUID único por dispatch). Esto garantiza que el UnoUrlResolver
    NUNCA pueda resolver accidentalmente una instancia preexistente del humano,
    ya que el pipe no existía antes del spawn.

    El proceso hijo se lanza sin stdin/stdout heredados para que no bloquee.
    Fail-closed: OSError/FileNotFoundError → el caller captura → EXECUTED_FAILED.
    """
    binary = _find_soffice_binary()
    if binary is None:
        raise FileNotFoundError(
            "soffice/libreoffice no encontrado en PATH — no se puede lanzar LO headless"
        )

    pipe_name = _pipe_name_from_profile(profile_dir)
    accept_str = f"pipe,name={pipe_name};urp;StarOffice.ComponentContext"
    profile_url = Path(profile_dir).as_uri()

    cmd = [
        binary,
        "--headless",
        "--norestore",
        "--nologo",
        "--nofirststartwizard",
        "--invisible",
        f"-env:UserInstallation={profile_url}",
        f"--accept={accept_str}",
    ]

    logger.info(
        "hermes.libreoffice_uno.spawn pipe=%s profile_dir=%s",
        pipe_name,
        profile_dir,
    )
    # pylint: disable=consider-using-with  # manual lifecycle — killed in finally
    return subprocess.Popen(  # noqa: S603
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _kill_lo_process(lo_process: subprocess.Popen[bytes] | None) -> None:
    """Termina el proceso LO efímero (idempotente, ignorar si ya terminó)."""
    if lo_process is None:
        return
    try:
        lo_process.terminate()
        try:
            lo_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            lo_process.kill()
            lo_process.wait(timeout=3)
    except OSError:
        pass  # ya terminó
    logger.debug("hermes.libreoffice_uno.process_killed pid=%s", lo_process.pid)


def _connect_uno_desktop(
    profile_dir: str,
    timeout_s: float,
    lo_process: subprocess.Popen[bytes],
) -> Any:
    """Conecta UNO al Desktop del proceso LO headless ya lanzado.

    Hace polling hasta que el pipe esté listo o se agote timeout_s.
    Si el proceso muere antes de que el pipe aparezca, falla inmediatamente
    (fail-closed — no se queda esperando un pipe que nunca llegará).

    INV-2: el pipe hermes-lo-<sha1[:16]> es único por action_id porque el
    profile_dir incorpora el action_id (UUID). El resolver UNO solo puede
    alcanzar este proceso efímero, no una instancia preexistente del humano.
    """
    import uno  # noqa: PLC0415  # lazy: ya se verificó disponibilidad en replay()

    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )

    pipe_name = _pipe_name_from_profile(profile_dir)
    connect_url = f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext"

    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None

    while time.monotonic() < deadline:
        # Fail fast if the LO process died before the pipe was ready.
        if lo_process.poll() is not None:
            raise RuntimeError(
                f"El proceso LO terminó inesperadamente (returncode={lo_process.returncode}) "
                f"antes de que el pipe {pipe_name!r} estuviera listo — fail-closed (INV-2)"
            )
        try:
            remote_ctx = resolver.resolve(connect_url)
            smgr = remote_ctx.ServiceManager
            desktop = smgr.createInstanceWithContext(
                "com.sun.star.frame.Desktop", remote_ctx
            )
            return desktop
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(_PIPE_POLL_INTERVAL_S)

    raise TimeoutError(
        f"No se pudo conectar UNO en {timeout_s}s "
        f"(pipe={pipe_name!r}): {last_exc}"
    )


def _make_open_props() -> tuple[Any, ...]:
    """Props para Desktop.loadComponentFromURL: Hidden=True (sin GUI)."""
    try:
        from com.sun.star.beans import PropertyValue  # noqa: PLC0415
    except ImportError:
        # En CI sin UNO: devolver tupla vacía (el caller ya chequeó disponibilidad).
        return ()

    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True

    macro_disabled = PropertyValue()
    macro_disabled.Name = "MacroExecutionMode"
    macro_disabled.Value = 4  # NEVER_EXECUTE — seguridad

    return (hidden, macro_disabled)


def _path_to_url(path: str) -> str:
    """Convierte path POSIX a URL de LibreOffice (file:///...)."""
    import uno  # noqa: PLC0415
    return uno.systemPathToFileUrl(os.path.abspath(path))


def _write_to_text_cursor(doc: Any, text: str) -> None:
    """Inserta texto al final del documento de texto."""
    body = doc.getText()
    cursor = body.createTextCursor()
    cursor.gotoEnd(False)
    body.insertString(cursor, text, False)


def _write_to_cell(doc: Any, payload: dict[str, Any], text: str) -> None:
    """Escribe en una celda de spreadsheet (address = "A1", "B2", etc.)."""
    address = payload.get("cell_address", "A1")
    sheet_index = payload.get("sheet_index", 0)
    sheets = doc.getSheets()
    sheet = sheets.getByIndex(sheet_index)
    cell = sheet.getCellByPosition(*_parse_cell_address(address))
    cell.setString(text)


def _parse_cell_address(address: str) -> tuple[int, int]:
    """Convierte "A1" → (col=0, row=0). Solo columnas A-Z (26 cols)."""
    address = address.upper().strip()
    col_letters = "".join(c for c in address if c.isalpha())
    row_digits = "".join(c for c in address if c.isdigit())
    if not col_letters or not row_digits:
        raise ValueError(f"Dirección de celda inválida: {address!r}")
    col = sum((ord(c) - ord("A") + 1) * (26 ** i) for i, c in enumerate(reversed(col_letters))) - 1
    row = int(row_digits) - 1
    return (col, row)


def _pipe_name_from_profile(profile_dir: str) -> str:
    """Genera un nombre de pipe UNO único por perfil (determinista, ≤31 chars).

    INV-2: profile_dir = /tmp/hermes-lo-<action_id> donde action_id es UUID
    generado por dispatch (único por operación). El pipe resultante
    hermes-lo-<sha1[:16]> es único por action_id → el UnoUrlResolver no puede
    alcanzar accidentalmente una instancia preexistente del humano.
    """
    digest = hashlib.sha1(profile_dir.encode(), usedforsecurity=False).hexdigest()[:16]  # noqa: S324
    return f"hermes-lo-{digest}"


def _make_profile_dir(base: str, action_id: UUID) -> str:
    """Crea un directorio de perfil aislado para esta acción."""
    profile_dir = f"{base}{action_id}"
    os.makedirs(profile_dir, mode=0o700, exist_ok=True)
    return profile_dir


def _cleanup_profile_dir(profile_dir: str) -> None:
    """Limpia el directorio de perfil temporal (idempotente)."""
    import shutil  # noqa: PLC0415
    try:
        shutil.rmtree(profile_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _require_str(payload: dict[str, Any], key: str) -> str:
    """Extrae un campo string requerido del payload. Fail-closed si falta."""
    val = payload.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"payload.{key!r} es requerido y debe ser un string no vacío")
    return val


def _assert_path_exists(path: str) -> None:
    """Verifica que el path existe antes de pasarlo a UNO (fail-closed)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Documento no encontrado: {path!r}")
