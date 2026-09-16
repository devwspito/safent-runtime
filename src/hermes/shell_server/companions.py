"""companions — root-staged registry loader + strict validation (024).

A "companion" is a sibling container Safent's launcher provisions and joins
to a FIXED network (`safent-companions`, 10.201.0.0/24) at install time —
today, exactly one: `safent-ads`. Unlike a user-typed managed-remote URL
(`hermes.shell_server.managed_remote_endpoints`, the OPPOSITE trust
direction: DNS-name-only, no IP literal), a companion's destination is
PINNED by the local installer, never by the owner typing a URL and never by
the daemon:

  - `/etc/hermes/companions/companions.json` is the read-only source registry.
    Root validates it explicitly and stages the accepted entries at
    `/run/hermes/companions/companions.json`, 0440 root:hermes. The daemon
    reads only that staged registry. `/etc` is read-only to hermes-runtime.service
    (ProtectSystem=strict) — no D-Bus verb, no REST path, no config-sync
    verb writes this file (INV-2). This loader is the ONLY reader.
  - The path is a CONSTANT — no env override — an override would be a knob
    a compromised process could point elsewhere (mirrors managed_remote_
    endpoints._ENDPOINTS_PATH's own no-override rule, stricter here since a
    companion's IP is trusted for a live nftables accept rule).
  - `slug` must be one of the shipped companions (`_COMPANION_SLUGS`) — an
    unknown slug is a tampered/future file this build doesn't understand.
  - `url` must be `https://<host ending in .safent.internal>:8443/...` — the
    EXACT INVERSE of managed_remote_endpoints' DNS-name rule: here the name
    is fixed by us, never a name the owner could point at a third party.
  - `ip` must sit inside the companion subnet (10.201.0.0/24) and match
    `port == 8443` — both are product constants (spec.md §7); a value
    outside them is a fabricated/corrupted file, not "a different
    companion" — reject entirely (SC-3).
  - `ca_fingerprint` is RE-DERIVED from the DER bytes at `ca_path` and
    cross-checked against the JSON value — the JSON field is a tamper
    check, never the root of trust; `ca_path`/the `bearer_ref` file target
    must live under the companion's own read-only mount directory (no
    arbitrary host file read via a corrupted JSON value).
  - Any single anomaly discards the WHOLE entry (fail-soft to "no
    companion" for that slug, never a partially-trusted one) — a companion
    is optional infrastructure; Safent boots and runs without it (FR-3).

Infrastructure layer: filesystem-backed (JSON + PEM), stdlib only.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import ssl
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

COMPANION_SOURCE_REGISTRY_PATH = Path("/etc/hermes/companions/companions.json")
COMPANION_RUNTIME_REGISTRY_PATH = Path("/run/hermes/companions/companions.json")
_COMPANIONS_PATH = COMPANION_RUNTIME_REGISTRY_PATH
_COMPANION_MOUNT_DIR = Path("/etc/hermes/companions")
_COMPANION_SUBNET = ipaddress.ip_network("10.201.0.0/24")
_ALLOWED_PORT = 8443
_ALLOWED_SCHEME = "https"
_HOST_SUFFIX = ".safent.internal"
_BEARER_REF_SCHEME = "file:"
_STAGED_REGISTRY_MODE = 0o440

# Where the root `ExecStartPre=-+` of hermes-runtime.service stages a copy of
# each companion's bearer (see ops/agents-os-edition/scripts/hermes-companion-
# bearer). tmpfs, 0750 root:hermes, one 0440 root:hermes file per slug. This
# exists because the bearer's uid/gid on the read-only HOST bind mount is an
# ENGINE artefact (0:0 rootless, 1000:1000 rootful) that is never `hermes`, so
# the daemon (uid 880) cannot read the mount directly on every engine.
COMPANION_RUNTIME_BEARER_DIR = "/run/hermes/companions"


def runtime_bearer_path(slug: str) -> str:
    """The staged-bearer path for *slug* — derived from the VALIDATED slug, never
    from any JSON field (a tampered `bearer_ref` can never point here)."""
    return f"{COMPANION_RUNTIME_BEARER_DIR}/{slug}.bearer"


# The 026 SSO private key (contracts/sso.md §3) has the EXACT SAME unreadable-
# by-the-daemon problem as the bearer above, and the SAME fix: `hermes-
# companion-bearer`'s root `ExecStartPre=-+` also copies this one. Fixed path,
# not per-slug — there is exactly one companion today (_COMPANION_SLUGS) and
# `companion_sso_authority.py` never templates it on a JSON field.
COMPANION_SSO_KEY_MOUNT_PATH = f"{_COMPANION_MOUNT_DIR}/ads-sso.key"
COMPANION_RUNTIME_SSO_KEY_PATH = f"{COMPANION_RUNTIME_BEARER_DIR}/ads-sso.key"

# The only companions this build knows how to seed/trust. An entry for any
# other slug is a tampered or future-version file — rejected, not ignored
# per-field (a slug we don't recognise gets NO partial trust).
_COMPANION_SLUGS: frozenset[str] = frozenset({"safent-ads"})


class CompanionConfigError(ValueError):
    """Raised internally when one companion entry fails validation.

    Never escapes `load_companions`/`get_companion` — callers only ever see
    the entry silently dropped (fail-soft, SC-3).
    """


@dataclass(frozen=True)
class CompanionEndpoint:
    """A validated companion — the ONLY shape `_grant_mcp_egress_for_managed_
    remote`/`_mcp_connect`/the nft generator are allowed to trust."""

    slug: str
    url: str
    host: str
    ip: str
    port: int
    ca_path: str
    ca_fingerprint: str
    bearer_ref: str

    @property
    def argv(self) -> list[str]:
        """The mcp-remote argv this companion is reached through (plan.md §1.4).

        `--header "Authorization: Bearer ${ADS_BEARER}"` is the literal
        `${VAR}` placeholder text, NOT the bearer's value (INV-4) — mcp-remote
        expands it from its own process env at connect time (`ADS_BEARER`,
        filled by `_autowire_companion_env`). Without this flag mcp-remote
        sends no Authorization header at all and every request to `/mcp`
        (BearerTokenMiddleware, safent-ads's mcp/presentation/http.py) is
        rejected 401 — the companion would list as a seeded server with zero
        reachable tools, not "absent" (FR-3) but silently broken instead.
        """
        return [
            "npx", "-y", "mcp-remote@0.8.6", self.url,
            "--header", "Authorization: Bearer ${ADS_BEARER}",
        ]


def load_companions(*, path: Path | None = None) -> dict[str, CompanionEndpoint]:
    """Return {slug: CompanionEndpoint} for every entry in *path* that
    validates. Fail-soft to {} on ANY anomaly (missing file, bad owner/
    permissions, malformed JSON, wrong version, any entry failing
    validation) — a companion is optional infrastructure (FR-3)."""
    path = _COMPANIONS_PATH if path is None else path
    if path == _COMPANIONS_PATH and not _is_root_staged_registry(path):
        return {}
    if not _is_trustworthy_file(path):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        return {}
    raw_entries = data.get("companions")
    if not isinstance(raw_entries, list):
        return {}

    result: dict[str, CompanionEndpoint] = {}
    for raw in raw_entries:
        try:
            endpoint = _validate_companion_entry(raw)
        except CompanionConfigError:
            continue
        result[endpoint.slug] = endpoint
    return result


def get_companion(slug: str, *, path: Path | None = None) -> CompanionEndpoint | None:
    """Return the validated companion for *slug*, or None if absent/invalid."""
    return load_companions(path=path).get(slug)


def is_companion_secret_file_trustworthy(path: Path) -> bool:
    """Public wrapper around `_is_trustworthy_file` — the SAME ownership/
    permission invariant this module enforces on `companions.json` also
    guards every other secret this build mounts read-only under
    `/etc/hermes/companions/` (the bearer today, the 026 SSO private key,
    `/etc/hermes/companions/ads-sso.key`, T004). Exposed so those loaders
    reuse this ONE check instead of re-implementing it (single source of
    truth for "no unprivileged/compromised process in this container could
    have planted or edited this file")."""
    return _is_trustworthy_file(path)


def read_companion_bearer(
    endpoint: CompanionEndpoint, *, prefer_runtime_copy: bool = True
) -> str | None:
    """Read *endpoint*'s bearer token.

    Two sources, in order:

      1. the root-staged copy at `runtime_bearer_path(slug)` (0440 root:hermes
         on tmpfs) — the ONLY one the daemon's uid 880 can read on every
         container engine, since the bind-mounted original's ownership is an
         engine artefact (see COMPANION_RUNTIME_BEARER_DIR). Its path comes
         from the validated slug, never from the JSON.
      2. the `bearer_ref` file on the read-only mount itself — used by the
         stage-in script (`prefer_runtime_copy=False`, so a rotated bearer is
         picked up from the source, not from last boot's copy) and as the
         fallback wherever the mount happens to be readable.

    Returns None (never raises) on any I/O error or an out-of-mount path —
    the bearer never appears in argv/logs/REST (INV-4); this is the ONLY
    function allowed to read its value, and only at connect time.
    """
    if prefer_runtime_copy:
        staged = _read_secret_file(Path(runtime_bearer_path(endpoint.slug)))
        if staged:
            return staged
    if not endpoint.bearer_ref.startswith(_BEARER_REF_SCHEME):
        return None
    bearer_path = Path(endpoint.bearer_ref[len(_BEARER_REF_SCHEME):])
    if not _is_within_companion_mount(bearer_path):
        return None
    return _read_secret_file(bearer_path)


def _read_secret_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Validation internals
# ---------------------------------------------------------------------------


def _is_root_staged_registry(path: Path) -> bool:
    """A daemon-owned/injected tmpfs file is never a root staging result."""
    try:
        file_stat = path.lstat()
        parent_stat = path.parent.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_uid == 0
        and stat.S_IMODE(file_stat.st_mode) == _STAGED_REGISTRY_MODE
        and stat.S_ISDIR(parent_stat.st_mode)
        and parent_stat.st_uid == 0
        and not parent_stat.st_mode & 0o022
    )


def _is_trustworthy_file(path: Path) -> bool:
    """True iff no process reachable from inside this container could have
    written *path*.

    EXACT INVARIANT — the file must carry no group/other write bit AND satisfy
    at least one of:

      (a) it is owned by uid 0 (root installed it), or
      (b) its OWN mount is read-only (``statvfs`` ``ST_RDONLY`` — the
          ``:ro`` bind ``run-safent.sh`` creates) AND its owner is not the
          uid this process runs as.

    (b) exists because uid 0 is not portable across engines: rootless
    podman/docker remap the installing owner to 0 inside the container, but
    ROOTFUL podman does not remap at all, so the very same host file that
    ``provision.sh`` wrote as the owner arrives as uid 1000 and (a) alone
    would reject a perfectly good install (Safent then boots with no
    companion at all, FR-3, silently). (b) is not weaker: a read-only mount
    cannot be written through by ANY uid in this container — including root
    — and the owner-uid check keeps the guarantee even if that mount were
    ever remounted read-write. What both branches deny is identical: no
    unprivileged/compromised process in this container can plant or edit the
    file whose ``ip`` becomes a live nftables accept rule.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_mode & 0o022:
        return False
    if st.st_uid == 0:
        return True
    return _is_on_read_only_mount(path) and st.st_uid != os.geteuid()


def _is_on_read_only_mount(path: Path) -> bool:
    """True iff *path*'s own filesystem is mounted read-only (MS_RDONLY)."""
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _validate_companion_entry(raw: object) -> CompanionEndpoint:
    if not isinstance(raw, dict):
        raise CompanionConfigError("companion entry must be a JSON object")

    slug = str(raw.get("slug") or "")
    if slug not in _COMPANION_SLUGS:
        raise CompanionConfigError(f"unknown companion slug: {slug!r}")

    host = _validate_url(str(raw.get("url") or ""))
    ip = _validate_ip(str(raw.get("ip") or ""))
    port = raw.get("port")
    if port != _ALLOWED_PORT:
        raise CompanionConfigError(f"companion port must be {_ALLOWED_PORT}")

    ca_path = _validate_mounted_path(str(raw.get("ca_path") or ""))
    ca_fingerprint = _validate_ca_fingerprint(ca_path, str(raw.get("ca_fingerprint") or ""))
    bearer_ref = _validate_bearer_ref(str(raw.get("bearer_ref") or ""))

    return CompanionEndpoint(
        slug=slug, url=str(raw.get("url")), host=host, ip=ip, port=port,
        ca_path=str(ca_path), ca_fingerprint=ca_fingerprint, bearer_ref=bearer_ref,
    )


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != _ALLOWED_SCHEME:
        raise CompanionConfigError(f"companion url must use https:// (got {parsed.scheme!r})")
    host = parsed.hostname or ""
    if not host.endswith(_HOST_SUFFIX):
        raise CompanionConfigError(f"companion host must end in {_HOST_SUFFIX!r}: {host!r}")
    if parsed.port is not None and parsed.port != _ALLOWED_PORT:
        raise CompanionConfigError(f"companion url port must be {_ALLOWED_PORT}")
    return host


def _validate_ip(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise CompanionConfigError(f"companion ip is not a valid address: {ip!r}") from exc
    if addr not in _COMPANION_SUBNET:
        raise CompanionConfigError(f"companion ip {ip!r} outside {_COMPANION_SUBNET}")
    return ip


def _validate_mounted_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not _is_within_companion_mount(path):
        raise CompanionConfigError(f"path outside the companion mount: {raw_path!r}")
    return path


def _is_within_companion_mount(path: Path) -> bool:
    try:
        path.relative_to(_COMPANION_MOUNT_DIR)
    except ValueError:
        return False
    return True


def _validate_ca_fingerprint(ca_path: Path, claimed_fingerprint: str) -> str:
    """Recompute the SHA-256 of the DER at *ca_path* and cross-check it
    against *claimed_fingerprint* — the JSON value is a tamper check, never
    the root of trust (see module docstring)."""
    try:
        pem = ca_path.read_text(encoding="utf-8")
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, ValueError) as exc:
        raise CompanionConfigError(f"cannot read/parse CA at {ca_path}: {exc}") from exc
    actual = f"sha256:{hashlib.sha256(der).hexdigest()}"
    if actual != claimed_fingerprint:
        raise CompanionConfigError("ca_fingerprint does not match the CA on disk")
    return actual


def _validate_bearer_ref(bearer_ref: str) -> str:
    if not bearer_ref.startswith(_BEARER_REF_SCHEME):
        raise CompanionConfigError(f"bearer_ref must start with {_BEARER_REF_SCHEME!r}")
    _validate_mounted_path(Path(bearer_ref[len(_BEARER_REF_SCHEME):]))
    return bearer_ref
