"""Governed tailnet control API (spec 022).

Endpoints:
  GET    /api/v1/tailnet               — status from status.json (names only, no secrets)
  POST   /api/v1/tailnet/connect       — {auth_key} → vault + staged handoff, 202
  POST   /api/v1/tailnet/disconnect    — {password} → PAM-gated (reuses the remote-access
                                          device-password gate: staged file, root helper verifies)
  GET    /api/v1/tailnet/peers         — peer names + online state
  GET    /api/v1/tailnet/ssh-hosts     — hosts approved for governed SSH (spec 022 v2),
                                          with when each was approved
  DELETE /api/v1/tailnet/ssh-hosts/{host} — revoke a host's SSH approval; requires the
                                          owner's TOTP (require_owner_mfa) — a HIGHER bar
                                          than the egress domain grant/revoke pair, because
                                          this host had REMOTE_EXEC, not just network reach.
                                          Audited (AuditKind.TAILNET_SSH_HOST_REVOKED).

Security model — mirrors ``remote_access_tunnel/api.py`` exactly (same reasoning:
the shell-server unit has ``NoNewPrivileges=yes`` and cannot call ``tailscale``/
``systemctl`` itself, nor verify the device password via PAM):
  - The auth key is validated for shape here, encrypted at rest in the
    ``SecretsVault`` (AES-GCM under master.key — audit/recovery only; the running
    node re-authenticates from its persisted state, not from this blob), and
    staged in PLAINTEXT into a 0600 file the root control helper consumes and
    shreds. The auth key is NEVER logged, NEVER echoed in a response, and NEVER
    passed via argv/env (see specs/022-tailnet-connectivity/contracts.md).
  - Disconnect stages the device password the same way remote-access's
    ``/disable`` does; the ROOT helper does the PAM verify, never this process.
  - Both actions land in the SAME staged file (``request.json``, action field) so
    the ops lane's single ``hermes-tailscale-control.path`` watches one path.

Contract for the staging directory / status file locations: see
specs/022-tailnet-connectivity/contracts.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.shell_server.remote_access_tunnel.rate_limiter import PasswordRateLimiter
from hermes.shell_server.security.mfa import MfaStore
from hermes.shell_server.security.owner_mfa_gate import require_owner_mfa
from hermes.shell_server.security.secrets import SecretsVault
from hermes.tailnet_ssh.infrastructure.json_host_allowlist_store import (
    DEFAULT_ALLOWLIST_PATH,
    JsonHostAllowlistStore,
)

logger = logging.getLogger("hermes.shell_server.tailnet")

_DEFAULT_STATUS_PATH = Path(
    os.environ.get("HERMES_TAILNET_STATUS_PATH", "/run/hermes/tailscale/status.json")
)
_DEFAULT_CONTROL_DIR = Path(
    os.environ.get("HERMES_TAILNET_CONTROL_DIR", "/run/hermes/tailscale-control")
)
_DEFAULT_AUTH_KEY_VAULT_PATH = Path(
    os.environ.get(
        "HERMES_TAILNET_AUTHKEY_VAULT_PATH", "/var/lib/hermes/tailscale-authkey.enc"
    )
)
_REQUEST_FILENAME = "request.json"
_AUTH_KEY_SECRET_ID = "tailnet-auth-key"

_AUTH_KEY_RE = re.compile(r"^tskey-auth-[A-Za-z0-9_-]{8,200}$")

_PASSWORD_MIN = 8
_PASSWORD_MAX = 256
_CTRL_LOWER = 0x20
_CTRL_DEL = 0x7F

_UNCONFIGURED_STATUS: dict = {
    "configured": False,
    "online": False,
    "node_name": None,
    "magicdns_suffix": None,
    "tailnet": None,
    "peers": [],
    "last_attempt": None,
}

# Shared rate-limiter instance — one per server process (mirrors remote-access).
_rate_limiter = PasswordRateLimiter()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class ConnectRequest(BaseModel):
    auth_key: str = Field(min_length=10, max_length=512)


class DisconnectRequest(BaseModel):
    password: str = Field(min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)


class TailnetPeer(BaseModel):
    name: str
    online: bool


class TailnetLastAttempt(BaseModel):
    """Verdict of the most recent connect attempt (025 hallazgo D) — no key
    material, ever. Written by hermes-tailscale-control, mirrored into
    status.json by hermes-tailscale-status-watcher (contracts.md §2)."""

    at: str
    ok: bool
    error_kind: str | None


class TailnetStatusResponse(BaseModel):
    # `configured` means LOGGED IN (== online) — see _read_status. Kept as a
    # separate field for backward-compat with existing readers that only
    # check `configured`; it is never true while `online` is false.
    configured: bool
    online: bool
    node_name: str | None
    magicdns_suffix: str | None
    tailnet: str | None
    peers: list[TailnetPeer]
    last_attempt: TailnetLastAttempt | None = None


class TailnetActionResponse(BaseModel):
    staged: bool


class TailnetPeersResponse(BaseModel):
    peers: list[TailnetPeer]


class SshHostEntry(BaseModel):
    host: str
    approved_at: str | None


class SshHostsResponse(BaseModel):
    hosts: list[SshHostEntry]


class RevokeSshHostRequest(BaseModel):
    totp: str = ""


# ---------------------------------------------------------------------------
# status.json reader (fail-soft: unconfigured/unreadable → "not configured")
# ---------------------------------------------------------------------------


def _read_status(status_path: Path) -> dict:
    """Read status.json into the wire shape. Fail-closed to "not configured"
    on any read/parse error (never invents online/configured on a bad read).

    025 hallazgo D: `configured` used to mean "status.json exists" — but the
    watcher writes this file as soon as tailscaled STARTS, whether or not
    `tailscale up` ever logged in (a rejected key left `online: false`
    forever, yet the file existed, so a caller saw configured:true — a false
    success). `configured` now means the SAME thing `online` does: reported
    "logged in" by the status watcher (contracts.md §2). Kept as a distinct
    field only for callers that historically checked `configured` alone.
    """
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_UNCONFIGURED_STATUS)
    if not isinstance(data, dict):
        return dict(_UNCONFIGURED_STATUS)

    online = bool(data.get("online", False))
    return {
        "configured": online,
        "online": online,
        "node_name": _as_str_or_none(data.get("node_name")),
        "magicdns_suffix": _as_str_or_none(data.get("magicdns_suffix")),
        "tailnet": _as_str_or_none(data.get("tailnet")),
        "peers": _parse_peers(data.get("peers")),
        "last_attempt": _parse_last_attempt(data.get("last_attempt")),
    }


def _as_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _parse_last_attempt(raw: object) -> dict | None:
    """Validate the watcher-mirrored last_attempt shape. Fail-soft: any
    missing/wrong-typed field -> None (never surfaces a half-formed verdict).
    """
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    ok = raw.get("ok")
    if not isinstance(at, str) or not isinstance(ok, bool):
        return None
    error_kind = raw.get("error_kind")
    return {"at": at, "ok": ok, "error_kind": error_kind if isinstance(error_kind, str) else None}


def _parse_peers(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    peers: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        peers.append({"name": name, "online": bool(entry.get("online", False))})
    return peers


# ---------------------------------------------------------------------------
# Auth-key persistence (SecretsVault, audit/recovery — the running node itself
# re-authenticates from its persisted tailscaled state, not from this blob)
# ---------------------------------------------------------------------------


def _persist_auth_key(vault: SecretsVault, auth_key: str, *, vault_path: Path) -> None:
    blob = vault.encrypt(secret_id=_AUTH_KEY_SECRET_ID, plaintext=auth_key)
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = vault_path.with_suffix(".tmp")
    try:
        tmp_path.write_bytes(blob)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        tmp_path.rename(vault_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Control-request staging (0600, atomic write+rename — root helper shreds it)
# ---------------------------------------------------------------------------


def _write_control_request(payload: dict, *, control_dir: Path) -> None:
    """Write the staged control request JSON with mode 0600.

    The parent dir (``/run/hermes/tailscale-control``) is tmpfiles-provisioned —
    we do NOT create it here (mirrors remote_access_tunnel/api.py: if absent in
    production, OS config is wrong and this must fail loudly, not paper over it).
    """
    target = control_dir / _REQUEST_FILENAME
    tmp_path = target.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        tmp_path.rename(target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _validate_password_chars(password: str) -> bool:
    """Reject C0/DEL control characters (mirrors remote_access_tunnel/api.py)."""
    return not any(ord(c) < _CTRL_LOWER or ord(c) == _CTRL_DEL for c in password)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_tailnet_router(
    *,
    vault: SecretsVault | None = None,
    status_path: Path | None = None,
    control_dir: Path | None = None,
    vault_path: Path | None = None,
    rate_limiter: PasswordRateLimiter | None = None,
    ssh_allowlist_path: Path | None = None,
    mfa: MfaStore | None = None,
) -> APIRouter:
    """Create the /api/v1/tailnet router.

    All path/vault overrides exist for tests; production uses the module
    defaults (env-overridable) and the module-level rate-limiter singleton.
    """
    effective_status_path = status_path or _DEFAULT_STATUS_PATH
    effective_control_dir = control_dir or _DEFAULT_CONTROL_DIR
    effective_vault_path = vault_path or _DEFAULT_AUTH_KEY_VAULT_PATH
    effective_vault = vault if vault is not None else SecretsVault()
    effective_limiter = rate_limiter if rate_limiter is not None else _rate_limiter
    effective_ssh_allowlist_path = ssh_allowlist_path or DEFAULT_ALLOWLIST_PATH

    router = APIRouter(prefix="/api/v1/tailnet", tags=["tailnet"])

    @router.get("", response_model=TailnetStatusResponse)
    async def get_status() -> TailnetStatusResponse:
        return TailnetStatusResponse(**_read_status(effective_status_path))

    @router.get("/peers", response_model=TailnetPeersResponse)
    async def get_peers() -> TailnetPeersResponse:
        status = _read_status(effective_status_path)
        return TailnetPeersResponse(peers=status["peers"])

    @router.post("/connect", status_code=202, response_model=TailnetActionResponse)
    async def connect(payload: ConnectRequest) -> TailnetActionResponse:
        """Stage a tailnet connect: validate shape, persist encrypted, hand off.

        Never echoes ``auth_key`` back and never logs it — only the staged-ok
        outcome is logged. The root control helper (out of this process's
        privilege) performs the actual ``tailscale up`` and shreds the handoff.
        """
        if not _AUTH_KEY_RE.match(payload.auth_key):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_auth_key",
                    "message": "La clave debe tener el formato tskey-auth-…",
                },
            )

        try:
            _persist_auth_key(effective_vault, payload.auth_key, vault_path=effective_vault_path)
        except OSError as exc:
            logger.error("hermes.tailnet.connect.vault_write_failed", extra={"error": str(exc)})
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "vault_write_failed",
                    "message": "No se pudo guardar la clave de forma segura. Inténtalo de nuevo.",
                },
            ) from exc

        try:
            _write_control_request(
                {
                    "action": "connect",
                    "requested_at": datetime.now(tz=UTC).isoformat(),
                    "auth_key": payload.auth_key,
                },
                control_dir=effective_control_dir,
            )
        except OSError as exc:
            logger.error("hermes.tailnet.connect.stage_failed", extra={"error": str(exc)})
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "stage_failed",
                    "message": "No se pudo preparar la conexión. Inténtalo de nuevo.",
                },
            ) from exc

        logger.info("hermes.tailnet.connect.staged")
        return TailnetActionResponse(staged=True)

    @router.post("/disconnect", response_model=TailnetActionResponse)
    async def disconnect(
        payload: DisconnectRequest, request: Request
    ) -> TailnetActionResponse:
        """Stage a tailnet disconnect — requires the device password (PAM-gated).

        Same design as remote_access_tunnel's ``/disable``: this process never
        verifies the password. On a wrong password the root helper aborts
        without acting; the UI must poll GET /tailnet to confirm the outcome.
        """
        key = _client_key(request)
        if effective_limiter.is_blocked(key):
            logger.warning("hermes.tailnet.disconnect.rate_limited", extra={"key": key})
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "too_many_attempts",
                    "message": "Demasiados intentos. Espera un momento e inténtalo de nuevo.",
                },
            )

        if not _validate_password_chars(payload.password):
            effective_limiter.record_failure(key)
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_password",
                    "message": "La contraseña contiene caracteres no permitidos.",
                },
            )

        try:
            _write_control_request(
                {
                    "action": "disconnect",
                    "requested_at": datetime.now(tz=UTC).isoformat(),
                    "password": payload.password,
                },
                control_dir=effective_control_dir,
            )
        except OSError as exc:
            logger.error("hermes.tailnet.disconnect.stage_failed", extra={"error": str(exc)})
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "stage_failed",
                    "message": "No se pudo preparar la desconexión. Inténtalo de nuevo.",
                },
            ) from exc

        # Defence-in-depth: count every staged disconnect toward the rate limit,
        # even before the root helper confirms/rejects the password (mirrors
        # remote_access_tunnel/api.py's reasoning exactly).
        effective_limiter.record_failure(key)

        logger.info("hermes.tailnet.disconnect.staged")
        return TailnetActionResponse(staged=True)

    # ── Governed SSH allow-list management (spec 022 v2) ───────────────────
    # `JsonHostAllowlistStore` is the SAME store security_hook.py's Step
    # 1.6-tailnet_ssh gate reads/writes — this is the owner-facing admin
    # screen for the hosts that gate already approved (or a follow-up screen
    # to REVOKE one), never a second grant path (grants only ever happen via
    # the per-host approval card, see ssh-v2.md §Governance).

    @router.get("/ssh-hosts", response_model=SshHostsResponse)
    async def list_ssh_hosts() -> SshHostsResponse:
        store = JsonHostAllowlistStore(effective_ssh_allowlist_path)
        return SshHostsResponse(
            hosts=[
                SshHostEntry(host=entry.host, approved_at=entry.approved_at)
                for entry in store.list_with_metadata()
            ]
        )

    @router.delete("/ssh-hosts/{host}", response_model=SshHostsResponse)
    async def revoke_ssh_host(host: str, payload: RevokeSshHostRequest) -> SshHostsResponse:
        """Revoke a host's governed-SSH approval. Requires the owner's TOTP —
        a HIGHER bar than the egress domain grant/revoke pair (that host had
        REMOTE_EXEC on the owner's tailnet, not just network reach)."""

        store = JsonHostAllowlistStore(effective_ssh_allowlist_path)
        store.revoke(host)
        logger.info("hermes.tailnet.ssh_host_revoked host=%s", host)

        return SshHostsResponse(
            hosts=[
                SshHostEntry(host=entry.host, approved_at=entry.approved_at)
                for entry in store.list_with_metadata()
            ]
        )

    return router
