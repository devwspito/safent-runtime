"""install_requests — the marker the UI leaves for the host to fulfil
(contracts/install-request.md, T006).

Extends the pre-existing marker mechanism (`.update-requested` /
`.uninstall-requested` in /var/lib/hermes/instance/, consumed by `safent
agent`) to a closed, five-verb vocabulary with per-verb TTL and structured
status readback.

Constitution Principle 0 condition (plan.md): this module has ZERO
governance/reasoning logic. It validates an enum and writes/reads a JSON
file — nothing else. Every decision about WHAT to do with a live request
(pull an image, run compose, recreate a container) is made by the host CLI
(`safent agent` / the desktop wrapper's embedded driver), never here.

Backward compatibility (data-model.md "expandir -> contraer"): POST
/api/v1/system/update and POST /api/v1/system/uninstall keep working
exactly as before AND additionally write the new-format marker, because
`safent agent` binaries already installed in the field only know how to
watch the OLD flat flag files — until a future session teaches the agent
the new format (T016), both are written together so neither path silently
regresses.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ParamSpec, TypeVar

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("hermes.shell_server.install_requests")

_INSTANCE_DIR = Path("/var/lib/hermes/instance")
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Closed vocabulary (install-request.md §1 / §3). `_COMPANION_SLUGS` is
# deliberately its OWN small constant here rather than importing
# companions.py's private `_COMPANION_SLUGS` — this endpoint validates its
# own input independently of that module's trust boundary. Keep both lists
# in sync if a companion is ever added.
VERBS: frozenset[str] = frozenset(
    {
        "install_companion",
        "repair_companion",
        "remove_companion",
        "update_system",
        "uninstall_system",
    }
)
_COMPANION_VERBS: frozenset[str] = frozenset(
    {"install_companion", "repair_companion", "remove_companion"}
)
_COMPANION_SLUGS: frozenset[str] = frozenset({"safent-ads"})
_DEFAULT_SLUG = "safent-ads"
_RETENTIONS: frozenset[str] = frozenset({"keep", "purge"})
_DEFAULT_RETENTION = "keep"

# Per-verb TTL (install-request.md §2) — replaces the old single
# _FLAG_STALE_S=15min, measured too short for a big download (UPD-03).
_TTL_S: dict[str, int] = {
    "install_companion": 30 * 60,
    "repair_companion": 15 * 60,
    "remove_companion": 5 * 60,
    "update_system": 45 * 60,
    "uninstall_system": 10 * 60,
}

# Legacy flat marker NAMES (system_update.py's original mechanism) — dual
# written ONLY for the two verbs an already-installed `safent agent` still
# watches (see module docstring). Resolved against _INSTANCE_DIR at call
# time (not a precomputed Path) so tests can retarget _INSTANCE_DIR.
_LEGACY_FLAG_NAME: dict[str, str] = {
    "update_system": ".update-requested",
    "uninstall_system": ".uninstall-requested",
}

# install-request.md §4: a claim younger than this is respected as live.
_CLAIM_TTL_S = 60
_ADS_WORK_VERBS = frozenset({"install_companion", "repair_companion"})
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _locked(fn: Callable[_P, _R]) -> Callable[_P, _R]:  # noqa: UP047
    @functools.wraps(fn)
    def run(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        _INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(_INSTANCE_DIR / ".install-requests.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fn(*args, **kwargs)
        finally:
            os.close(fd)

    return run


@dataclass(frozen=True)
class InstallRequestStatus:
    verb: str
    state: str  # 'pending' | 'claimed'
    expires_at: str
    last_failure: dict[str, object] | None = None


@dataclass(frozen=True)
class ClaimedRequest:
    """What `claim_request` hands back to a successful claimant (T016) — the
    fields a caller needs to actually DO the work, nothing else. `slug` is
    None for the two verbs that carry no slug (update_system/uninstall_system)."""

    verb: str
    slug: str | None
    request_id: str = ""


def _marker_path(verb: str) -> Path:
    return _INSTANCE_DIR / f"request-{verb}.json"


def _claim_path(verb: str) -> Path:
    return _INSTANCE_DIR / f"request-{verb}.claim"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime(_ISO_FORMAT)


def _read_marker(verb: str) -> dict[str, object] | None:
    try:
        with open(_marker_path(verb), encoding="utf-8") as fh:
            raw = fh.read(8193)
            if len(raw) > 8192:  # noqa: PLR2004
                return None
            parsed = json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_expired(marker: dict[str, object]) -> bool:
    expires_at = marker.get("expires_at")
    if not isinstance(expires_at, str):
        return True
    try:
        expiry = datetime.strptime(expires_at, _ISO_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return True
    return _now() >= expiry


def _delete_marker(verb: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(_marker_path(verb))


def _has_live_claim(verb: str) -> bool:
    try:
        age_s = time.time() - os.stat(_claim_path(verb)).st_mtime
    except OSError:
        return False
    return age_s < _CLAIM_TTL_S


def _claim_owned_by(verb: str, claimant: str) -> bool:
    try:
        return _claim_path(verb).read_text(encoding="utf-8") == claimant
    except OSError:
        return False


def _write_claim(verb: str, claimant: str) -> None:
    _INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _claim_path(verb).with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(claimant)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, _claim_path(verb))


def _status_from_marker(verb: str, marker: dict[str, object]) -> InstallRequestStatus:
    if verb in _ADS_WORK_VERBS and marker.get("state") == "claimed" and not _has_live_claim(verb):
        _fail_ads_marker(verb, marker, "host_interrupted")
    if marker.get("state") == "failed":
        failure = marker.get("last_failure")
        return InstallRequestStatus(
            verb=verb,
            state="failed",
            expires_at=str(marker.get("expires_at")),
            last_failure=failure if isinstance(failure, dict) else None,
        )
    state = "claimed" if _has_live_claim(verb) else "pending"
    return InstallRequestStatus(verb=verb, state=state, expires_at=str(marker.get("expires_at")))


def _write_marker_atomic(verb: str, document: dict[str, object]) -> None:
    _INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _marker_path(verb).with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(document, fh)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, _marker_path(verb))


def _write_legacy_flag(verb: str) -> None:
    legacy_name = _LEGACY_FLAG_NAME.get(verb)
    if legacy_name is None:
        return
    try:
        _INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_INSTANCE_DIR / legacy_name, "w", encoding="utf-8") as fh:
            fh.write("requested\n")
    except OSError as exc:
        logger.warning("hermes.install_requests.legacy_flag_write_failed verb=%s: %s", verb, exc)


@_locked
def is_verb_live(verb: str) -> bool:
    """True if `verb` has a pending/claimed request right now. Lazily
    expires a stale marker as a side effect (install-request.md §1 #4)."""
    marker = _read_marker(verb)
    if marker is None:
        return False
    if _is_expired(marker):
        _delete_marker(verb)
        logger.info("hermes.install_requests.expired verb=%s", verb)
        return False
    return _status_from_marker(verb, marker).state != "failed"


@_locked
def create_request(
    verb: str, *, slug: str | None = None, retention: str | None = None
) -> tuple[bool, InstallRequestStatus]:
    """Validate-free creation (the HTTP layer validates the enum before
    calling this). Returns (accepted, status) — accepted=False means a live
    request already existed and its status is returned unchanged
    (install-request.md §1 #5: the second press never starts a second
    install)."""
    existing = _read_marker(verb)
    if (
        existing is not None
        and not _is_expired(existing)
        and _status_from_marker(verb, existing).state != "failed"
    ):
        logger.info("hermes.install_requests.already_live verb=%s", verb)
        return False, _status_from_marker(verb, existing)

    created_at = _now()
    expires_at = created_at + timedelta(seconds=_TTL_S[verb])
    document: dict[str, object] = {
        "schema_version": 1,
        "verb": verb,
        "created_at": _iso(created_at),
        "expires_at": _iso(expires_at),
        "attempt": 1,
        "request_id": uuid.uuid4().hex,
    }
    if slug is not None:
        document["slug"] = slug
    if retention is not None:
        document["retention"] = retention

    _write_marker_atomic(verb, document)
    with contextlib.suppress(OSError):
        os.remove(_claim_path(verb))
    _write_legacy_flag(verb)
    logger.info("hermes.install_requests.created verb=%s slug=%s", verb, slug)
    return True, InstallRequestStatus(verb=verb, state="pending", expires_at=_iso(expires_at))


@_locked
def claim_request(  # noqa: PLR0911
    verb: str, *, claimant: str, allow_reclaim: bool = True
) -> ClaimedRequest | None:
    """Claim a live, unclaimed request for *verb* (install-request.md §4:
    mutually-exclusive claim between `safent agent` and the open app).

    Returns None when there is nothing live to claim, OR when someone
    else already holds a live claim (< _CLAIM_TTL_S old) — the caller must
    back off, never barge in. Re-claiming with the SAME *claimant* while
    already holding it just refreshes the timestamp (a long-running
    install renews its own claim instead of losing it mid-flight).

    Never consumes the marker itself — only `resolve_request` does that,
    once the work is actually done (T016 test: "fallo -> vuelve a pending,
    no bucle").
    """
    marker = _read_marker(verb)
    if marker is None:
        return None
    if _is_expired(marker):
        _delete_marker(verb)
        logger.info("hermes.install_requests.expired verb=%s", verb)
        return None
    if _has_live_claim(verb) and not _claim_owned_by(verb, claimant):
        return None
    if verb in _ADS_WORK_VERBS:
        if _status_from_marker(verb, marker).state == "failed":
            return None
        if marker.get("state") == "claimed" and not allow_reclaim:
            return None
        request_id = marker.get("request_id")
        if (
            type(marker.get("schema_version")) is not int
            or marker.get("schema_version") != 1
            or (
                request_id is not None
                and (
                    not isinstance(request_id, str)
                    or len(request_id) != 32  # noqa: PLR2004
                    or any(c not in "0123456789abcdef" for c in request_id)
                )
            )  # noqa: PLR2004
            or marker.get("slug") != _DEFAULT_SLUG
            or marker.get("verb") != verb
            or set(marker)
            - {
                "schema_version",
                "verb",
                "slug",
                "created_at",
                "expires_at",
                "attempt",
                "request_id",
                "state",
                "last_failure",
            }
        ):
            _fail_ads_marker(verb, marker, "invalid_request")
            return None
        for other in _ADS_WORK_VERBS - {verb}:
            if _has_live_claim(other):
                return None
        marker["state"] = "claimed"
        marker.setdefault("request_id", uuid.uuid4().hex)
        _write_marker_atomic(verb, marker)
    _write_claim(verb, claimant)
    logger.info("hermes.install_requests.claimed verb=%s claimant=%s", verb, claimant)
    slug = marker.get("slug")
    return ClaimedRequest(
        verb=verb,
        slug=slug if isinstance(slug, str) else None,
        request_id=str(marker.get("request_id", "")),
    )


def _fail_ads_marker(verb: str, marker: dict[str, object], code: str) -> None:
    marker["state"] = "failed"
    marker["expires_at"] = _iso(_now() + timedelta(hours=24))
    marker["last_failure"] = {
        "code": code,
        "label": "La operacion no termino. Revisa el estado y vuelve a solicitarla.",
        "retryable": True,
    }
    _write_marker_atomic(verb, marker)


@_locked
def renew_ads_request(verb: str, *, claimant: str, request_id: str) -> bool:
    marker = _read_marker(verb) if verb in _ADS_WORK_VERBS else None
    if not marker or marker.get("state") != "claimed" or _is_expired(marker):
        return False
    if (
        marker.get("request_id") != request_id
        or not _claim_owned_by(verb, claimant)
        or not _has_live_claim(verb)
    ):
        return False
    os.utime(_claim_path(verb), None)
    return True


@_locked
def resolve_ads_request(verb: str, *, claimant: str, request_id: str, success: bool) -> bool:
    marker = _read_marker(verb) if verb in _ADS_WORK_VERBS else None
    if not marker or marker.get("state") != "claimed" or marker.get("request_id") != request_id:
        return False
    if not _claim_owned_by(verb, claimant) or not _has_live_claim(verb) or _is_expired(marker):
        return False
    with contextlib.suppress(OSError):
        os.remove(_claim_path(verb))
    if success:
        _delete_marker(verb)
    else:
        _fail_ads_marker(verb, marker, "companion_install_failed")
    return True


@_locked
def reject_unsupported_companion_request() -> None:
    """The native consumer never executes removal or legacy system flags."""
    marker = _read_marker("remove_companion")
    if marker and marker.get("state") != "failed" and not _is_expired(marker):
        _fail_ads_marker("remove_companion", marker, "native_operation_unsupported")


@_locked
def resolve_request(verb: str, *, success: bool) -> None:
    """Release *verb*'s claim. On success ALSO consumes the marker (deleted
    before the caller acts is the contract's own invariant — this call
    happens AFTER, so this is the "yes, it truly finished" confirmation).
    On failure the marker survives: the request returns to `pending` for a
    future attempt (install-request.md: claimed -> failed(cause) ->
    pending), never re-executed in a tight loop because releasing the
    claim does not create a new expiry — the SAME `expires_at` still bounds
    how long it stays retryable."""
    if verb in _ADS_WORK_VERBS:
        raise ValueError("Ads resolution requires the exact claim and request identity")
    with contextlib.suppress(OSError):
        os.remove(_claim_path(verb))
    if success:
        _delete_marker(verb)
        logger.info("hermes.install_requests.applied verb=%s", verb)
    else:
        marker = _read_marker(verb)
        if verb in _ADS_WORK_VERBS and marker:
            _fail_ads_marker(verb, marker, "companion_install_failed")
        logger.info("hermes.install_requests.failed verb=%s", verb)


@_locked
def list_live_requests() -> list[InstallRequestStatus]:
    statuses: list[InstallRequestStatus] = []
    for verb in sorted(VERBS):
        marker = _read_marker(verb)
        if marker is None:
            continue
        if _is_expired(marker):
            _delete_marker(verb)
            logger.info("hermes.install_requests.expired verb=%s", verb)
            continue
        statuses.append(_status_from_marker(verb, marker))
    return statuses


def _status_payload(status: InstallRequestStatus) -> dict[str, object]:
    payload: dict[str, object] = {
        "verb": status.verb,
        "state": status.state,
        "expires_at": status.expires_at,
    }
    if status.last_failure is not None:
        payload["last_failure"] = status.last_failure
    return payload


def _validate_and_normalize(
    body: dict[str, object],
) -> tuple[str, str | None, str | None] | str:
    """Returns (verb, slug, retention) on success, or the 400 `code` string
    on failure. The ONLY logic this module contains beyond marker I/O
    (Constitution Principle 0 condition): enum membership, nothing else."""
    verb = body.get("verb")
    if not isinstance(verb, str) or verb not in VERBS:
        return "unknown_verb"
    assert isinstance(verb, str)  # narrowed by the membership check above

    slug: str | None = None
    if verb in _COMPANION_VERBS:
        raw_slug = body.get("slug")
        slug = raw_slug if isinstance(raw_slug, str) and raw_slug else _DEFAULT_SLUG
        if slug not in _COMPANION_SLUGS:
            return "unknown_slug"

    retention: str | None = None
    if verb == "remove_companion":
        raw_retention = body.get("retention")
        if isinstance(raw_retention, str) and raw_retention in _RETENTIONS:
            retention = raw_retention
        else:
            retention = _DEFAULT_RETENTION

    return verb, slug, retention


def create_install_requests_router() -> APIRouter:
    from hermes.shell_server.cowork.live_view_support import verify_token  # noqa: PLC0415

    router = APIRouter()

    def _auth(request: Request) -> None:
        expected = getattr(request.app.state, "shell_webui_token", "")
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth[:7].lower() == "bearer " else ""
        if not verify_token(tok, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @router.post("/api/v1/system/requests")
    async def post_install_request(request: Request) -> JSONResponse:
        _auth(request)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        result = _validate_and_normalize(body)
        if isinstance(result, str):
            logger.warning("hermes.install_requests.rejected code=%s", result)
            return JSONResponse(status_code=400, content={"accepted": False, "code": result})

        verb, slug, retention = result
        accepted, status = create_request(verb, slug=slug, retention=retention)
        content = {"accepted": accepted, "request": _status_payload(status)}
        return JSONResponse(status_code=200 if accepted else 409, content=content)

    @router.get("/api/v1/system/requests")
    async def get_install_requests(request: Request) -> dict[str, object]:
        _auth(request)
        return {"requests": [_status_payload(s) for s in list_live_requests()]}

    # Backward-compat aliases (install-request.md §3) — behaviour preserved
    # byte-for-byte for existing callers: always 200/accepted, no 409 at
    # this path (a second press stays idempotent via create_request itself).
    @router.post("/api/v1/system/update")
    async def legacy_update_alias(request: Request) -> dict[str, object]:
        _auth(request)
        create_request("update_system")
        return {"ok": True, "updating": True}

    @router.post("/api/v1/system/uninstall")
    async def legacy_uninstall_alias(request: Request) -> dict[str, object]:
        _auth(request)
        create_request("uninstall_system")
        return {"ok": True}

    return router
