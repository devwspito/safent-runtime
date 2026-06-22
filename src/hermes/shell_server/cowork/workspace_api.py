"""Workspace file API — list, download, and upload agent deliverables.

The agent writes deliverables into /var/lib/hermes/workspace (configured via
HERMES_WORKSPACE_DIR). This router exposes them to the web UI:
  GET  /api/v1/workspace/files        — list all files (read-only, no auth)
  GET  /api/v1/workspace/file/{name}  — download a file (read-only, no auth)
  POST /api/v1/workspace/files        — upload a file into the workspace
                                        (mutating → global Bearer middleware applies)

Security:
  - Only files directly inside the workspace dir are served or written (no
    subdirectory traversal). File names are sanitised via os.path.basename and
    an explicit resolve+prefix check against the workspace root.
  - No symlinks outside the workspace dir are followed (Path.resolve() check).
  - Upload enforces a 25 MB max size and rejects empty files.
  - On filename collision the upload de-duplicates with a numeric suffix rather
    than silently overwriting.
  - path returned in the listing is basename only — never exposes FS layout.
  - Fail-soft: if the workspace dir is missing on GET, returns an empty list.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

logger = logging.getLogger("hermes.shell_server.cowork.workspace_api")

_DEFAULT_WORKSPACE = "/var/lib/hermes/workspace"
_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB hard cap

# Extension → kind mapping (lowercase suffixes, no dot)
_EXT_KIND: dict[str, str] = {
    "xls": "xls",
    "xlsx": "xls",
    "doc": "doc",
    "docx": "doc",
    "pdf": "pdf",
    "js": "js",
    "py": "py",
    "png": "image",
    "jpg": "image",
    "jpeg": "image",
    "gif": "image",
    "svg": "image",
    "webp": "image",
}


def _workspace_dir() -> Path:
    return Path(os.environ.get("HERMES_WORKSPACE_DIR", _DEFAULT_WORKSPACE))


def _infer_kind(name: str) -> str:
    suffix = Path(name).suffix.lstrip(".").lower()
    return _EXT_KIND.get(suffix, "file")


def _safe_child(workspace: Path, name: str) -> Path | None:
    """Resolve `name` to a path strictly inside `workspace`.

    Returns None if the resolved path escapes the workspace or is not a
    regular file. Uses basename to strip any directory component in `name`.
    """
    safe_name = os.path.basename(name)
    if not safe_name:
        return None
    candidate = (workspace / safe_name).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _safe_upload_path(workspace: Path, name: str) -> Path | None:
    """Resolve a sanitised upload destination strictly inside `workspace`.

    Returns the resolved Path (which may or may not exist yet) or None if the
    name is empty, consists only of dots/separators, or escapes the workspace.
    Does NOT check whether the path is an existing file — callers do that.
    """
    safe_name = os.path.basename(name)
    # Reject names that collapse to empty or to bare dot-entries after basename.
    if not safe_name or safe_name in {".", ".."}:
        return None
    candidate = (workspace / safe_name).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        return None
    return candidate


def _deduplicate(dest: Path) -> Path:
    """Return a non-colliding path under the same directory.

    If `dest` does not exist, returns it as-is.

    Naming strategy:
    - Single-extension files (``report.pdf``, ``.env``):
        insert counter before the final suffix → ``report (1).pdf``.
    - Multi-extension files (``archive.tar.gz``) and dotfiles with no
        suffix (``.env`` → suffix is empty after stripping the dot-prefix):
        the counter is inserted before the final suffix component only.
        For dotfiles whose ``name`` IS the suffix (e.g. ``.env``),
        ``Path.suffix`` returns ``""`` and ``Path.stem`` returns ``.env``,
        so we append to the full name → ``.env (1)``.
    """
    if not dest.exists():
        return dest

    name = dest.name
    # Determine the split point: use only the *last* suffix so that
    # "archive.tar.gz" → base="archive.tar", ext=".gz".
    # For dotfiles like ".env": suffix="" and stem=".env", so we treat
    # the whole name as the base and append with no extension.
    suffix = dest.suffix   # last suffix only, e.g. ".gz" or ".pdf" or ""
    base = name[: len(name) - len(suffix)] if suffix else name

    for counter in range(1, 1000):
        candidate = dest.with_name(f"{base} ({counter}){suffix}")
        if not candidate.exists():
            return candidate

    # All 999 slots taken — use a timestamp-derived name as last resort.
    import time as _time  # noqa: PLC0415
    ts = int(_time.time())
    return dest.with_name(f"{base}_{ts}{suffix}")


def create_workspace_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/workspace/files")
    async def list_workspace_files() -> list[dict]:
        """List agent deliverables in the workspace directory.

        Returns [{name, kind, path}] — path is always basename only.
        Fail-soft: empty list if directory is absent or unreadable.
        """
        workspace = _workspace_dir()
        if not workspace.is_dir():
            return []
        try:
            entries = [
                {
                    "name": f.name,
                    "kind": _infer_kind(f.name),
                    "path": f.name,  # basename only — no traversal surface
                }
                for f in sorted(workspace.iterdir())
                if f.is_file()
            ]
        except OSError as exc:
            logger.warning(
                "hermes.cowork.workspace.list_failed",
                extra={"workspace": str(workspace), "error": str(exc)},
            )
            return []
        return entries

    @router.get("/api/v1/workspace/file/{name}")
    async def download_workspace_file(name: str) -> FileResponse:
        """Download a single file from the workspace directory.

        Guards against path traversal: only files directly in the workspace
        directory are served. Returns 404 if the file does not exist or the
        name is invalid.
        """
        workspace = _workspace_dir()
        candidate = _safe_child(workspace, name)
        if candidate is None:
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(path=str(candidate), filename=candidate.name)

    @router.post("/api/v1/workspace/files", status_code=201)
    async def upload_workspace_file(file: UploadFile) -> dict:
        """Upload a file into the workspace directory.

        The file is saved into the same directory that GET /workspace/files
        reads from, so the agent can immediately reference it by name.

        Security controls applied:
          - Filename sanitised to basename only (strips path separators, `..`,
            absolute paths — no path traversal).
          - Final resolved path verified to be strictly inside the workspace root.
          - Empty files rejected (422).
          - Files larger than 25 MB rejected (413) — Content-Length or stream
            size measured during read; we never buffer more than the cap + 1 byte.
          - On name collision, de-duplicates with a numeric suffix rather than
            silently overwriting an existing file.

        Auth: the global Bearer middleware in main.py covers all POST /api/v1/*
        routes; no additional per-route dependency is needed here.

        Returns: { "name": <saved filename>, "path": <absolute path>, "size": <bytes> }
        """
        workspace = _workspace_dir()

        raw_name = file.filename or ""
        dest = _safe_upload_path(workspace, raw_name)
        if dest is None:
            raise HTTPException(
                status_code=422,
                detail="invalid filename: must be a non-empty name without path separators",
            )

        # Stream the upload, enforcing the size cap. We read one byte beyond
        # the cap so that over-size files are detected without buffering them
        # entirely, while also measuring the size of valid uploads.
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(64 * 1024)  # 64 KiB read granularity
            if not chunk:
                break
            total += len(chunk)
            if total > _UPLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"file too large: maximum allowed size is {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB",
                )
            chunks.append(chunk)

        if total == 0:
            raise HTTPException(status_code=422, detail="empty file not allowed")

        workspace.mkdir(parents=True, exist_ok=True)
        dest = _deduplicate(dest)

        dest.write_bytes(b"".join(chunks))

        logger.info(
            # 'name' is a reserved LogRecord attribute — using it in `extra` raises
            # KeyError and 500s the upload. Use a non-reserved key.
            "hermes.cowork.workspace.file_uploaded",
            extra={"upload_name": dest.name, "upload_size": total},
        )
        return {"name": dest.name, "path": str(dest), "size": total}

    return router
