"""Egress permission elevation — the owner grants/revokes the domains the agent
may reach. This is the control-plane behind the friendly "permissions" UI.

Security model: DEFAULT-DENY is the baseline (netns + proxy block everything). The
owner ELEVATES specific domains here; granted domains are persisted and pushed to
the egress proxy's control socket so the agent's browser AND terminal can reach
them — everything else stays denied. "We're Security-First; how conservative the
owner is, is the owner's call."

The shell-server runs as `hermes` (group hermes) → it can connect to the proxy
control socket (root:hermes 0660) to push policy. Grants persist across restarts
in /var/lib/hermes/egress-grants.json and are re-pushed at boot.

C1 PASS-4 (2026-06-19) — MCP egress grants
------------------------------------------
The browser/terminal egress plane (above) and the MCP egress plane are SEPARATE: the MCP
children run in their own netns with a distinct proxy source IP whose policy is PINNED and
immune to the browser's grants. Granting a browser domain must NOT silently open it for
MCPs, and vice-versa. This module therefore exposes a SECOND set of MCP host-grant endpoints
(under /api/v1/egress/mcp on the same router). MCP grants persist in
/var/lib/hermes/mcp-egress-grants.json
(read by the proxy entrypoint at boot to seed the MCP pin) and are pushed at runtime to
the proxy control socket with the RESERVED session marker ``__mcp_grant__`` — the engine
routes that marker to the MCP's PINNED whitelist (extend, mode forced default-deny)
instead of the browser global. Same default-deny posture: the owner elevates the SPECIFIC
host a network-MCP needs; everything else stays denied.

C1 PASS-5 — curated seed is the floor (never wiped)
---------------------------------------------------
This module persists + pushes ONLY the owner-granted MCP hosts (the grants file does NOT
contain the curated BYOK seed — replicate.com, context7.com). The CURATED seed lives in
the proxy entrypoint and is registered with the engine as the pinned floor. The engine's
``grant_to_pinned`` ALWAYS recomputes the pinned whitelist as ``curated_seed ∪
owner_grants``, so pushing the owner-only set here (boot re-apply OR any grant/revoke
cycle) can never wipe the curated hosts. Previously these pushes used SET/overwrite
semantics against the pin and clobbered the seed → curated BYOK got 403. Revoke removes an
owner grant but never a curated host (the seed is immutable in the engine).
"""

from __future__ import annotations

import json
import logging
import re
import socket
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("hermes.shell_server.egress")

_GRANTS_PATH = Path("/var/lib/hermes/egress-grants.json")
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


def _load_from(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text())
        return sorted({str(d) for d in data.get("domains", []) if d})
    except (OSError, json.JSONDecodeError):
        return []


def _save_to(path: Path, domains: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"domains": sorted(set(domains))}))


def _push_session(session_id: str, domains: list[str]) -> bool:
    """Push an allow-list to the egress proxy control socket under ``session_id``.

    For the browser plane ``session_id`` is the audit label (applied as global); for the
    MCP plane it is the reserved marker that the engine routes to the pinned MCP policy.
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(_PROXY_SOCK)
        msg = json.dumps(
            {"session_id": session_id, "mode": "default-deny", "domains": domains}
        ) + "\n"
        s.sendall(msg.encode())
        resp = s.recv(64).decode("utf-8", "replace").strip()
        s.close()
        return resp.startswith("OK")
    except OSError as exc:
        logger.warning("hermes.egress.push_failed session=%s: %s", session_id, exc)
        return False


def _load() -> list[str]:
    return _load_from(_GRANTS_PATH)


def _save(domains: list[str]) -> None:
    _save_to(_GRANTS_PATH, domains)


def _push(domains: list[str]) -> bool:
    """Push the browser-plane allow-list to the proxy (default-deny + domains)."""
    return _push_session(_SESSION, domains)


def apply_persisted_grants() -> bool:
    """Re-push the persisted grants (browser + MCP planes) to the proxy at startup.

    Idempotent. The proxy also seeds the MCP pin from the MCP grants file at boot; this
    re-push covers the case where the shell-server starts/restarts after the proxy and the
    owner added grants while the proxy was already up.

    C1 PASS-5: we push ONLY the owner grants here. The engine unions the curated seed back
    in on every ``grant_to_pinned`` call, so this re-push extends — never replaces — the
    curated floor. Re-pushing an empty owner-grant set leaves the curated BYOK hosts
    reachable (seed ∪ ∅ = seed).
    """
    domains = _load()
    ok = _push(domains)
    mcp_domains = _load_from(_MCP_GRANTS_PATH)
    mcp_ok = _push_session(_MCP_GRANT_SESSION, mcp_domains)
    logger.info(
        "hermes.egress.boot_apply browser_domains=%d pushed=%s mcp_domains=%d mcp_pushed=%s",
        len(domains), ok, len(mcp_domains), mcp_ok,
    )
    return ok and mcp_ok


def _normalize(domain: str) -> str:
    return domain.strip().lower().removeprefix("*.").rstrip(".")


class _DomainBody(BaseModel):
    domain: str


def create_egress_router() -> APIRouter:
    """Router for the owner's egress allow-list (the elevation UI's backend)."""
    router = APIRouter(prefix="/api/v1/egress", tags=["egress"])

    @router.get("/domains")
    async def list_domains() -> dict:
        domains = _load()
        return {"domains": domains, "pushed": _push(domains)}

    @router.post("/domains/grant")
    async def grant(body: _DomainBody) -> dict:
        d = _normalize(body.domain)
        if not _DOMAIN_RE.match(d):
            return {"ok": False, "error": f"dominio inválido: {body.domain!r}"}
        domains = sorted(set(_load()) | {d})
        _save(domains)
        ok = _push(domains)
        logger.info("hermes.egress.granted domain=%s pushed=%s", d, ok)
        return {"ok": True, "domain": d, "domains": domains, "pushed": ok}

    @router.post("/domains/revoke")
    async def revoke(body: _DomainBody) -> dict:
        d = _normalize(body.domain)
        domains = sorted(set(_load()) - {d})
        _save(domains)
        ok = _push(domains)
        logger.info("hermes.egress.revoked domain=%s pushed=%s", d, ok)
        return {"ok": True, "domain": d, "domains": domains, "pushed": ok}

    # ── MCP-plane egress grants (C1 PASS-4) ────────────────────────────────────────
    # SEPARATE plane from the browser/terminal: a host granted here reaches MCP servers
    # only (the MCP netns' pinned proxy policy), and vice-versa. Grants persist in the MCP
    # grants file (seeded into the MCP pin at boot by the proxy entrypoint) and are pushed
    # at runtime under the reserved MCP_GRANT_SESSION marker so the engine extends the
    # PINNED MCP whitelist (mode forced default-deny) — the browser global is untouched.
    # Mounted on the SAME (already-wired) router so the elevation UI reaches them without
    # extra wiring. Default-deny holds: only granted hosts pass.

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
