"""Egress permission elevation — the owner controls the network mode and domain lists.

Security model:
  - ALLOW mode (default): any domain is reachable EXCEPT entries in the owner's
    deny-list and the system blocklist of malicious domains. Implemented as
    ``open-logged`` in the proxy, where both lists act as blockers.
  - DENY mode: default-deny + the owner's explicit allow-list (as before).

Changing the MODE requires owner MFA (TOTP), same bar as changing security policies.
Adding/removing individual domains from the deny-list or allow-list does NOT require
MFA — granular list edits are operational, not posture changes.

The shell-server runs as `hermes` (group hermes) → it can connect to the proxy
control socket (root:hermes 0660) to push policy. Grants and the deny-list persist
across restarts in /var/lib/hermes/ and are re-pushed at boot.

MCP plane (C1 PASS-4/5): separate grants file, reserved session marker, unchanged.
The network-mode toggle affects ONLY the browser/terminal egress plane.
"""

from __future__ import annotations

import json
import logging
import re
import socket
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes.shell_server.security.mfa import MfaStore
from hermes.shell_server.security.owner_mfa_gate import require_owner_mfa

logger = logging.getLogger("hermes.shell_server.egress")

_GRANTS_PATH = Path("/var/lib/hermes/egress-grants.json")
_DENY_PATH = Path("/var/lib/hermes/egress-denylist.json")
_MODE_PATH = Path("/var/lib/hermes/egress-mode.json")
# System malicious-domain blocklist (baked at build, loaded by the proxy at boot). The
# shell-server reads it ONLY to report the count for the "N maliciosos bloqueados" badge.
_BLOCKLIST_PATH = Path("/usr/share/hermes/egress-blocklist.txt")


def _blocklist_count() -> int:
    """Count of non-comment domains in the system malicious blocklist (0 if absent)."""
    try:
        n = 0
        with _BLOCKLIST_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s and not s.startswith("#"):
                    n += 1
        return n
    except Exception:  # noqa: BLE001 — badge is cosmetic; never fail the egress API
        return 0
# C1 PASS-4: MCP-plane grants persist in a SEPARATE file so they never cross into the
# browser plane. The proxy entrypoint reads this exact path at boot to seed the MCP pin.
_MCP_GRANTS_PATH = Path("/var/lib/hermes/mcp-egress-grants.json")
_PROXY_SOCK = "/run/hermes/egress-proxy.sock"
_SESSION = "owner-grants"  # audit label; the control socket applies it as global
# C1 PASS-4: the reserved marker the engine routes to the MCP's PINNED whitelist (extend,
# mode forced default-deny) instead of the browser global. MUST match
# EgressPolicyEngine.MCP_GRANT_SESSION.
_MCP_GRANT_SESSION = "__mcp_grant__"
# Hostname (optionally a leading wildcard, stripped). No scheme, no path, no port.
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")

_ALLOW_MODE = "allow"
_DENY_MODE = "deny"
_DEFAULT_NETWORK_MODE = _ALLOW_MODE


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _load_from(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text())
        return sorted({str(d) for d in data.get("domains", []) if d})
    except (OSError, json.JSONDecodeError):
        return []


def _save_to(path: Path, domains: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"domains": sorted(set(domains))}))


def _load_mode() -> str:
    """Return the persisted network mode (allow|deny). Defaults to allow."""
    try:
        data = json.loads(_MODE_PATH.read_text())
        raw = data.get("mode", _DEFAULT_NETWORK_MODE)
        return raw if raw in (_ALLOW_MODE, _DENY_MODE) else _DEFAULT_NETWORK_MODE
    except (OSError, json.JSONDecodeError):
        return _DEFAULT_NETWORK_MODE


def _save_mode(mode: str) -> None:
    _MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MODE_PATH.write_text(json.dumps({"mode": mode}))


def _load() -> list[str]:
    return _load_from(_GRANTS_PATH)


def _save(domains: list[str]) -> None:
    _save_to(_GRANTS_PATH, domains)


def _load_denylist() -> list[str]:
    return _load_from(_DENY_PATH)


def _save_denylist(domains: list[str]) -> None:
    _save_to(_DENY_PATH, domains)


# ---------------------------------------------------------------------------
# Proxy socket push
# ---------------------------------------------------------------------------


def _push_session(
    session_id: str,
    domains: list[str],
    mode: str = "default-deny",
    deny: list[str] | None = None,
) -> bool:
    """Push a policy to the egress proxy control socket under ``session_id``.

    ``deny`` is the owner's denylist (applied in open-logged / ALLOW mode). It is
    forwarded as the ``deny`` field of the control command (parsed since this release).
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(_PROXY_SOCK)
        payload: dict = {"session_id": session_id, "mode": mode, "domains": domains}
        if deny is not None:
            payload["deny"] = deny
        msg = json.dumps(payload) + "\n"
        s.sendall(msg.encode())
        resp = s.recv(64).decode("utf-8", "replace").strip()
        s.close()
        return resp.startswith("OK")
    except OSError as exc:
        logger.warning("hermes.egress.push_failed session=%s: %s", session_id, exc)
        return False


def _push(domains: list[str]) -> bool:
    """Push the browser-plane allow-list to the proxy (default-deny + domains)."""
    return _push_session(_SESSION, domains)


# ---------------------------------------------------------------------------
# Egress mode application
# ---------------------------------------------------------------------------


def _push_allow_mode(deny_domains: list[str]) -> bool:
    """Push the ALLOW (open-logged) policy with the owner's denylist."""
    return _push_session(_SESSION, [], mode="open-logged", deny=deny_domains)


def _push_deny_mode(allow_domains: list[str]) -> bool:
    """Push the DENY (default-deny) policy with the owner's allowlist."""
    return _push_session(_SESSION, allow_domains, mode="default-deny")


def _apply_network_mode(mode: str | None = None) -> bool:
    """Push the proxy policy matching the persisted network mode.

    This is the single source of truth for what the proxy's browser plane does.
    Called at boot (apply_persisted_grants) and on every mode/list change.
    """
    effective_mode = mode if mode is not None else _load_mode()
    if effective_mode == _ALLOW_MODE:
        ok = _push_allow_mode(_load_denylist())
        logger.info("hermes.egress.mode_applied mode=allow pushed=%s", ok)
        return ok
    allow_domains = _load()
    ok = _push_deny_mode(allow_domains)
    logger.info(
        "hermes.egress.mode_applied mode=deny allow_domains=%d pushed=%s",
        len(allow_domains),
        ok,
    )
    return ok


def apply_browser_egress_for_preset() -> bool:
    """Apply the browser-plane egress policy.

    The network-mode toggle is now sovereign and independent of the preset. This
    function is kept for backward-compat call sites (policies_api.set_preset) but
    no longer reads the preset — it applies the persisted network mode as-is.
    The preset change still has its own effect on tool policies; the egress mode
    is not coupled to it anymore.
    """
    return _apply_network_mode()


def apply_persisted_grants() -> bool:
    """Re-push the persisted state (mode + lists, browser + MCP planes) at startup.

    Idempotent. The proxy also seeds the MCP pin from the MCP grants file at boot;
    this re-push covers restarts where the shell-server comes up after the proxy.
    """
    ok = _apply_network_mode()
    mcp_domains = _load_from(_MCP_GRANTS_PATH)
    mcp_ok = _push_session(_MCP_GRANT_SESSION, mcp_domains)
    logger.info(
        "hermes.egress.boot_apply mode=%s pushed=%s mcp_domains=%d mcp_pushed=%s",
        _load_mode(), ok, len(mcp_domains), mcp_ok,
    )
    return ok and mcp_ok


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _normalize(domain: str) -> str:
    return domain.strip().lower().removeprefix("*.").rstrip(".")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class _DomainBody(BaseModel):
    domain: str


class _ModeBody(BaseModel):
    mode: str   # "allow" | "deny"
    totp: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_egress_router(mfa: MfaStore | None = None) -> APIRouter:
    """Router for the owner's network-mode toggle, deny-list, allow-list, and MCP grants."""
    router = APIRouter(prefix="/api/v1/egress", tags=["egress"])
    mfa_store = mfa or MfaStore()

    # ── Network mode ────────────────────────────────────────────────────────────

    @router.get("/mode")
    async def get_mode() -> dict:
        mode = _load_mode()
        return {
            "mode": mode,
            "description": (
                "allow: any domain reachable except denylist and system blocklist"
                if mode == _ALLOW_MODE
                else "deny: only explicitly allowed domains reachable"
            ),
        }

    @router.post("/mode")
    async def set_mode(body: _ModeBody) -> dict:
        if body.mode not in (_ALLOW_MODE, _DENY_MODE):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_mode", "message": f"mode must be 'allow' or 'deny', got {body.mode!r}"},
            )
        require_owner_mfa(mfa_store, body.totp, action="cambiar el modo de red")
        _save_mode(body.mode)
        ok = _apply_network_mode(body.mode)
        logger.info("hermes.egress.mode_changed mode=%s pushed=%s", body.mode, ok)
        return {"ok": True, "mode": body.mode, "pushed": ok}

    # ── Deny-list (used in ALLOW mode) ──────────────────────────────────────────

    @router.post("/deny/add")
    async def deny_add(body: _DomainBody) -> dict:
        d = _normalize(body.domain)
        if not _DOMAIN_RE.match(d):
            return {"ok": False, "error": f"dominio inválido: {body.domain!r}"}
        domains = sorted(set(_load_denylist()) | {d})
        _save_denylist(domains)
        ok = _apply_network_mode()
        logger.info("hermes.egress.deny_added domain=%s pushed=%s", d, ok)
        return {"ok": True, "domain": d, "denylist": domains, "pushed": ok}

    @router.post("/deny/remove")
    async def deny_remove(body: _DomainBody) -> dict:
        d = _normalize(body.domain)
        domains = sorted(set(_load_denylist()) - {d})
        _save_denylist(domains)
        ok = _apply_network_mode()
        logger.info("hermes.egress.deny_removed domain=%s pushed=%s", d, ok)
        return {"ok": True, "domain": d, "denylist": domains, "pushed": ok}

    # ── Allow-list (used in DENY mode) — kept unchanged ─────────────────────────

    @router.get("/domains")
    async def list_domains() -> dict:
        mode = _load_mode()
        return {
            "mode": mode,
            "domains": _load(),
            "denylist": _load_denylist(),
            "blocklist_count": _blocklist_count(),
        }

    @router.post("/domains/grant")
    async def grant(body: _DomainBody) -> dict:
        d = _normalize(body.domain)
        if not _DOMAIN_RE.match(d):
            return {"ok": False, "error": f"dominio inválido: {body.domain!r}"}
        domains = sorted(set(_load()) | {d})
        _save(domains)
        ok = _apply_network_mode()
        logger.info("hermes.egress.granted domain=%s pushed=%s", d, ok)
        return {"ok": True, "domain": d, "domains": domains, "pushed": ok}

    @router.post("/domains/revoke")
    async def revoke(body: _DomainBody) -> dict:
        d = _normalize(body.domain)
        domains = sorted(set(_load()) - {d})
        _save(domains)
        ok = _apply_network_mode()
        logger.info("hermes.egress.revoked domain=%s pushed=%s", d, ok)
        return {"ok": True, "domain": d, "domains": domains, "pushed": ok}

    # ── MCP-plane egress grants (C1 PASS-4) — unchanged ─────────────────────────

    @router.get("/mcp/domains")
    async def list_mcp_domains() -> dict:
        domains = _load_from(_MCP_GRANTS_PATH)
        return {"domains": domains, "pushed": _push_session(_MCP_GRANT_SESSION, domains)}

    @router.post("/mcp/domains/grant")
    async def grant_mcp(body: _DomainBody) -> dict:
        d = _normalize(body.domain)
        if not _DOMAIN_RE.match(d):
            return {"ok": False, "error": f"dominio inválido: {body.domain!r}"}
        domains = sorted(set(_load_from(_MCP_GRANTS_PATH)) | {d})
        _save_to(_MCP_GRANTS_PATH, domains)
        ok = _push_session(_MCP_GRANT_SESSION, domains)
        logger.info("hermes.egress.mcp_granted domain=%s pushed=%s", d, ok)
        return {"ok": True, "domain": d, "domains": domains, "pushed": ok}

    @router.post("/mcp/domains/revoke")
    async def revoke_mcp(body: _DomainBody) -> dict:
        d = _normalize(body.domain)
        domains = sorted(set(_load_from(_MCP_GRANTS_PATH)) - {d})
        _save_to(_MCP_GRANTS_PATH, domains)
        ok = _push_session(_MCP_GRANT_SESSION, domains)
        logger.info("hermes.egress.mcp_revoked domain=%s pushed=%s", d, ok)
        return {"ok": True, "domain": d, "domains": domains, "pushed": ok}

    return router
