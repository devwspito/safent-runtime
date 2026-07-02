"""Workspace file API — list, download, and upload agent deliverables.

The agent writes deliverables into /var/lib/hermes/workspace (configured via
HERMES_WORKSPACE_DIR). This router exposes them to the web UI:
  GET  /api/v1/workspace/files             — list directory contents (Finder-style)
  GET  /api/v1/workspace/file/{name}       — download a top-level file (legacy)
  GET  /api/v1/workspace/download          — download any file by ?path= (subfolder-aware)
  POST /api/v1/workspace/files             — upload a file into the workspace
                                             (mutating → global Bearer middleware applies)

Security:
  - All path inputs are resolved with Path.resolve() and verified to fall
    strictly inside the resolved workspace root — no traversal is possible.
  - Symlinks that point outside the workspace root are rejected (resolve()
    follows symlinks, so the final resolved path is always checked).
  - File names are sanitised via os.path.basename on the legacy download
    endpoint; the newer `path` parameter accepts relative sub-paths but the
    same resolve+prefix check applies.
  - Upload enforces a 25 MB max size and rejects empty files.
  - On filename collision the upload de-duplicates with a numeric suffix rather
    than silently overwriting.
  - Fail-soft: if the workspace dir is missing on GET, returns an empty list.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile
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


def _resolve_root(workspace: Path) -> Path:
    """Return the resolved (symlink-expanded) absolute workspace root."""
    return workspace.resolve()


def _safe_entry_path(workspace: Path, rel_path: str) -> Path | None:
    """Resolve a relative path to an entry strictly inside `workspace`.

    Accepts paths with subdirectory components (e.g. "reports/q1.pdf").
    Returns None if:
      - the input is empty or navigates outside the workspace (traversal);
      - the resolved path escapes the workspace root (symlink or ../);
      - the path does not exist.

    Does NOT filter on file-vs-directory — callers decide what is acceptable.
    """
    # Normalise: strip leading slashes so joinpath doesn't treat it as absolute.
    clean = rel_path.lstrip("/")
    if not clean:
        return None
    root = _resolve_root(workspace)
    candidate = (root / clean).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.exists():
        return None
    return candidate


def _safe_rel_upload_path(workspace: Path, rel_path: str) -> Path | None:
    """Resolve a relative path (with subdirs) for a NEW upload, strictly inside
    `workspace`. Unlike _safe_entry_path this does NOT require the path to exist
    (we are about to create it). Rejects traversal / absolute / symlink escapes.
    Used by the folder-bridge upload to preserve directory structure under
    `workspace/bridge/<name>/...`.
    """
    clean = rel_path.strip().lstrip("/")
    if not clean or ".." in clean.split("/"):
        return None
    root = _resolve_root(workspace)
    candidate = (root / clean).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    # Must not resolve to the root itself and must have a filename component.
    if candidate == root or not candidate.name or candidate.name in {".", ".."}:
        return None
    return candidate


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


def _entry_dict(entry: Path, workspace_root: Path) -> dict:
    """Build the response dict for one directory entry."""
    stat = entry.stat()
    rel = entry.relative_to(workspace_root)
    return {
        "name": entry.name,
        "kind": _infer_kind(entry.name) if entry.is_file() else "folder",
        "path": rel.as_posix(),
        "is_dir": entry.is_dir(),
        "size": stat.st_size if entry.is_file() else 0,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    }


def create_workspace_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/workspace/files")
    async def list_workspace_files(
        path: str = Query(default="", description="Relative path inside workspace to list"),
    ) -> list[dict]:
        """List directory contents inside the workspace (Finder-style).

        path="" (default) lists the workspace root.
        path="subdir" lists workspace/subdir.

        Returns [{name, kind, path, is_dir, size, modified}].
        Fail-soft: empty list if directory is absent or unreadable.
        Rejects traversal attempts with HTTP 400.
        """
        workspace = _workspace_dir()
        root = _resolve_root(workspace)

        if path:
            target_dir = _safe_entry_path(workspace, path)
            if target_dir is None or not target_dir.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_path", "message": "path is outside workspace or does not exist"},
                )
        else:
            target_dir = root
            if not target_dir.is_dir():
                return []

        try:
            entries = sorted(target_dir.iterdir(), key=lambda e: (not e.is_dir(), e.name))
            return [_entry_dict(e, root) for e in entries]
        except OSError as exc:
            logger.warning(
                "hermes.cowork.workspace.list_failed",
                extra={"workspace": str(target_dir), "error": str(exc)},
            )
            return []

    @router.get("/api/v1/workspace/download")
    async def download_workspace_file_by_path(
        path: str = Query(..., min_length=1, description="Relative path of the file inside workspace"),
    ) -> FileResponse:
        """Download a file from anywhere inside the workspace by relative path.

        Supports subdirectories: ?path=reports/q1.pdf
        Guards against path traversal: the resolved path must be strictly
        inside the workspace root. Returns 400 on traversal, 404 if not found.
        """
        workspace = _workspace_dir()
        candidate = _safe_entry_path(workspace, path)
        if candidate is None or not candidate.is_file():
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "file not found"},
            )
        return FileResponse(path=str(candidate), filename=candidate.name)

    @router.get("/api/v1/workspace/file/{name}")
    async def download_workspace_file(name: str) -> FileResponse:
        """Download a single top-level file from the workspace directory.

        Legacy endpoint: only serves files directly in the workspace root.
        For files in subdirectories use GET /api/v1/workspace/download?path=.
        Guards against path traversal: returns 404 if the name is invalid.
        """
        workspace = _workspace_dir()
        candidate = _safe_child(workspace, name)
        if candidate is None:
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(path=str(candidate), filename=candidate.name)

    @router.post("/api/v1/workspace/files", status_code=201)
    async def upload_workspace_file(
        file: UploadFile,
        rel_path: str | None = Form(default=None),
    ) -> dict:
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

        # Folder-bridge upload: rel_path preserves the picked folder's structure
        # under workspace/bridge/<name>/... . Otherwise a flat single-file upload.
        if rel_path:
            dest = _safe_rel_upload_path(workspace, rel_path)
            if dest is None:
                raise HTTPException(
                    status_code=422,
                    detail="invalid rel_path: must stay inside the workspace (no '..'/absolute)",
                )
        else:
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
        if rel_path:
            # Preserve structure; overwrite in place (a bridge re-upload should
            # update the same file, not spawn "name (1)" copies).
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
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
