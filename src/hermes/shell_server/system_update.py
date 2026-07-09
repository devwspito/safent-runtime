"""system_update — let the user update Safent FROM THE UI (no terminal).

The container is sandboxed and cannot recreate itself (touching the host podman would
defeat the cage). So the flow is: the UI calls POST /api/v1/system/update, which drops
an "update requested" marker into the daemon-owned instance dir; a host-side agent
(`safent agent`, installed once by get-safent.sh as a launchd/systemd unit) watches for
that marker and runs the same `safent update` (prune-before-pull + recreate). GET reports
the current version and, best-effort, the latest published version so the UI can show an
"update available" hint.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.request

from fastapi import APIRouter, HTTPException, Request

import hermes

logger = logging.getLogger("hermes.shell_server.system_update")

# Daemon-owned dir (uid 880 can write here; the parent /var/lib/hermes is root:root
# 0755). The host `safent agent` watches this exact path.
_INSTANCE_DIR = "/var/lib/hermes/instance"
_UPDATE_FLAG = os.path.join(_INSTANCE_DIR, ".update-requested")
# Uninstall marker — same mechanism as update: the sandboxed UI can't touch the host,
# so it drops this marker and the host `safent agent` runs `safent uninstall` (removes
# the container + data volume + the CLI + the agent). podman/docker are left installed.
_UNINSTALL_FLAG = os.path.join(_INSTANCE_DIR, ".uninstall-requested")

# A CLI update takes ~2-5 min. If the flag is older than this, no host watcher
# picked it up (agent not installed / dead) — treat it as stale and clear it so
# the UI stops showing an eternal "Updating…" and offers the button again.
_FLAG_STALE_S = 15 * 60


def _updating() -> bool:
    try:
        st = os.stat(_UPDATE_FLAG)
    except OSError:
        return False
    if time.time() - st.st_mtime > _FLAG_STALE_S:
        try:
            os.remove(_UPDATE_FLAG)
        except OSError:
            pass
        logger.warning("hermes.system_update.flag_stale_cleared (no host agent picked it up)")
        return False
    return True

# Source of truth for "what's the latest version" — the repo VERSION file on main.
_LATEST_URL = os.environ.get(
    "SAFENT_VERSION_URL",
    "https://raw.githubusercontent.com/devwspito/safent-runtime/main/VERSION",
)


def _parse(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(v).strip().lstrip("v").split("."):
        num = "".join(c for c in chunk if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def _fetch_latest() -> str | None:
    try:
        with urllib.request.urlopen(_LATEST_URL, timeout=5) as r:
            return r.read().decode("utf-8").strip() or None
    except Exception:  # noqa: BLE001 — network is best-effort; UI still works without it
        return None


def create_system_update_router() -> APIRouter:
    from hermes.shell_server.cowork.training_live import _verify_token  # noqa: PLC0415

    router = APIRouter()

    def _auth(request: Request) -> None:
        expected = getattr(request.app.state, "shell_webui_token", "")
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth[:7].lower() == "bearer " else ""
        if not _verify_token(tok, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @router.get("/api/v1/system/update")
    async def system_update_status(request: Request) -> dict:
        _auth(request)
        current = str(getattr(hermes, "__version__", "0"))
        latest = _fetch_latest()
        available = bool(latest) and _parse(latest) > _parse(current)
        return {
            "current_version": current,
            "latest_version": latest,
            "update_available": available,
            "updating": _updating(),
        }

    @router.post("/api/v1/system/update")
    async def system_update_request(request: Request) -> dict:
        _auth(request)
        try:
            os.makedirs(_INSTANCE_DIR, exist_ok=True)
            with open(_UPDATE_FLAG, "w", encoding="utf-8") as fh:
                fh.write("requested\n")
        except OSError as exc:
            logger.warning("hermes.system_update.flag_write_failed: %s", exc)
            raise HTTPException(status_code=500, detail="could not request update")
        return {"ok": True, "updating": True}

    @router.post("/api/v1/system/uninstall")
    async def system_uninstall_request(request: Request) -> dict:
        _auth(request)
        try:
            os.makedirs(_INSTANCE_DIR, exist_ok=True)
            with open(_UNINSTALL_FLAG, "w", encoding="utf-8") as fh:
                fh.write("requested\n")
        except OSError as exc:
            logger.warning("hermes.system_update.uninstall_flag_write_failed: %s", exc)
            raise HTTPException(status_code=500, detail="could not request uninstall")
        return {"ok": True}

    return router
