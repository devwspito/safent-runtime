"""GET /api/v1/workspace/files — list and download agent deliverables.

The agent writes deliverables into /var/lib/hermes/workspace (configured via
HERMES_WORKSPACE_DIR). This router exposes them read-only to the web UI.

Security:
  - Only files directly inside the workspace dir are served (no subdirectory
    traversal). The `name` parameter is sanitised via os.path.basename and an
    explicit prefix check against the resolved path.
  - No symlinks outside the workspace dir are followed (Path.resolve() check).
  - path returned in the listing is basename only — never exposes FS layout.
  - Fail-soft: if the workspace dir is missing, returns an empty list (the
    agent may not have written anything yet).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger("hermes.shell_server.cowork.workspace_api")

_DEFAULT_WORKSPACE = "/var/lib/hermes/workspace"

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

    return router
