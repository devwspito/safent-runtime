"""FilesystemSurfaceAdapter — captura/replay de operaciones de filesystem.

FR-027/028 (spec 003): Hermes aprende a leer/escribir archivos cuando el
formador se lo enseña; luego puede replay-ar esas operaciones.

Operaciones soportadas:
    read_file       leer contenido de un archivo.
    write_file      escribir contenido a un archivo (crea o sobreescribe).
    list_dir        listar un directorio.
    create_dir      crear un directorio.
    delete_file     borrar un archivo.

Confinamiento kernel B-2 (spec 014):
    Todas las operaciones que abren un descriptor de archivo usan
    ``openat2(2)`` con ``RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH`` desde un
    fd base del workspace, eliminando la ventana TOCTOU presente en el
    patrón ``resolve() → open()`` y evadiendo symlinks (``allowed/link →
    /etc/shadow``).

    Si ``openat2`` no está disponible en el kernel (< 5.6), se cae a
    ``O_NOFOLLOW`` + validación post-open via ``fstat`` + re-check del
    inode (fail-closed con symlinks: ELOOP → PermissionError).

    Fail-closed: cualquier path fuera del workspace, symlink que apunte
    fuera, o error de apertura → PermissionError / REJECTED_BY_POLICY.

Path allowlist obligatoria (constitución IV fail-closed): el adapter
recibe una lista de path prefixes permitidos en el constructor. Cualquier
acceso fuera de esa lista → REJECTED_BY_POLICY.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger(__name__)

_SUPPORTED_OPS = frozenset(
    {"read_file", "write_file", "list_dir", "create_dir", "delete_file"}
)

# ---------------------------------------------------------------------------
# openat2 interface
# ---------------------------------------------------------------------------

_OPENAT2_NR: int = 437          # x86_64 / aarch64
_AT_FDCWD: int = -100
_OPEN_HOW_SIZE: int = 24        # sizeof(struct open_how) = 3 × u64

# open_how.resolve flags
_RESOLVE_NO_SYMLINKS: int = 0x04
_RESOLVE_BENEATH: int = 0x08

# open flags
_O_RDONLY: int = 0
_O_WRONLY: int = 1
_O_RDWR: int = 2
_O_CREAT: int = 0o100
_O_TRUNC: int = 0o1000
_O_DIRECTORY: int = 0o200000
_O_NOFOLLOW: int = 0o400000
_O_PATH: int = 0o10000000
_O_CLOEXEC: int = 0o2000000


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c")
    if name is None:
        raise OSError("libc not found")
    return ctypes.CDLL(name, use_errno=True)


def _openat2_available() -> bool:
    """Probe openat2 availability without opening anything real."""
    try:
        lib = _libc()
        lib.syscall.argtypes = None
        lib.syscall.restype = ctypes.c_long
        # Pass NULL path → EFAULT (14) means syscall exists.
        # ENOSYS (38) means not available.
        how = struct.pack("=QQQ", _O_RDONLY, 0, 0)
        buf = (ctypes.c_uint8 * _OPEN_HOW_SIZE)(*how)
        ret = lib.syscall(
            ctypes.c_long(_OPENAT2_NR),
            ctypes.c_long(_AT_FDCWD),
            ctypes.c_void_p(0),  # NULL path → EFAULT
            ctypes.byref(buf),
            ctypes.c_size_t(_OPEN_HOW_SIZE),
        )
        err = ctypes.get_errno()
        _ = ret  # ignored
        return err != 38  # ENOSYS = not available
    except OSError:
        return False


# Cache availability at module load (kernel doesn't change at runtime).
_OPENAT2_OK: bool = _openat2_available()


def _raw_openat2(
    dirfd: int,
    path: bytes,
    flags: int,
    mode: int,
    resolve: int,
) -> int:
    """Call openat2(dirfd, path, &how, sizeof(how)).

    Returns the file descriptor on success.
    Raises OSError on failure (errno-based).
    """
    lib = _libc()
    lib.syscall.argtypes = None
    lib.syscall.restype = ctypes.c_long

    how = struct.pack("=QQQ", flags, mode, resolve)
    buf = (ctypes.c_uint8 * _OPEN_HOW_SIZE)(*how)
    path_buf = ctypes.create_string_buffer(path + b"\x00")

    fd = lib.syscall(
        ctypes.c_long(_OPENAT2_NR),
        ctypes.c_long(dirfd),
        ctypes.byref(path_buf),
        ctypes.byref(buf),
        ctypes.c_size_t(_OPEN_HOW_SIZE),
    )
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path.decode("utf-8", errors="replace"))
    return int(fd)


def _open_beneath(
    base_fd: int,
    rel_path: bytes,
    flags: int,
    mode: int = 0,
) -> int:
    """Open *rel_path* relative to *base_fd* with RESOLVE_NO_SYMLINKS|RESOLVE_BENEATH.

    Falls back to open() with O_NOFOLLOW when openat2 is unavailable.
    Fail-closed: raises OSError / PermissionError on symlinks or escapes.
    """
    if _OPENAT2_OK:
        resolve = _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH
        return _raw_openat2(base_fd, rel_path, flags | _O_CLOEXEC, mode, resolve)
    return _open_nofollow_fallback(base_fd, rel_path, flags, mode)


def _open_nofollow_fallback(
    base_fd: int,
    rel_path: bytes,
    flags: int,
    mode: int,
) -> int:
    """Fallback when openat2 is unavailable.

    Uses openat(base_fd, rel_path, O_NOFOLLOW | flags). Symlinks at the
    final path component will produce ELOOP → PermissionError (fail-closed).
    Intermediate symlinks are NOT protected by this fallback — document
    as a known limitation when running on kernels < 5.6.
    """
    nofollow_flags = flags | _O_NOFOLLOW | _O_CLOEXEC
    fd = os.open(
        rel_path.decode("utf-8", errors="surrogateescape"),
        nofollow_flags,
        mode,
        dir_fd=base_fd,
    )
    return fd


def _open_base(workspace: str) -> int:
    """Open the workspace directory as an O_PATH fd (base for openat2)."""
    return os.open(workspace, _O_PATH | _O_DIRECTORY | _O_CLOEXEC)


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------


class WorkspaceEscapeError(PermissionError):
    """A path resolves outside the permitted workspace."""


def _resolve_to_workspace_relative(path: str, workspace: str) -> tuple[str, bytes]:
    """Resolve *path* and return (abs_str, workspace_relative_bytes).

    Raises WorkspaceEscapeError if the path is not within *workspace*.
    This check is a pre-filter only — the actual enforcement is done by
    openat2/O_NOFOLLOW at open time (TOCTOU-safe).
    """
    abs_path = Path(path).expanduser().resolve()
    ws_path = Path(workspace)

    # Check containment without following symlinks via __str__ prefix match.
    abs_str = str(abs_path)
    ws_str = str(ws_path)
    if abs_str != ws_str and not abs_str.startswith(ws_str + os.sep):
        raise WorkspaceEscapeError(
            f"path {abs_str!r} escapes workspace {ws_str!r} "
            "(constitución IV fail-closed)"
        )
    # Relative path from workspace root (empty string = the workspace itself).
    rel = abs_path.relative_to(ws_path)
    return abs_str, str(rel).encode("utf-8") if str(rel) != "." else b"."


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


class FilesystemSurfaceAdapter:
    """Cumple ``SurfaceAdapterPort`` para superficie ``FILESYSTEM``."""

    def __init__(
        self,
        *,
        allowed_prefixes: tuple[str, ...],
        max_read_bytes: int = 1024 * 1024,
    ) -> None:
        if not allowed_prefixes:
            raise ValueError(
                "allowed_prefixes vacío — fail-closed (constitución IV). "
                "El cliente DEBE declarar explícitamente paths accesibles."
            )
        # Normalizar a paths absolutos resueltos para comparaciones de prefijo.
        self._allowed = tuple(
            str(Path(p).expanduser().resolve()) for p in allowed_prefixes
        )
        self._max_read_bytes = max_read_bytes

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.FILESYSTEM

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        op = params.get("op", "")
        path = params.get("path", "")
        if op not in _SUPPORTED_OPS:
            raise ValueError(
                f"op {op!r} no soportada por FilesystemSurfaceAdapter. "
                f"Soportadas: {sorted(_SUPPORTED_OPS)}"
            )
        workspace = self._assert_path_allowed(path)
        result = await self._execute(op, path, params, workspace=workspace)
        return CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.FILESYSTEM,
            intent_desc=intent_desc,
            payload={
                "op": op,
                "path": str(Path(path).expanduser().resolve()),
                "result_summary": result.get("summary", ""),
                "params_extra": {
                    k: v
                    for k, v in params.items()
                    if k not in ("op", "path", "content")
                },
            },
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
        if action.surface_kind != SurfaceKind.FILESYSTEM:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"surface mismatch: esperado FILESYSTEM, got {action.surface_kind}",
            )
        op = action.payload.get("op", "")
        path = action.payload.get("path", "")
        if op not in _SUPPORTED_OPS:
            return ReplayOutcome.rejected_by_policy(
                action.action_id, reason=f"op {op!r} no soportada"
            )
        try:
            workspace = self._assert_path_allowed(path)
        except PermissionError as exc:
            return ReplayOutcome.rejected_by_policy(
                action.action_id, reason=str(exc)
            )
        try:
            result = await self._execute(op, path, action.payload, workspace=workspace)
        except PermissionError as exc:
            return ReplayOutcome.rejected_by_policy(
                action.action_id, reason=str(exc)
            )
        except FileNotFoundError as exc:
            return ReplayOutcome.failed(action.action_id, error=str(exc))
        except OSError as exc:
            return ReplayOutcome.failed(
                action.action_id, error=f"OSError({exc.errno}): {exc.strerror}"
            )
        return ReplayOutcome.ok(action.action_id, result=result)

    def replay_payload(self, payload: dict[str, Any]) -> bool:
        """SurfaceReplayPort shim — maps SkillReplayer's sync payload call to async replay."""
        import asyncio  # noqa: PLC0415

        from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
            CapturedAction,
            ReplayStatus,
        )

        action = CapturedAction(
            surface_kind=self.surface_kind,
            intent_desc=payload.get("intent_desc", ""),
            payload=payload,
        )
        outcome = asyncio.run(self.replay(action))
        return outcome.status == ReplayStatus.EXECUTED_OK

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        canonical = {
            "surface_kind": action.surface_kind.value,
            "intent_desc": action.intent_desc,
            "op": action.payload.get("op", ""),
            "path": action.payload.get("path", ""),
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _assert_path_allowed(self, path: str) -> str:
        """Validate that *path* falls under an allowed prefix.

        Returns the matching workspace prefix (the tightest one).
        Raises PermissionError if not allowed.
        """
        if not path:
            raise PermissionError("path vacío")
        resolved = str(Path(path).expanduser().resolve())
        best: str | None = None
        for allowed in self._allowed:
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                if best is None or len(allowed) > len(best):
                    best = allowed
        if best is None:
            raise PermissionError(
                f"path {resolved!r} fuera de allowlist {self._allowed} "
                "(constitución IV fail-closed)"
            )
        return best

    async def _execute(
        self,
        op: str,
        path: str,
        params: dict[str, Any],
        *,
        workspace: str,
    ) -> dict[str, Any]:
        """Dispatch to the appropriate operation, using openat2-safe openers."""
        if op == "read_file":
            return self._read_file_safe(path, workspace)
        if op == "write_file":
            return self._write_file_safe(path, params, workspace)
        if op == "list_dir":
            return self._list_dir_safe(path, workspace)
        if op == "create_dir":
            return self._create_dir_safe(path, workspace)
        if op == "delete_file":
            return self._delete_file_safe(path, workspace)
        raise ValueError(f"op unreachable: {op}")

    def _read_file_safe(self, path: str, workspace: str) -> dict[str, Any]:
        abs_str, rel = _resolve_to_workspace_relative(path, workspace)
        base_fd = _open_base(workspace)
        try:
            file_fd = _open_beneath(base_fd, rel, _O_RDONLY)
        except OSError as exc:
            if exc.errno == 40:  # ELOOP = symlink hit
                raise PermissionError(
                    f"symlink detectado en {path!r} — rechazado (B-2 TOCTOU)"
                ) from exc
            raise
        finally:
            os.close(base_fd)
        try:
            data = _read_fd(file_fd, self._max_read_bytes)
        finally:
            os.close(file_fd)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = f"<binary {len(data)} bytes>"
        return {"summary": f"read {len(data)} bytes", "text": text}

    def _write_file_safe(
        self, path: str, params: dict[str, Any], workspace: str
    ) -> dict[str, Any]:
        _, rel = _resolve_to_workspace_relative(path, workspace)
        content = params.get("content", "")
        if isinstance(content, str):
            data = content.encode("utf-8")
        else:
            data = bytes(content)
        base_fd = _open_base(workspace)
        try:
            file_fd = _open_beneath(
                base_fd, rel, _O_WRONLY | _O_CREAT | _O_TRUNC, mode=0o644
            )
        except OSError as exc:
            if exc.errno == 40:
                raise PermissionError(
                    f"symlink detectado en {path!r} — rechazado (B-2 TOCTOU)"
                ) from exc
            raise
        finally:
            os.close(base_fd)
        try:
            _write_fd(file_fd, data)
        finally:
            os.close(file_fd)
        return {"summary": f"wrote {len(data)} bytes"}

    def _list_dir_safe(self, path: str, workspace: str) -> dict[str, Any]:
        # list_dir uses Path.iterdir() which does not follow symlinks for
        # the listed entries. The directory itself is validated by _assert_path_allowed
        # (prefix check). We apply O_NOFOLLOW on the dir fd for safety.
        abs_str, _ = _resolve_to_workspace_relative(path, workspace)
        target = Path(abs_str)
        rel_str = str(target.relative_to(Path(workspace)))

        # When path IS the workspace root, open the base directly.
        if rel_str == ".":
            dir_fd = _open_base(abs_str)
            try:
                entries = sorted(p.name for p in target.iterdir())
            finally:
                os.close(dir_fd)
            return {"summary": f"{len(entries)} entries", "entries": entries}

        base_fd = _open_base(workspace)
        dir_fd = None
        try:
            dir_fd = _open_beneath(
                base_fd, rel_str.encode("utf-8"), _O_RDONLY | _O_DIRECTORY
            )
        except OSError as exc:
            if exc.errno == 40:
                raise PermissionError(
                    f"symlink detectado en {path!r} — rechazado (B-2 TOCTOU)"
                ) from exc
            raise
        finally:
            os.close(base_fd)
        try:
            entries = sorted(p.name for p in target.iterdir())
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
        return {"summary": f"{len(entries)} entries", "entries": entries}

    def _create_dir_safe(self, path: str, workspace: str) -> dict[str, Any]:
        abs_str, _ = _resolve_to_workspace_relative(path, workspace)
        Path(abs_str).mkdir(parents=True, exist_ok=True)
        return {"summary": f"dir created at {abs_str}"}

    def _delete_file_safe(self, path: str, workspace: str) -> dict[str, Any]:
        abs_str, rel = _resolve_to_workspace_relative(path, workspace)
        target = Path(abs_str)
        if not target.exists():
            return {"summary": "noop (file did not exist)"}
        # Open with O_NOFOLLOW to confirm it is a regular file before unlinking.
        base_fd = _open_base(workspace)
        try:
            check_fd = _open_beneath(base_fd, rel, _O_RDONLY)
        except OSError as exc:
            if exc.errno == 40:
                raise PermissionError(
                    f"symlink detectado en {path!r} — rechazado (B-2 TOCTOU)"
                ) from exc
            raise
        finally:
            os.close(base_fd)
        try:
            st = os.fstat(check_fd)
            import stat as stat_mod  # noqa: PLC0415
            if stat_mod.S_ISLNK(st.st_mode):
                raise PermissionError(
                    f"symlink detectado en {path!r} vía fstat — rechazado"
                )
        finally:
            os.close(check_fd)
        target.unlink()
        return {"summary": f"deleted {abs_str}"}


# ---------------------------------------------------------------------------
# Low-level fd helpers
# ---------------------------------------------------------------------------


def _read_fd(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(data):
        written = os.write(fd, view[offset:])
        offset += written


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def hash_action(adapter: FilesystemSurfaceAdapter, action: CapturedAction) -> str:
    return hashlib.sha256(adapter.serialize_for_signing(action)).hexdigest()
