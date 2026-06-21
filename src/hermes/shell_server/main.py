"""hermes-shell-server — backend local de la Hermes Shell.

Escucha SOLO en 127.0.0.1:7517. Expone:

  /healthz
  /api/v1/profile                       perfil del SO (personal-desktop, etc)
  /api/v1/runtime/status                estado del agente (mock por ahora)

  /api/v1/chat                          POST mensaje → encola vía ControlPlanePort
                                         Devuelve {task_id, stream_path}
  /ws/tasks/{task_id}                   WebSocket stream de tarea (daemon-owned)

  /api/v1/chat/conversations            Mirror read-only del historial
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.shell_server.providers.domain import (
    ProviderKind,
    new_provider,
)
from hermes.shell_server.providers.repo import SQLiteProviderRepository
from hermes.shell_server.security.secrets import SecretsVault
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes-shell-server")

_DB_PATH = Path(
    os.environ.get(
        "HERMES_SHELL_DB",
        "/var/lib/hermes/shell-state.db",
    )
)


# ============================================================
# Pydantic schemas
# ============================================================


class ChatRequest(BaseModel):
    conversation_id: str | None = None  # propagado al daemon como contexto
    user_message: str = Field(min_length=1)
    dedup_key: str | None = None  # CTRL-P1-27: idempotencia doble-envío


class ChatStartResponse(BaseModel):
    """Resultado de encolar un mensaje de chat (T048 / FR-010).

    task_id: UUID de la tarea en la cola del agente.
    stream_path: ruta del socket WS de chunks (/ws/tasks/{task_id}).
    """

    task_id: str
    stream_path: str


# ============================================================
# App factory
# ============================================================


def _seed_providers_if_empty(repo: SQLiteProviderRepository) -> None:
    """Si no hay providers + env vars seed estan seteadas, crea un seed.

    Se usa para que la VM de demo arranque con el vLLM de la DGX ya
    configurado y activo, sin que el usuario tenga que meterlo.
    """
    if repo.list_all():
        return
    seed_url = os.environ.get("HERMES_SEED_VLLM_URL")
    seed_model = os.environ.get("HERMES_SEED_VLLM_MODEL")
    seed_key = os.environ.get("HERMES_SEED_VLLM_KEY")
    seed_alias = os.environ.get("HERMES_SEED_VLLM_ALIAS", "vLLM (seed)")
    if not seed_url or not seed_model:
        return
    provider = new_provider(
        alias=seed_alias,
        kind=ProviderKind.VLLM,
        default_model=seed_model,
        base_url=seed_url,
        has_api_key=bool(seed_key),
    )
    repo.add(provider=provider, api_key=seed_key)
    repo.set_active(provider_id=provider.provider_id)
    logger.info(
        "Seeded provider %s model=%s base=%s",
        seed_alias,
        seed_model,
        seed_url,
    )


def _build_audit_tail_writer():
    """Construct AuditTailWriter with HttpsAuditTailTransport if configured.

    If HERMES_CP_AUDIT_URL is absent the writer runs with FakeAuditTailTransport
    so that the spool + background-thread machinery still operates locally.
    """
    from hermes.agents_os.infrastructure.audit_tail_writer import (  # noqa: PLC0415
        AuditTailWriter,
        FakeAuditTailTransport,
    )

    spool_dir = Path(
        os.environ.get(
            "HERMES_AUDIT_SPOOL_DIR", "/var/lib/hermes/audit-tail-pending"
        )
    )
    audit_url = os.environ.get("HERMES_CP_AUDIT_URL")
    if audit_url:
        from hermes.agents_os.infrastructure.audit_tail_writer import (  # noqa: PLC0415
            HttpsAuditTailTransport,
        )

        transport = HttpsAuditTailTransport(
            url=audit_url,
            client_cert=os.environ.get("HERMES_CP_CLIENT_CERT"),
            client_key=os.environ.get("HERMES_CP_CLIENT_KEY"),
        )
    else:
        transport = FakeAuditTailTransport()
        logger.info("HERMES_CP_AUDIT_URL not set — audit tail using in-process spool only")
    return AuditTailWriter(transport=transport, spool_dir=spool_dir)


def _build_prometheus_exporter():
    """Build a PrometheusExporterAdapter with a minimal TelemetryOptInService.

    In production the TelemetryOptInService state is loaded from DB; here we
    use a minimal in-memory instance (disabled by default per FR-061).

    Signing key: derived from master.key via HKDF so it is deterministic across
    restarts (the telemetry audit chain is in-memory, but re-derives the same key
    from the same master). Falls back to a fresh random key in dev/CI where
    master.key is absent — logged as a warning so the gap is visible.
    """
    import secrets as _secrets  # noqa: PLC0415

    from hermes.agents_os.application.audit_hash_chain import (  # noqa: PLC0415
        AuditHashChainSigner,
    )
    from hermes.agents_os.application.telemetry_opt_in import (  # noqa: PLC0415
        TelemetryOptInService,
    )
    from hermes.agents_os.infrastructure.prometheus_exporter import (  # noqa: PLC0415
        PrometheusExporterAdapter,
    )

    try:
        signing_key = SecretsVault().derive_subkey(label="telemetry-audit-chain")
    except (RuntimeError, ValueError):
        # master.key absent (dev/CI without the baked image). Use a random key;
        # the telemetry chain is non-persistent anyway so cross-restart linkage
        # is not possible in this environment. This is logged as a warning —
        # production images always have master.key from the keygen oneshot unit.
        signing_key = _secrets.token_bytes(32)
        logger.warning(
            "hermes.telemetry_signer.ephemeral_key",
            extra={"reason": "master.key unavailable — dev/CI environment"},
        )

    signer = AuditHashChainSigner(signing_key=signing_key)
    telemetry = TelemetryOptInService(audit_signer=signer)
    return PrometheusExporterAdapter(telemetry=telemetry)


# ---------------------------------------------------------------------------
# T048 — ControlPlane D-Bus client factory
# ---------------------------------------------------------------------------


def _build_dbus_control_plane_client() -> Any:
    """Construye el cliente D-Bus del control-plane LOCAL (T048 / FR-010).

    En entornos sin D-Bus (CI, dev sin daemon) devuelve un stub que lanza
    AgentUnavailable en cualquier operación mutadora — el endpoint 503s limpio.
    El adaptador real (T053 / dbus_fast_runtime_client.py) se cablea aquí cuando
    está disponible; en su ausencia, este stub es el comportamiento correcto:
    fail-closed, sin fallback passthrough (CTRL-P1-11, SC-005).
    """
    try:
        from hermes.shell_server.chat.dbus_control_plane_adapter import (  # noqa: PLC0415
            DbusControlPlaneAdapter,
        )

        return DbusControlPlaneAdapter(sender_uid=os.getuid())
    except ImportError:
        return _UnavailableControlPlane(
            reason="dbus_control_plane_adapter no disponible en este entorno"
        )


class _UnavailableControlPlane:
    """Stub fail-closed: lanza AgentUnavailable en toda operación (CTRL-P1-11)."""

    def __init__(self, *, reason: str) -> None:
        self._reason = reason

    async def enqueue(self, **_: Any) -> None:
        raise AgentUnavailable(self._reason)

    async def get_queue_status(self, **_: Any) -> None:
        raise AgentUnavailable(self._reason)

    async def list_pending(self, **_: Any) -> tuple:
        raise AgentUnavailable(self._reason)

    async def get_task_status(self, **_: Any) -> None:
        raise AgentUnavailable(self._reason)

    async def list_configured_tasks(self, **_: Any) -> tuple:
        raise AgentUnavailable(self._reason)

    async def list_recent_tasks(self, **_: Any) -> tuple:
        raise AgentUnavailable(self._reason)

    async def pause(self, **_: Any) -> None:
        raise AgentUnavailable(self._reason)

    async def resume(self, **_: Any) -> None:
        raise AgentUnavailable(self._reason)

    async def list_hitl_pending(self, **_: Any) -> list:
        return []

    async def approve(self, **_: Any) -> str:
        raise AgentUnavailable(self._reason)

    async def reject(self, **_: Any) -> None:
        raise AgentUnavailable(self._reason)


def _resolve_operator_display_name() -> str:
    """Return the human operator's display name for the profile endpoint.

    Resolution order:
      1. HERMES_OPERATOR_DISPLAY_NAME env var (explicit override).
      2. GECOS field from /etc/passwd for the 'hermes-user' account.
      3. Username of the current process (os.environ USER).
    Never returns an empty string — falls back to "hermes" as last resort.
    """
    env_name = os.environ.get("HERMES_OPERATOR_DISPLAY_NAME", "").strip()
    if env_name:
        return env_name
    import pwd  # noqa: PLC0415

    for candidate in ("hermes-user", os.environ.get("USER", "hermes")):
        try:
            entry = pwd.getpwnam(candidate)
            gecos = (entry.pw_gecos or "").split(",")[0].strip()
            if gecos:
                return gecos
            if entry.pw_name:
                return entry.pw_name
        except (KeyError, AttributeError):
            continue
    # No real owner name configured — return empty so the UI shows a neutral
    # owner label instead of leaking the internal account name.
    return ""


def _load_bootstrap_commitment(commit_path: Path) -> str | None:
    """Return the hex SHA-256 commitment of the webui bootstrap secret, or None.

    C3 (PASS-5): the bootstrap secret's PLAINTEXT must NOT be readable by the uid
    the agent/daemon/MCP run as, AND the commitment must NOT be REPLACEABLE by
    them — not at steady state, not at provisioning time, and not by redirecting
    the path the reader trusts. On Unix, the right to unlink/rename a directory
    ENTRY is governed by WRITE on the CONTAINING directory, not by the entry's
    own mode/owner. The named adversary is uid 880 (hermes, the daemon) / uid 886
    (hermes-sandbox, the agent/MCP).

    History of the hole:
      * PASS-2 made the plaintext 0400 root:root + commitment 0440 root:hermes,
        but BOTH lived directly in /var/lib/hermes/ (0755 hermes) → uid 880 could
        unlink/rename the root-owned commitment and substitute its own.
      * PASS-3 moved both files into /var/lib/hermes/bootstrap/ (0755 root:root)
        so uid 880/886 could not replace the FILES.
      * PASS-4 made provisioning recreate that bootstrap dir from scratch every
        boot (O_NOFOLLOW + *at, no early-exit, no symlink follow).
      * PASS-5 closes the LAST leg: the PARENT /var/lib/hermes was STILL 0755
        hermes, so AFTER provisioning, uid 880 could `rename` the whole root-owned
        `bootstrap` dir ENTRY away and drop in its OWN `bootstrap` dir (or symlink)
        holding a commitment of a secret it chose. The reader opened the
        commitment by PLAIN PATH (read_text — no O_NOFOLLOW, no ownership check),
        so it would FOLLOW the swapped entry and TRUST the forged commitment. One
        GET / handshake later, the gate is owned.

    The CLASS fix has two independent legs (defence in depth — either alone shuts
    the door; together they are belt-and-braces):

      1. The bootstrap dir lives under a ROOT-OWNED parent uid 880/886 cannot
         write — /var/lib/hermes-bootstrap (0755 root:root), a SIBLING of
         /var/lib/hermes, created by tmpfiles. uid 880 cannot rename/replace the
         `bootstrap` entry because it cannot write that parent at all.

      2. This reader O_NOFOLLOW-opens the commitment and fstat-VERIFIES the file
         AND its containing dir are root-owned regular-file / directory before
         trusting the bytes. If the entry was swapped for a symlink, a non-root
         file, or sits in a non-root dir, we reject (return None) and fail CLOSED.

    Layout (all root:root, recreated fresh each boot by the root ExecStartPre):
      * <parent>/bootstrap/                       0755 root:root
      * <parent>/bootstrap/webui-bootstrap        0400 root:root  PLAINTEXT
            (host owner reads via `podman exec … cat`; unreadable by uid 880/886)
      * <parent>/bootstrap/webui-bootstrap.commit 0444 root:root  COMMITMENT
            (hex SHA-256; world-readable so uid 880 can VERIFY; non-invertible)

    Returns the lowercase-hex digest, or None if absent/unreadable/malformed/
    untrusted — in which case GET / cannot mint a session token (default-deny: no
    commitment ⇒ read-only browse only, never a fail-open gate).
    """
    import stat as _stat  # noqa: PLC0415

    # Open the file with O_NOFOLLOW so a symlink swapped in for the commitment is
    # rejected outright (ELOOP) instead of silently followed to attacker content.
    try:
        fd = os.open(commit_path, os.O_RDONLY | os.O_NOFOLLOW)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        st = os.fstat(fd)
        # Trust ONLY a genuine regular file owned by root:root. A non-regular
        # entry (fifo/socket/dir) or any non-root owner means the path was
        # redirected or replaced — reject and fail closed.
        if not _stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_gid != 0:
            logger.warning(
                "shell_http_auth.bootstrap_commitment_untrusted",
                extra={
                    "path": str(commit_path),
                    "reason": "commitment file is not a root-owned regular file",
                },
            )
            return None
        # The file being root-owned is not enough: an attacker who owns the
        # CONTAINING dir could have planted a root-owned file there is impossible
        # (cannot chown to root), but could redirect the whole `bootstrap` dir
        # entry. Verify the containing dir is itself a root-owned directory,
        # opened WITHOUT following symlinks, so a swapped dir entry is caught.
        try:
            dir_fd = os.open(
                str(commit_path.parent),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
            )
        except (FileNotFoundError, PermissionError, OSError):
            return None
        try:
            dst = os.fstat(dir_fd)
            if (
                not _stat.S_ISDIR(dst.st_mode)
                or dst.st_uid != 0
                or dst.st_gid != 0
            ):
                logger.warning(
                    "shell_http_auth.bootstrap_commitment_untrusted",
                    extra={
                        "path": str(commit_path),
                        "reason": "containing dir is not a root-owned directory",
                    },
                )
                return None
        finally:
            os.close(dir_fd)
        raw = os.read(fd, 4096).decode("utf-8", "replace").strip().lower()
    finally:
        os.close(fd)
    # A SHA-256 hex digest is exactly 64 lowercase hex chars. Reject anything
    # else so a truncated/garbage file can never be mistaken for a valid gate.
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        logger.warning(
            "shell_http_auth.bootstrap_commitment_malformed",
            extra={"path": str(commit_path)},
        )
        return None
    logger.info(
        "shell_http_auth.bootstrap_commitment_loaded",
        extra={
            "path": str(commit_path),
            "note": (
                "owner opens the UI once at http://HOST:PORT/?k=<secret>; read the "
                "plaintext secret from the host (root) with: "
                "podman exec lumen-os cat /var/lib/hermes-bootstrap/bootstrap/"
                "webui-bootstrap"
            ),
        },
    )
    return raw


def _provision_bootstrap_secret() -> None:
    """Provision the webui bootstrap dir + secret + commitment FRESH, as root.

    Invoked ONLY by the unit's root `ExecStartPre=+…` (the `+` prefix makes PID1
    spawn it as uid 0 outside the unit's User=/CapabilityBoundingSet= confinement)
    BEFORE the shell-server (User=hermes) starts. The shell-server itself never
    calls this — it only READS the commitment via `_load_bootstrap_commitment`.

    C3 PASS-5 — provisioning must NOT trust ANY pre-existing bootstrap dir or
    commitment, AND the bootstrap dir's PARENT must itself be root-owned so the
    named adversary cannot rename/replace the `bootstrap` ENTRY after we finish.
    PASS-4 recreated the bootstrap dir from scratch each boot, but its parent was
    /var/lib/hermes (0755 hermes, uid 880, the daemon adversary): on Unix the
    right to rename/unlink a directory ENTRY is governed by WRITE on the
    CONTAINING dir, not the entry's mode. So AFTER provisioning, uid 880 could
    rename our root-owned `bootstrap` away and drop in its OWN dir/symlink with a
    commitment of a secret it chose — and the reader (opening by plain path)
    would follow and trust it. One forged GET / handshake later, the gate is
    owned.

    PASS-5 moves the bootstrap dir under a ROOT-OWNED parent uid 880/886 cannot
    write — /var/lib/hermes-bootstrap (0755 root:root, a SIBLING of
    /var/lib/hermes, created by tmpfiles). Now uid 880 cannot rename/replace the
    `bootstrap` entry because it cannot write that parent at all. This provisioner
    additionally fstat-verifies the parent is root-owned before trusting it, and
    the reader (`_load_bootstrap_commitment`) O_NOFOLLOW-opens + root-verifies
    both the commitment file and its dir — belt and braces.

    The fix: STOP trusting any pre-existing state. Every boot, atomically and via
    O_NOFOLLOW + *at syscalls (no path re-resolution, no symlink following, no
    TOCTOU window):

      1. Open the ROOT-OWNED parent /var/lib/hermes-bootstrap with
         O_NOFOLLOW|O_DIRECTORY (reject a symlinked parent outright) and
         fstat-verify it is a root-owned directory (reject otherwise, fail closed).
      2. rename-away then rm -rf ANY existing `bootstrap` entry (dir, symlink, or
         file) relative to that dir fd — never reuse attacker state.
      3. mkdirat a FRESH `bootstrap`, chown root:root, chmod 0755.
      4. Re-open it with O_NOFOLLOW|O_DIRECTORY and fstat-verify it is a real
         directory, root-owned, mode 0755 — reject anything else.
      5. Generate a NEW 256-bit secret; write the plaintext (0400 root:root) and
         its SHA-256 commitment (0444 root:root) with openat + O_CREAT|O_EXCL|
         O_NOFOLLOW inside the verified dir fd. NEVER early-exit on a pre-existing
         commitment.

    Tradeoff: the secret rotates on every boot, so the owner re-reads it via
    `podman exec … cat /var/lib/hermes-bootstrap/bootstrap/webui-bootstrap` after
    each boot rather than relying on a bookmarked ?k=<secret> URL. Correct
    security posture: an unforgeable, attacker-free gate beats a
    convenient-but-poisonable one. The UI is never a paperweight — the owner
    always has a valid fresh secret on the host side.
    """
    import hashlib  # noqa: PLC0415
    import secrets  # noqa: PLC0415
    import stat as _stat  # noqa: PLC0415

    plaintext_path = Path(
        os.environ.get(
            "HERMES_SHELL_BOOTSTRAP",
            "/var/lib/hermes-bootstrap/bootstrap/webui-bootstrap",
        )
    )
    commit_path = Path(
        os.environ.get(
            "HERMES_SHELL_BOOTSTRAP_COMMIT",
            "/var/lib/hermes-bootstrap/bootstrap/webui-bootstrap.commit",
        )
    )
    bootstrap_dir = plaintext_path.parent
    parent_dir = bootstrap_dir.parent
    bootstrap_name = bootstrap_dir.name
    plaintext_name = plaintext_path.name
    commit_name = commit_path.name

    # 1. Open the PARENT with O_NOFOLLOW|O_DIRECTORY. If the parent is a symlink,
    #    O_NOFOLLOW makes this fail — we refuse to provision under a redirected
    #    parent. All subsequent operations use *at syscalls anchored to this fd so
    #    a concurrent rename of `bootstrap` cannot redirect us mid-flight.
    parent_fd = os.open(parent_dir, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        # 1b. The parent MUST be root-owned. If it is writable by the adversary
        #     (uid 880/886), it could rename/replace our `bootstrap` entry AFTER
        #     we finish provisioning. A non-root parent means the deployment is
        #     misconfigured (tmpfiles must create /var/lib/hermes-bootstrap as
        #     0755 root:root) — refuse rather than provision into an unsafe parent.
        pst = os.fstat(parent_fd)
        if not _stat.S_ISDIR(pst.st_mode) or pst.st_uid != 0 or pst.st_gid != 0:
            raise RuntimeError(
                "bootstrap parent dir is not root-owned: "
                f"{parent_dir} (uid={pst.st_uid} gid={pst.st_gid}); "
                "tmpfiles must create it 0755 root:root"
            )
        # 2. Remove ANY pre-existing `bootstrap` entry, whatever its type. We do
        #    NOT trust it (an attacker uid-880 may have planted a dir, a symlink,
        #    or a file). rmtree handles a real dir; unlink handles symlink/file.
        #    We rename-away first so the removal is atomic from the gate's POV and
        #    a partially-removed tree can never be mistaken for valid state.
        _purge_existing_bootstrap_entry(parent_fd, bootstrap_name)

        # 3. Create a FRESH root-owned dir relative to the verified parent fd.
        os.mkdir(bootstrap_name, mode=0o755, dir_fd=parent_fd)
        os.chown(bootstrap_name, 0, 0, dir_fd=parent_fd, follow_symlinks=False)
        os.chmod(bootstrap_name, 0o755, dir_fd=parent_fd, follow_symlinks=False)

        # 4. Re-open the dir we just created with O_NOFOLLOW and fstat-verify it is
        #    a genuine, root-owned directory before writing secrets into it.
        dir_fd = os.open(
            bootstrap_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
            dir_fd=parent_fd,
        )
        try:
            st = os.fstat(dir_fd)
            if not _stat.S_ISDIR(st.st_mode):
                raise RuntimeError("bootstrap path is not a directory after mkdir")
            if st.st_uid != 0 or st.st_gid != 0:
                raise RuntimeError("bootstrap dir is not root-owned after chown")
            if _stat.S_IMODE(st.st_mode) != 0o755:
                raise RuntimeError("bootstrap dir mode is not 0755 after chmod")

            # 5. Write a FRESH secret + commitment. O_EXCL guarantees we create
            #    the entries (the dir was just made empty); O_NOFOLLOW refuses any
            #    symlink that somehow appeared. NEVER reuse a pre-existing value.
            secret = secrets.token_hex(32)
            _write_root_file(dir_fd, plaintext_name, secret.encode(), 0o400)
            commitment = hashlib.sha256(secret.encode()).hexdigest()
            _write_root_file(dir_fd, commit_name, commitment.encode(), 0o444)
        finally:
            os.close(dir_fd)
    finally:
        os.close(parent_fd)


def _purge_existing_bootstrap_entry(parent_fd: int, name: str) -> None:
    """Atomically rename-away then destroy any existing `name` under parent_fd.

    The entry may be a real directory, a symlink, or a plain file planted by an
    attacker uid-880 (who can write the parent dir). We rename it to a unique
    sibling first (atomic, removes it from the canonical name immediately) and
    then destroy the renamed copy. lstat (follow_symlinks=False) classifies the
    renamed entry without ever traversing a symlink target.
    """
    import errno  # noqa: PLC0415
    import secrets  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import stat as _stat  # noqa: PLC0415

    try:
        os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return  # Nothing to purge — clean slate.

    quarantine = f".{name}.stale-{secrets.token_hex(8)}"
    os.rename(name, quarantine, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)

    st = os.lstat(quarantine, dir_fd=parent_fd)
    if _stat.S_ISDIR(st.st_mode):
        # A real directory: remove its whole subtree. Resolve an absolute path for
        # rmtree since it does not accept a dir_fd-relative name on all platforms.
        target = os.path.join(
            os.readlink(f"/proc/self/fd/{parent_fd}"), quarantine
        )
        shutil.rmtree(target, ignore_errors=True)
        try:
            os.rmdir(quarantine, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                raise
    else:
        # Symlink or plain file: a single unlink removes it (never followed).
        os.unlink(quarantine, dir_fd=parent_fd)


def _write_root_file(dir_fd: int, name: str, data: bytes, mode: int) -> None:
    """Create `name` under dir_fd with exactly `data`, root:root, given mode.

    O_CREAT|O_EXCL|O_NOFOLLOW: the file must NOT already exist (the dir was just
    freshly made) and must never be a symlink. fchown/fchmod operate on the open
    fd so they cannot be redirected by a racing rename.
    """
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
        dir_fd=dir_fd,
    )
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        os.write(fd, data)
    finally:
        os.close(fd)


def _commitment_matches(commitment: str, presented: str) -> bool:
    """Constant-time check that SHA-256(presented) equals the stored commitment.

    `presented` is the secret the owner supplies on GET / (?k= / header). We hash
    it and compare against the commitment the shell-server can read. The shell
    never holds the plaintext secret itself, so it cannot forge a handshake — it
    can only VERIFY one presented by the host-side owner.
    """
    import hashlib  # noqa: PLC0415
    import hmac as _hmac  # noqa: PLC0415

    if not commitment or not presented:
        return False
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return _hmac.compare_digest(digest, commitment)


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager  # noqa: PLC0415

    audit_writer = _build_audit_tail_writer()
    prometheus_exporter = _build_prometheus_exporter()

    async def _boot_apply_egress_grants() -> None:
        # Re-push the owner's persisted egress allow-list to the proxy at boot (the
        # proxy starts at default-deny and does NOT read the grants itself). Retry
        # until the proxy's control socket is up (systemd may start us concurrently).
        import asyncio as _asyncio  # noqa: PLC0415

        from hermes.shell_server.egress_api import apply_persisted_grants  # noqa: PLC0415

        for _ in range(10):
            if await _asyncio.to_thread(apply_persisted_grants):
                return
            await _asyncio.sleep(3)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        import asyncio as _asyncio  # noqa: PLC0415

        audit_writer.start_background()
        _egress_boot = _asyncio.create_task(_boot_apply_egress_grants())
        yield
        _egress_boot.cancel()
        audit_writer.stop()

    app = FastAPI(
        title="Hermes Shell — local backend",
        version="0.4.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    vault = SecretsVault()

    # ── HTTP edge auth (V-1, critical) ────────────────────────────────────────
    # The control plane is reachable via the published port; with NO auth, any
    # client that reaches it is a full operator (create providers → hijack the LLM
    # backend, POST /chat → inject agent tasks, POST /approvals/{id} → auto-approve
    # HITL, POST /mcp → spawn code). Require a per-install Bearer token (HKDF subkey
    # of master.key, stable per-install) on every STATE-CHANGING request. The token
    # is delivered to the same-origin webui via the injected index.html; the run
    # posture publishes on 127.0.0.1 only (network boundary). Closes the unauth
    # chain + the HITL bypass + SSRF reachability + the confused-deputy.
    import hmac as _hmac_mod  # noqa: PLC0415
    import secrets as _secrets_mod  # noqa: PLC0415
    import time as _time_mod  # noqa: PLC0415
    from fastapi import Request as _Req  # noqa: PLC0415
    from fastapi.responses import JSONResponse as _JSONResp  # noqa: PLC0415
    try:
        _AUTH_TOKEN = vault.derive_subkey(label="shell-http-auth").hex()
    except Exception:  # noqa: BLE001 — master.key absent (dev/CI without baked image)
        _AUTH_TOKEN = _secrets_mod.token_hex(32)
        logger.warning("shell_http_auth.master_key_absent — ephemeral token (dev/CI)")
    # The stable per-install operator token is NEVER served to a browser anymore
    # (C3): serving it in the unauth GET / let any process reaching :7517 scrape a
    # full-mutator credential that never rotated. It stays server-side only, used
    # to verify the daemon↔shell internal calls and to MINT short-lived session
    # tokens once an owner proves possession of the bootstrap secret (below).
    app.state.shell_auth_token = _AUTH_TOKEN

    # ── Bootstrap commitment (C3 PASS-5, owner-proof, uid-decoupled + unmovable) ─
    # PASS-1 wrote a 0600 plaintext owned by uid 880 (hermes). PASS-2 split it into
    # root:root plaintext + commitment, but BOTH lived in /var/lib/hermes/ (0755
    # hermes) so uid 880 could unlink/rename them. PASS-3 moved both into a
    # bootstrap/ subdir (root:root). PASS-4 recreated that subdir from scratch each
    # boot. PASS-5 closes the LAST leg: the PARENT was still /var/lib/hermes (0755
    # hermes), so AFTER provisioning uid 880 could rename the root-owned `bootstrap`
    # ENTRY itself and substitute its own dir/symlink (dir-write governs entry
    # replacement, not the entry's mode). The CLASS fix:
    #   1. The bootstrap dir now lives under a ROOT-OWNED parent uid 880/886 cannot
    #      write — /var/lib/hermes-bootstrap (0755 root:root, created by tmpfiles,
    #      a SIBLING of /var/lib/hermes). uid 880 cannot rename/replace the
    #      `bootstrap` entry because it cannot write that parent at all.
    #   2. _load_bootstrap_commitment O_NOFOLLOW-opens the commitment and
    #      fstat-verifies the file AND its dir are root-owned before trusting it —
    #      a swapped entry (symlink, non-root file, non-root dir) is rejected,
    #      fail-closed.
    #   * the PLAINTEXT is provisioned root:root 0400 by the unit's root
    #     ExecStartPre (see ops/container dropin) — unreadable by uid 880/886;
    #   * the shell-server only ever READS the COMMITMENT (hex SHA-256), root:root
    #     0444 — world-readable but root-only-writable, non-invertible, unmovable.
    # The owner reads the plaintext from the host (root): `podman exec … cat
    # /var/lib/hermes-bootstrap/bootstrap/webui-bootstrap`, then presents it ONCE
    # to GET / (?k= or the X-Lumen-Bootstrap header). On a constant-time commitment
    # match GET / mints a short-lived ROTATING session token (never the operator
    # token). Default-deny: no commitment ⇒ no token, read-only browse only — never
    # a fail-open gate.
    _BOOTSTRAP_COMMIT_PATH = Path(
        os.environ.get(
            "HERMES_SHELL_BOOTSTRAP_COMMIT",
            "/var/lib/hermes-bootstrap/bootstrap/webui-bootstrap.commit",
        )
    )
    _BOOTSTRAP_COMMITMENT = _load_bootstrap_commitment(_BOOTSTRAP_COMMIT_PATH)
    if _BOOTSTRAP_COMMITMENT is None:
        logger.warning(
            "shell_http_auth.bootstrap_unavailable",
            extra={
                "reason": (
                    "no bootstrap commitment — GET / cannot mint session tokens "
                    "(read-only browse only). Production: the root ExecStartPre "
                    "provisions it; dev/CI without that step has no owner gate."
                )
            },
        )
    app.state.shell_bootstrap_commitment = _BOOTSTRAP_COMMITMENT

    # ── Rotating session tokens (C3, option c) ────────────────────────────────
    # Short-lived bearer tokens minted after a successful bootstrap handshake. The
    # webui sends one as `Authorization: Bearer` on mutating calls. Each is random,
    # expires, and is independent of the stable operator token, so a leaked page /
    # history entry exposes at most a single short-lived credential, not the
    # install's master mutator key.
    _SESSION_TTL_S = int(os.environ.get("HERMES_SHELL_SESSION_TTL_S", "3600"))
    _session_tokens: dict[str, float] = {}  # token → unix expiry

    def _mint_session_token() -> str:
        now = _time_mod.time()
        # Opportunistic GC so the dict can't grow unbounded across reloads.
        for _t, _exp in list(_session_tokens.items()):
            if _exp <= now:
                _session_tokens.pop(_t, None)
        tok = _secrets_mod.token_hex(32)
        _session_tokens[tok] = now + _SESSION_TTL_S
        return tok

    def _session_token_valid(tok: str) -> bool:
        exp = _session_tokens.get(tok)
        if exp is None:
            return False
        if exp <= _time_mod.time():
            _session_tokens.pop(tok, None)
            return False
        return True

    app.state.mint_session_token = _mint_session_token
    _MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    @app.middleware("http")
    async def _require_operator_token(request: _Req, call_next):  # noqa: ANN001,ANN202
        path = request.url.path
        if request.method in _MUTATING_METHODS and path.startswith("/api/v1/"):
            auth = request.headers.get("authorization", "")
            token = auth[7:] if auth[:7].lower() == "bearer " else ""
            # Accept EITHER the server-side operator token (internal callers) OR a
            # valid unexpired rotating session token minted via the bootstrap
            # handshake (the webui). compare_digest on the operator path keeps the
            # comparison constant-time; the session path is a dict membership of a
            # high-entropy random token.
            operator_ok = bool(token) and _hmac_mod.compare_digest(token, _AUTH_TOKEN)
            if not (operator_ok or (token and _session_token_valid(token))):
                return _JSONResp(
                    {"detail": "unauthorized: operator token required"},
                    status_code=401,
                )
        return await call_next(request)

    repo = SQLiteProviderRepository(db_path=_DB_PATH, vault=vault)
    _seed_providers_if_empty(repo)
    from hermes.shell_server.chat.conversation_repo import (
        SQLiteConversationRepository,
    )

    conv_repo = SQLiteConversationRepository(db_path=_DB_PATH)
    # Registro de agentes compartido (misma shell-state.db que el daemon). El
    # shell-server SOLO lo LEE (agente activo para taguear la conversación); la
    # gobernanza vive en el daemon vía D-Bus (Principio 0). El seed es race-safe.
    from hermes.agents.infrastructure.sqlite_agent_registry import (  # noqa: PLC0415
        SqliteAgentRegistry,
    )

    agent_registry = SqliteAgentRegistry(db_path=_DB_PATH)
    app.state.repo = repo
    app.state.vault = vault
    app.state.conv_repo = conv_repo
    app.state.audit_writer = audit_writer
    app.state.prometheus_exporter = prometheus_exporter
    # T048: ControlPlanePort client (D-Bus → daemon). Populated here so tests
    # can replace app.state.control_plane with a stub before the first request.
    # The real client is built lazily at first use to avoid D-Bus errors at
    # import time in non-OS environments.
    app.state.control_plane = _build_dbus_control_plane_client()

    from hermes.shell_server.training.api import (
        _get_orchestrator,
        create_training_router,
    )

    # Wire the teaching (spec 004/US3) isolation layer. open_teaching_session
    # opens an isolated context (agent-browser --session) and claims OPERATOR
    # input-ownership in the ledger; the recording lifecycle (start/stop/sign) is
    # driven by the training router itself. We pass the same orchestrator the
    # router uses for consistency.
    from hermes.agents_os.application.teaching.input_ownership_ledger import (
        InputOwnershipLedger,
    )
    from hermes.agents_os.application.teaching.teaching_session_orchestrator import (
        TeachingSessionOrchestrator,
    )
    from hermes.agents_os.infrastructure.agent_browser_teaching_context import (
        AgentBrowserTeachingContext,
    )

    _teaching_orchestrator = TeachingSessionOrchestrator(
        training_orchestrator=_get_orchestrator(_DB_PATH),
        context_factory=AgentBrowserTeachingContext(),
        ledger=InputOwnershipLedger(),
    )
    app.include_router(
        create_training_router(_DB_PATH, teaching_orchestrator=_teaching_orchestrator)
    )

    from hermes.shell_server.agent_browser import create_browser_router

    app.include_router(create_browser_router())

    # Integrations (Composio) es OPCIONAL por diseño. Si su import o registro
    # falla — p.ej. el SDK de Composio no puede crear su cache dir — NO debe
    # tumbar el shell-server entero (chat, providers, onboarding dependen de él).
    # Degradamos el panel de Integraciones, no el SO. El error queda en el journal.
    try:
        from hermes.shell_server.integrations.api import create_integrations_router

        app.include_router(create_integrations_router(_DB_PATH))
    except Exception:  # noqa: BLE001
        logger.exception(
            "Integrations (Composio) no disponible — el shell-server arranca sin "
            "el panel de Integraciones (degradación, no fallo total)"
        )

    from hermes.shell_server.audit_api import create_audit_router

    app.include_router(create_audit_router(_DB_PATH))

    # Wizard HTTP eliminado (Principio 0: SO-nativo, no API). El onboarding
    # de providers vive ahora en la UI nativa LumenSO (ProvidersApp) que llama
    # a D-Bus `configure_native_provider` directo.
    from hermes.shell_server.setup.api import create_setup_router

    app.include_router(create_setup_router())

    from hermes.shell_server.remote_access_tunnel.api import (
        create_remote_access_tunnel_router,
    )

    app.include_router(create_remote_access_tunnel_router())

    from hermes.shell_server.remote_control.api import (
        create_remote_control_router,
    )

    rc_key = vault.derive_subkey(label="remote-control:token-cipher")
    rc_kid = os.environ.get("HERMES_RC_KID", "rc-v1")
    rc_signaling = os.environ.get(
        "HERMES_RC_SIGNALING_WS",
        "ws://127.0.0.1:7518/rc",
    )
    app.include_router(
        create_remote_control_router(
            db_path=_DB_PATH,
            cipher_key=rc_key,
            cipher_kid=rc_kid,
            signaling_ws_base=rc_signaling,
        )
    )

    # ------------------------------------------------------------------
    # Healthz & profile
    # ------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "hermes-shell-server",
            "version": "0.4.0",
            "ts": datetime.now(tz=UTC).isoformat(),
        }

    @app.get("/api/v1/profile")
    async def profile() -> dict[str, Any]:
        profile_name = "unknown"
        try:
            with open("/etc/agents-os-profile", encoding="utf-8") as f:
                profile_name = f.read().strip()
        except OSError:
            pass
        display_name = _resolve_operator_display_name()
        return {
            "profile": profile_name,
            "user": os.environ.get("USER", ""),
            "display_name": display_name,
        }

    @app.get("/metrics")
    async def metrics() -> Any:
        """Prometheus text-format metrics (gated by telemetry opt-in FR-061)."""
        from fastapi.responses import PlainTextResponse  # noqa: PLC0415

        body = prometheus_exporter.render_textfile()
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/audit/tail/stats")
    async def audit_tail_stats() -> dict[str, Any]:
        stats = audit_writer.stats()
        return {
            "queued_in_memory": stats.queued_in_memory,
            "persisted_pending": stats.persisted_pending,
            "published_total": stats.published_total,
            "failures_total": stats.failures_total,
            "last_publish_at": stats.last_publish_at.isoformat()
            if stats.last_publish_at
            else None,
        }

    @app.get("/api/v1/runtime/status")
    async def runtime_status() -> dict[str, Any]:
        return {
            "state": "idle",
            "active_task_count": 0,
            "telemetry_enabled": False,
            "captured_at": datetime.now(tz=UTC).isoformat(),
        }

    # ------------------------------------------------------------------
    # Tasks dashboard (F007 — supervision read-only)
    # GET /api/v1/tasks/configured — one row per authorized trigger
    # GET /api/v1/tasks/recent     — recent work items activity log
    # Fail-soft: returns empty list + available=false on daemon unavailable
    # ------------------------------------------------------------------

    @app.get("/api/v1/tasks/configured")
    async def list_configured_tasks(limit: int = 200) -> dict[str, Any]:
        """Configured tasks dashboard.

        Returns all non-revoked authorized triggers with their recurrence,
        last-run time, last status, and next scheduled fire (for timer triggers).

        Fail-soft: if the runtime daemon is unavailable, returns an empty list
        with available=false — the shell renders a disconnected state.
        """
        try:
            rows = await app.state.control_plane.list_configured_tasks(limit=limit)
            return {
                "available": True,
                "tasks": [
                    {
                        "trigger_id": r.trigger_id,
                        "label": r.label,
                        "trigger_type": r.trigger_type,
                        "recurrence": r.recurrence,
                        "recurrence_human": getattr(r, "recurrence_human", "") or "",
                        "enabled": r.enabled,
                        "risk_ceiling": r.risk_ceiling,
                        "last_run_at": r.last_run_at,
                        "last_status": r.last_status,
                        "next_run_at": r.next_run_at,
                        # Per-agent attribution for the calendar board.
                        "target_agent_id": getattr(r, "target_agent_id", "") or "",
                        # P3 fields — present on rows created after the P3 migration.
                        "one_shot": bool(getattr(r, "one_shot", False)),
                        "task_instruction": getattr(r, "task_instruction", "") or "",
                        "title": getattr(r, "title", "") or "",
                    }
                    for r in rows
                ],
            }
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.shell_server.tasks.configured.unavailable",
                extra={"reason": str(exc)},
            )
            return {"available": False, "tasks": []}

    @app.get("/api/v1/tasks/recent")
    async def list_recent_tasks(limit: int = 50) -> dict[str, Any]:
        """Recent work items activity log.

        Returns the most recent work items across all statuses, ordered by
        enqueued_at descending. instruction is truncated to 120 chars.

        Fail-soft: if the runtime daemon is unavailable, returns an empty list
        with available=false.
        """
        try:
            rows = await app.state.control_plane.list_recent_tasks(limit=limit)
            return {
                "available": True,
                "tasks": [
                    {
                        "task_id": r.task_id,
                        "label": r.label,
                        "status": r.status,
                        "trigger_kind": r.trigger_kind,
                        "enqueued_at": r.enqueued_at,
                        "claimed_at": r.claimed_at,
                    }
                    for r in rows
                ],
            }
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.shell_server.tasks.recent.unavailable",
                extra={"reason": str(exc)},
            )
            return {"available": False, "tasks": []}

    # ------------------------------------------------------------------
    # Chat: mirror read-only del historial + POST encola vía control-plane
    # T048: POST /api/v1/chat → ControlPlanePort.enqueue (sin fallback).
    # T055: WS /ws/chat passthrough ELIMINADO; conversation_repo = mirror read-only.
    # CTRL-P1-26 / G6 / SC-004.
    # ------------------------------------------------------------------

    @app.get("/api/v1/chat/conversations")
    async def list_conversations(agent_id: str | None = None) -> list[dict]:
        """Recientes (supervisión read-only). ?agent_id filtra por agente del
        roster; sin él devuelve todas las conversaciones."""
        items = conv_repo.list_summaries(agent_id=agent_id)
        return [
            {
                "conversation_id": str(c.conversation_id),
                "title": c.title,
                "provider_alias": c.provider_alias,
                "model": c.model,
                "started_at": c.started_at.isoformat(),
                "last_msg_at": c.last_msg_at.isoformat(),
                "message_count": c.message_count,
                "agent_id": c.agent_id,
            }
            for c in items
        ]

    @app.get("/api/v1/chat/conversations/{conv_id}")
    async def get_conversation(conv_id: UUID) -> dict:
        """Mirror read-only — T055: no escribe desde este handler."""
        try:
            d = conv_repo.get_detail(conversation_id=conv_id)
        except Exception:
            raise HTTPException(404, "conversation not found")
        return {
            "conversation_id": str(d.conversation_id),
            "title": d.title,
            "provider_alias": d.provider_alias,
            "model": d.model,
            "started_at": d.started_at.isoformat(),
            "messages": [
                {"role": m.role, "content": m.content} for m in d.messages
            ],
        }

    @app.delete("/api/v1/chat/conversations/{conv_id}", status_code=204)
    async def delete_conversation(conv_id: UUID) -> None:
        try:
            conv_repo.delete(conversation_id=conv_id)
        except Exception:
            raise HTTPException(404, "conversation not found")

    @app.post("/api/v1/chat", response_model=ChatStartResponse)
    async def chat_start(payload: ChatRequest) -> ChatStartResponse:
        """T048 🔒 — Encola el mensaje vía ControlPlanePort.enqueue.

        Fail-hard: si el daemon no está disponible → 503 agent_unavailable.
        Sin fallback passthrough (CTRL-P1-11, CTRL-P1-26, SC-005, FR-010).

        enqueued_by lo deriva el daemon del sender_uid del bus (GetConnectionUnixUser).
        El shell-server NO lo manda en el payload — sería spoofeable.
        """
        from hermes.tasks.control_plane.domain.ports import (  # noqa: PLC0415
            AuthenticatedChannel,
        )

        channel = AuthenticatedChannel(sender_uid=os.getuid())
        conv_id_str = payload.conversation_id or str(uuid4())
        # CTRL-P1-27: dedup_key por mensaje de chat (1 ejecución por doble-envío).
        dedup_key = payload.dedup_key or f"chat:{conv_id_str}:{hash(payload.user_message)}"

        try:
            result = await app.state.control_plane.enqueue(
                channel=channel,
                trigger_kind="chat_message",
                text=payload.user_message,
                priority=0,
                dedup_key=dedup_key,
                # I5 (schema agent_tasks): un chat_message DEBE llevar
                # conversation_id o el INSERT OR IGNORE lo descarta en silencio.
                conversation_id=conv_id_str,
            )
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.shell_server.chat.agent_unavailable",
                extra={"reason": str(exc)},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "agent_unavailable",
                    "message": "El agente no está disponible. Comprueba que hermes-runtime está activo.",
                },
            ) from exc

        logger.info(
            "hermes.shell_server.chat.enqueued",
            extra={"task_id": str(result.task_id), "conv_id": conv_id_str},
        )

        # NOTA: la persistencia del mensaje del usuario (create_or_touch +
        # append_message role="user") la hace el DAEMON al encolar — es el dueño
        # del store (GATE 0 / M2, dbus_runtime_service.enqueue). El shell-server
        # NO debe persistir aquí: hacerlo duplicaba cada mensaje del usuario en el
        # mirror (se veía 2× al reabrir la conversación). conv_repo es read-only
        # para la cara; el daemon escribe, los GET /chat/conversations leen.

        return ChatStartResponse(
            task_id=str(result.task_id),
            stream_path=result.stream_path,
        )

    # ------------------------------------------------------------------
    # Providers REST endpoint — exposes the configured LLM providers to
    # the web UI model picker. Read-only, no secrets returned.
    # ------------------------------------------------------------------

    @app.get("/api/v1/providers")
    async def list_providers() -> list[dict]:
        """List configured providers (read-only, no API keys returned)."""
        providers = repo.list_all()
        return [
            {
                "provider_id": str(p.provider_id),
                "alias": p.alias,
                "kind": p.kind.value if hasattr(p.kind, "value") else str(p.kind),
                "default_model": p.default_model,
                "base_url": p.base_url,
                "is_active": p.is_active,
                "enabled": p.enabled,
            }
            for p in providers
        ]

    # ------------------------------------------------------------------
    # Lumen Cowork — web UI API routers (must be registered BEFORE the
    # static mount so /api/v1/* routes are resolved first).
    # ------------------------------------------------------------------

    from hermes.shell_server.cowork.chat_stream import (  # noqa: PLC0415
        create_chat_stream_router,
    )
    from hermes.shell_server.cowork.workspace_api import (  # noqa: PLC0415
        create_workspace_router,
    )
    from hermes.shell_server.cowork.approvals_api import (  # noqa: PLC0415
        create_approvals_router,
    )
    from hermes.shell_server.cowork.policies_api import (  # noqa: PLC0415
        create_policies_router,
    )

    from hermes.shell_server.egress_api import create_egress_router  # noqa: PLC0415

    app.include_router(create_chat_stream_router())
    app.include_router(create_workspace_router())
    app.include_router(create_approvals_router())
    app.include_router(create_policies_router())
    app.include_router(create_egress_router())

    # ------------------------------------------------------------------
    # D-Bus runtime proxy — shared by all new REST routers.
    # Instantiated once here; individual requests call it per-operation.
    # ------------------------------------------------------------------
    from hermes.shell_server.cowork.dbus_proxy import DbusRuntimeProxy  # noqa: PLC0415

    app.state.dbus_proxy = DbusRuntimeProxy()

    # ------------------------------------------------------------------
    # New REST routers: providers native, agents, skills hub, mcp,
    # tasks mutations, security center, memory.
    # All registered BEFORE the static mount so /api/v1/* is resolved first.
    # ------------------------------------------------------------------
    from hermes.shell_server.cowork.providers_api import (  # noqa: PLC0415
        create_providers_router,
    )
    from hermes.shell_server.cowork.agents_api import (  # noqa: PLC0415
        create_agents_router,
        create_composio_router,
    )
    from hermes.shell_server.cowork.skills_api import (  # noqa: PLC0415
        create_skills_hub_router,
    )
    from hermes.shell_server.cowork.mcp_api import (  # noqa: PLC0415
        create_mcp_router,
    )
    from hermes.shell_server.cowork.tasks_api import (  # noqa: PLC0415
        create_tasks_router,
    )
    from hermes.shell_server.cowork.security_api import (  # noqa: PLC0415
        create_security_router,
    )
    from hermes.shell_server.cowork.memory_api import (  # noqa: PLC0415
        create_memory_router,
    )
    from hermes.shell_server.cowork.web_search_api import (  # noqa: PLC0415
        create_web_search_router,
    )

    app.include_router(create_providers_router())
    app.include_router(create_agents_router())
    app.include_router(create_composio_router())
    app.include_router(create_skills_hub_router())
    app.include_router(create_mcp_router())
    app.include_router(create_tasks_router())
    app.include_router(create_security_router())
    app.include_router(create_memory_router())
    app.include_router(create_web_search_router())

    # ------------------------------------------------------------------
    # Static web UI — mounted LAST so it never shadows /api/v1/* or /ws/*.
    # The webui/ directory is owned by the frontend engineer; we only mount it.
    # html=True makes FastAPI serve index.html for bare directory requests so
    # client-side routing works (GET / → index.html).
    # If the directory is absent (dev without a frontend build), skip silently
    # so the API server still starts cleanly.
    # ------------------------------------------------------------------
    _webui_dir = Path(__file__).parent / "webui"
    if _webui_dir.is_dir():
        from fastapi.staticfiles import StaticFiles  # noqa: PLC0415
        from fastapi.responses import FileResponse  # noqa: PLC0415
        from starlette.types import Scope  # noqa: PLC0415

        class _NoCacheStatic(StaticFiles):
            """Serve assets with no-cache so the browser always revalidates.

            The web UI is a single-page app whose ES modules import each other by
            relative path with no content hash in the URL. With the default
            (no Cache-Control) browsers apply heuristic caching and keep serving
            stale modules after an update — so a deploy/bake silently doesn't show.
            Forcing revalidation (etag/last-modified still make it a cheap 304)
            keeps the UI always fresh without disabling caching entirely.
            """

            async def get_response(self, path: str, scope: Scope):  # noqa: ANN001
                resp = await super().get_response(path, scope)
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
                return resp

        # Assets are served under /webui/* — this MATCHES the absolute paths the
        # frontend uses in index.html (<link href="/webui/style.css">,
        # <script src="/webui/js/app.js">, /webui/assets/...). The JS modules
        # import each other relatively (./foo.js), so they resolve under /webui/
        # too. Mounted before the bare "/" route; /api/* and /ws/* are registered
        # earlier so neither is shadowed.
        app.mount("/webui", _NoCacheStatic(directory=str(_webui_dir)), name="webui")

        @app.get("/", include_in_schema=False)
        async def _serve_webui_index(request: Request):  # noqa: ANN202
            # C3 PASS-5: do NOT serve the stable operator token to unauthenticated
            # GET /. The owner proves possession of the bootstrap secret (?k=… or
            # the X-Lumen-Bootstrap header — its PLAINTEXT is root:root 0400 inside
            # the root-owned /var/lib/hermes-bootstrap/bootstrap/ dir, readable
            # ONLY from the host side, see _load_bootstrap_commitment).
            # The shell-server holds only a non-invertible COMMITMENT, so it can
            # VERIFY a presented secret but cannot itself forge the handshake — and
            # neither can any in-container uid-880/uid-886 process, which at most
            # reads the same commitment. On a match we mint a SHORT-LIVED ROTATING
            # session token and inject THAT; scraping the page yields no usable
            # mutator credential. Without a valid secret the SPA still loads
            # (read-only browse), it simply can't mutate. The token never crosses
            # the network boundary (loopback publish) and is not the master key.
            import json as _json_mod  # noqa: PLC0415

            from fastapi.responses import HTMLResponse  # noqa: PLC0415

            presented = (
                request.query_params.get("k")
                or request.headers.get("x-lumen-bootstrap", "")
            )
            page = (_webui_dir / "index.html").read_text(encoding="utf-8")
            if presented and _commitment_matches(
                app.state.shell_bootstrap_commitment or "", presented
            ):
                session_token = app.state.mint_session_token()
                # JSON-encode the minted value so it can never break out of the JS
                # string literal (defense in depth).
                inject = (
                    "<script>window.__LUMEN_TOKEN__="
                    + _json_mod.dumps(session_token)
                    + ";</script>"
                )
                page = page.replace("</head>", inject + "</head>", 1)
            # no-store: the response may carry a freshly minted session token AND
            # the request URL may carry the bootstrap secret — neither must be
            # persisted by any intermediary or the browser disk cache.
            return HTMLResponse(page, headers={"Cache-Control": "no-store"})

        logger.info("hermes.shell_server.webui.mounted", extra={"path": str(_webui_dir)})
    else:
        logger.info(
            "hermes.shell_server.webui.absent",
            extra={"path": str(_webui_dir), "note": "web UI not bundled — API-only mode"},
        )

    return app


def main() -> int:
    import uvicorn  # noqa: PLC0415

    from hermes.logging_setup import configure_structured_logging  # noqa: PLC0415

    configure_structured_logging(service="hermes-shell-server", version="0.4.0")
    # Default 127.0.0.1 (production-safe). Para VM con SLIRP hostfwd,
    # systemd unit override a 0.0.0.0 (SLIRP enruta a la IP guest, no
    # al loopback del guest).
    host = os.environ.get("HERMES_SHELL_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("HERMES_SHELL_BIND_PORT", "7517"))
    logger.info("hermes-shell-server binding %s:%s", host, port)
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level="info",
        # V (forensics + DoS): record HTTP footsteps (the daemon audit logs agent
        # actions, not attacker HTTP calls), cap concurrent connections, and drop
        # idle keep-alives so a connection flood can't exhaust the single-loop daemon.
        access_log=True,
        limit_concurrency=256,
        timeout_keep_alive=15,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
