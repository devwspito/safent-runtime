"""hermes.config_sync.remote_approvals — Enterprise remote-approval push/poll
loop (Fase 2 Phase 4b, RUNTIME/associate side).

Associate-initiated, pull-only/NAT: this module NEVER opens an inbound socket.
Both directions are outbound HTTPS calls the associate makes on its own
schedule, mirroring `config_sync/__main__.py`'s own transport hardening
(HTTPS-only endpoint validation, `follow_redirects=False`, response body-size
cap):

  PUSH  POST {cloud}/v1/approvals               (Bearer instance_secret)
  POLL  GET  {cloud}/v1/approvals/decisions?instance_id=...  (Bearer instance_secret)
  ACK   POST {cloud}/v1/approvals/ack            (Bearer instance_secret)

WIRE CONTRACT (2026-07 hardening — coordinate with the cloud side):
  - PUSH body gains `request_id` (str, fresh uuid4 minted PER PUSH occurrence)
    alongside the existing `proposal_id` (unchanged: the LOCAL, deterministic
    per-action id — see I-3 below, NEVER reused for the remote correlation).
    The cloud MUST track admin-decision state keyed by `request_id`, NOT
    `proposal_id` — otherwise a byte-identical repeated action can only ever
    be decided ONCE (see bug #2 below).
  - The signed decision envelope gains a 9th PINNED key, `request_id` (str) —
    see `_ENVELOPE_KEYS`. The cloud signer must include it before signing.
  - NEW `POST {cloud}/v1/approvals/ack` body: `{"request_ids": [<uuid>, ...]}`
    (batch). The cloud marks each acked so `GET .../decisions` (unacked_only)
    stops re-serving it — closes bug #1 (unbounded poll growth) below.

Bug fixed 2026-07 (adversarial review, PASS_WITH_NITS):
  #1 Decisions were never ACKed, so the cloud's unacked_only GET kept
     re-serving every resolved decision forever until the 2 MiB response cap
     started silently dropping ALL decisions. Fixed by `_ack_decisions`
     (batched per tick) + retention pruning of the local bookkeeping tables
     (`_prune_stale_remote_approval_state`).
  #2 `proposal_id` is uuid5(action_digest) — deterministic — so a REPEATED
     byte-identical enterprise action reused the SAME proposal_id, which the
     cloud had already resolved; the re-served (already-resolved) decision
     never delivered a FRESH one, so the second occurrence blocked until
     timeout. Fixed with `request_id`: a fresh, per-push-occurrence identity
     used ONLY for the remote round-trip. The LOCAL proposal_id scheme (and
     its concurrent-caller dedup via `_pending_events`) is untouched — see I-3.

HARD SECURITY INVARIANTS (see also security_hook.py / approval_router.py):
  I-1 The caged agent can NEVER self-approve via the cloud. The associate
      holds ONLY the tenant's Ed25519 PUBLIC key (verify-only, from the
      pairing record) — a decision is applied ONLY when its signature
      verifies against that key. There is no code path here that mints or
      accepts an unsigned/self-issued approval.
  I-2 The local human can ALWAYS deny — untouched by this module. A local
      DENY calls `signal_native_danger_approval` directly (dbus_runtime_
      service.reject_action); this module never overrides an already-resolved
      row (see `_resolve_pending_row`'s atomic `WHERE status='pending'` guard).
  I-3 This module NEVER substitutes the kernel floor. It only ever resumes the
      SAME `threading.Event` that `security_hook._resolve_native_danger_
      approval` already registered for a native-danger action that had ALREADY
      passed the hardline/self-jailbreak/denylist floor — it cannot invent a
      new approval seam nor skip that floor.

PINNED decision envelope (byte-identical to the cloud signer — do not deviate):
  keys (all str): action_digest, agent_id, approver_user_id, decided_at,
                  decision ("approve"|"deny"), instance_id, nonce, proposal_id,
                  request_id
  decision_signing_bytes = json.dumps(envelope, sort_keys=True,
      separators=(",", ":"), ensure_ascii=False).encode("utf-8")

Fail-closed verification matrix (`_verify_and_apply_decision`): a decision is
applied ONLY when ALL of the following hold — bad/tampered signature, a
mismatched instance_id (≠ this instance), an unknown/foreign `request_id`, a
`request_id` that is NOT the latest one pushed for its proposal (superseded by
a later occurrence — see bug #2), a `proposal_id` that disagrees with the one
`request_id` maps to locally, an unknown/already-resolved proposal_id, a
mismatched action_digest (≠ the pending row's own digest), or an already-seen
nonce (replay) — each independently abort with NO resume and NO DB mutation.

Idempotency: a `remote_approval_pushed` row (proposal_id, request_id,
pushed_at) records EVERY push occurrence (history, not upsert-in-place) so a
later decision can never be mis-applied to a newer occurrence that superseded
it — `_fetch_unpushed_enterprise_rows` still dedupes "already pushed for this
row version" via `MAX(pushed_at)` per proposal_id, and a revived row
(register_pending's delete+recreate on re-registration) gets a fresh
`created_at` and is therefore re-pushed with a fresh `request_id`. A
`remote_approval_decision_nonces` table is the anti-replay store for applied
decisions. Both tables are pruned by age (`_prune_stale_remote_approval_
state`, `_STATE_RETENTION_DAYS`) — never for a proposal that is still
'pending' locally.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

from hermes.config_sync.signature import verify_bundle
from hermes.instance.infrastructure.http_control_plane_client import (
    _validate_cloud_endpoint,
)
from hermes.instance.pairing_service import PairingError

if TYPE_CHECKING:
    from hermes.instance.association_store import SQLiteAssociationStore

logger = logging.getLogger("hermes.config_sync.remote_approvals")

_HTTP_TIMEOUT_S = 20.0
_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB — mirrors config_sync's policy fetch cap.
_PUBKEY_HEX_LEN = 64  # Ed25519 public key = 32 bytes = 64 hex chars.

# PINNED — the exact 9 keys the cloud signs. Order here is irrelevant (the
# canonical bytes are produced with sort_keys=True); this tuple only drives
# the defensive shape-check below. `request_id` (2026-07): the per-occurrence
# correlation id — see module docstring's WIRE CONTRACT / bug #2.
_ENVELOPE_KEYS: tuple[str, ...] = (
    "action_digest", "agent_id", "approver_user_id", "decided_at",
    "decision", "instance_id", "nonce", "proposal_id", "request_id",
)
_VALID_DECISIONS: frozenset[str] = frozenset({"approve", "deny"})

# Outcomes for which the decision is considered fully resolved from THIS
# instance's perspective and must be ACKed (bug #1) so the cloud's
# unacked_only GET stops re-serving it:
#   - applied:        the happy path — decision verified and applied.
#   - already_resolved: a re-served duplicate for a row we (or a local DENY)
#     already resolved — acking it is exactly what stops the infinite re-serve.
#   - replayed_nonce:  same duplicate, caught by the nonce store instead.
#   - stale_request:   the request_id belongs to an occurrence that has since
#     been SUPERSEDED by a fresher push (bug #2) — it can never be validly
#     applied to the new occurrence, so there is nothing to gain by re-serving
#     it either.
# Anything else (bad_signature, invalid_envelope, wrong_instance,
# unknown_request, request_proposal_mismatch, unknown_proposal,
# digest_mismatch) is NOT acked: these signal tampering/corruption/foreign
# data, and silently acking them would hide the problem instead of surfacing
# it via continued re-delivery.
_ACK_OUTCOMES: frozenset[str] = frozenset(
    {"applied", "already_resolved", "replayed_nonce", "stale_request"}
)

# Retention for the local bookkeeping tables below (LOW/1 fix — unbounded
# growth). A row past this age is pruned UNLESS its proposal_id is still
# 'pending' locally (an exceptionally slow admin decision must not lose its
# request_id mapping mid-flight).
_STATE_RETENTION_DAYS: int = 30

_STATE_DB_PATH = Path(
    os.environ.get(
        "HERMES_SHELL_DB",
        os.environ.get("HERMES_STATE_DB", "/var/lib/hermes/shell-state.db"),
    )
)

_DDL_REMOTE_APPROVAL_STATE = """
CREATE TABLE IF NOT EXISTS remote_approval_pushed (
    request_id  TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    pushed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS remote_approval_pushed_proposal_idx
    ON remote_approval_pushed(proposal_id);
CREATE TABLE IF NOT EXISTS remote_approval_decision_nonces (
    nonce   TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
"""


def decision_signing_bytes(envelope: dict[str, str]) -> bytes:
    """PINNED — byte-identical to the cloud signer. Do NOT deviate."""
    return json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# SQLite plumbing — shares shell-state.db with pending_approvals (capabilities
# BC); the two tracking tables here are config_sync's own bookkeeping, kept
# out of capabilities/infrastructure/schema.py (different bounded context).
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_remote_approval_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL_REMOTE_APPROVAL_STATE)


def _safe_json_object(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _safe_json_list(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw) if raw else []
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


# ---------------------------------------------------------------------------
# PUSH — associate-initiated, one HTTP POST per not-yet-pushed enterprise row.
# ---------------------------------------------------------------------------


def _fetch_unpushed_enterprise_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """route='enterprise' AND pending rows not yet pushed for THIS row version.

    A revived row (register_pending's delete+recreate on re-registration) gets
    a fresh created_at, so comparing pushed_at < created_at re-pushes it
    instead of silently skipping it forever (a plain proposal_id-seen check
    would NOT — proposal_id is deterministic and survives revival).

    `remote_approval_pushed` keeps ONE ROW PER PUSH OCCURRENCE (history, keyed
    by `request_id`, see bug #2) — this compares against the LATEST push per
    proposal_id (`MAX(pushed_at)`), never an older superseded occurrence.
    """
    return conn.execute(
        """
        SELECT pa.proposal_id, pa.agent_id, pa.tool_name, pa.action_digest,
               pa.risk, pa.sensitivity, pa.parameters_redacted, pa.created_at
        FROM pending_approvals pa
        LEFT JOIN (
            SELECT proposal_id, MAX(pushed_at) AS pushed_at
            FROM remote_approval_pushed
            GROUP BY proposal_id
        ) rp ON rp.proposal_id = pa.proposal_id
        WHERE pa.route = 'enterprise' AND pa.status = 'pending'
          AND (rp.proposal_id IS NULL OR rp.pushed_at < pa.created_at)
        ORDER BY pa.created_at ASC
        """
    ).fetchall()


def _build_push_body(row: sqlite3.Row, *, request_id: str) -> dict[str, Any]:
    """PINNED push body — params_redacted is already-redacted (register_pending
    ran `_redact_parameters` before persisting `parameters_redacted`); never
    the raw args, never any secret/key.

    `request_id` (2026-07, bug #2): a FRESH uuid4 minted for THIS push
    occurrence — distinct from `proposal_id` (the deterministic, LOCAL
    per-action id, sent unchanged for continuity/audit). The cloud must key
    admin-decision state off `request_id`, not `proposal_id` — see the module
    docstring's WIRE CONTRACT.
    """
    return {
        "proposal_id": row["proposal_id"],
        "request_id": request_id,
        "agent_id": row["agent_id"] or "",
        "tool_name": row["tool_name"] or "",
        "params_redacted": _safe_json_object(row["parameters_redacted"]),
        "action_digest": row["action_digest"] or "",
        "risk": row["risk"] or "",
        "sensitivity": _safe_json_list(row["sensitivity"]),
        "created_at": row["created_at"] or "",
    }


def _post_approval(*, cloud_endpoint: str, instance_secret: str, body: dict) -> bool:
    """PUSH one proposal. Never raises; False on ANY transport/HTTP failure
    (retried on the next tick — the 'pushed' marker is only written on success)."""
    try:
        resp = httpx.post(
            f"{cloud_endpoint.rstrip('/')}/v1/approvals",
            headers={"Authorization": f"Bearer {instance_secret}"},
            json=body,
            timeout=_HTTP_TIMEOUT_S,
            follow_redirects=False,  # SSRF mitigation — mirrors config_sync's fetch.
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "hermes.config_sync.remote_approvals.push_error", extra={"reason": str(exc)}
        )
        return False
    if resp.status_code not in (200, 201, 204):
        logger.warning(
            "hermes.config_sync.remote_approvals.push_http_error",
            extra={"status": resp.status_code},
        )
        return False
    return True


def _mark_pushed(
    conn: sqlite3.Connection, *, proposal_id: str, request_id: str, pushed_at: str
) -> None:
    """Records ONE push occurrence. INSERT-only (PK is `request_id`, fresh per
    call) — never overwrites an earlier occurrence's row, so a late decision
    for a superseded `request_id` can still be recognized as stale rather than
    silently vanishing (see `_is_latest_request_for_proposal`)."""
    conn.execute(
        "INSERT OR IGNORE INTO remote_approval_pushed "
        "(request_id, proposal_id, pushed_at) VALUES (?, ?, ?)",
        (request_id, proposal_id, pushed_at),
    )


def push_pending_enterprise_approvals(
    *, db_path: Path, cloud_endpoint: str, instance_secret: str,
) -> None:
    """PUSH every route='enterprise' pending row not yet pushed for its
    current version. Fail-soft per-row: one failed push never blocks the
    others and is retried on the next tick (no marker written on failure).

    Each attempt mints a fresh `request_id` (bug #2) — a failed POST simply
    discards it and a later retry mints another; only a SUCCESSFUL push
    persists its request_id, so the cloud never holds an orphaned request the
    associate has no memory of.
    """
    conn = _connect(db_path)
    try:
        _ensure_remote_approval_schema(conn)
        for row in _fetch_unpushed_enterprise_rows(conn):
            request_id = str(uuid4())
            body = _build_push_body(row, request_id=request_id)
            pushed = _post_approval(
                cloud_endpoint=cloud_endpoint, instance_secret=instance_secret, body=body
            )
            if pushed:
                _mark_pushed(
                    conn,
                    proposal_id=row["proposal_id"],
                    request_id=request_id,
                    pushed_at=row["created_at"],
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POLL — associate-initiated GET for signed decisions.
# ---------------------------------------------------------------------------


def _fetch_decisions(
    *, cloud_endpoint: str, instance_id: str, instance_secret: str
) -> list[dict]:
    """GET pending decisions. Never raises; [] on ANY transport/parse failure."""
    url = f"{cloud_endpoint.rstrip('/')}/v1/approvals/decisions?instance_id={instance_id}"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {instance_secret}"},
            timeout=_HTTP_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "hermes.config_sync.remote_approvals.poll_error", extra={"reason": str(exc)}
        )
        return []
    if resp.status_code != 200:
        return []

    content = resp.content
    if len(content) > _MAX_BODY_BYTES:
        logger.warning(
            "hermes.config_sync.remote_approvals.poll_body_too_large",
            extra={"size": len(content)},
        )
        return []
    try:
        data = json.loads(content)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "hermes.config_sync.remote_approvals.poll_parse_error",
            extra={"reason": str(exc)},
        )
        return []

    decisions = data.get("decisions") if isinstance(data, dict) else None
    return decisions if isinstance(decisions, list) else []


def _extract_envelope(item: Any) -> dict[str, str] | None:
    """Shape-check ONE decision item into the 9-key str envelope, or None.

    Fail-closed: a missing/empty/wrong-typed field, or a `decision` outside
    {"approve","deny"}, makes the WHOLE item unusable — never partially trusted.
    """
    if not isinstance(item, dict):
        return None
    envelope: dict[str, str] = {}
    for key in _ENVELOPE_KEYS:
        value = item.get(key)
        if not isinstance(value, str) or not value:
            return None
        envelope[key] = value
    if envelope["decision"] not in _VALID_DECISIONS:
        return None
    return envelope


def _nonce_seen_or_mark(conn: sqlite3.Connection, nonce: str) -> bool:
    """Atomically marks *nonce* seen. True iff this is the FIRST time (safe to
    proceed); False if already seen — a replay, fail-closed."""
    now = datetime.now(tz=UTC).isoformat()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO remote_approval_decision_nonces (nonce, seen_at) "
        "VALUES (?, ?)",
        (nonce, now),
    )
    return cursor.rowcount == 1


def _fetch_pending_row(conn: sqlite3.Connection, proposal_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT proposal_id, status, action_digest FROM pending_approvals "
        "WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()


def _fetch_proposal_id_for_request(conn: sqlite3.Connection, request_id: str) -> str | None:
    """Resolve a decision's `request_id` back to the LOCAL `proposal_id` it was
    pushed for (bug #2). None when this associate never pushed this
    request_id (foreign/expired-and-pruned/bogus data) — fail-closed."""
    row = conn.execute(
        "SELECT proposal_id FROM remote_approval_pushed WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    return row["proposal_id"] if row is not None else None


def _is_latest_request_for_proposal(
    conn: sqlite3.Connection, *, request_id: str, proposal_id: str
) -> bool:
    """True iff *request_id* is the MOST RECENT push occurrence for
    *proposal_id*. A repeated identical action mints a fresh request_id per
    occurrence (bug #2) — a decision that arrives for an OLDER, superseded
    occurrence must never resolve the newer one's still-pending row."""
    row = conn.execute(
        "SELECT request_id FROM remote_approval_pushed WHERE proposal_id = ? "
        "ORDER BY pushed_at DESC LIMIT 1",
        (proposal_id,),
    ).fetchone()
    return row is not None and row["request_id"] == request_id


def _resolve_pending_row(conn: sqlite3.Connection, proposal_id: str, new_status: str) -> bool:
    """Atomically flips a still-pending row to *new_status*.

    True iff THIS call performed the transition. False means the row was
    resolved by someone else in the meantime (e.g. a local DENY that raced
    ahead) — I-2's local-deny priority is preserved: whichever resolution
    reaches the DB first wins, and a late decision for an already-resolved
    row is a no-op (never resurrects/overrides it).
    """
    now = datetime.now(tz=UTC).isoformat()
    cursor = conn.execute(
        "UPDATE pending_approvals SET status = ?, resolved_at = ? "
        "WHERE proposal_id = ? AND status = 'pending'",
        (new_status, now, proposal_id),
    )
    return cursor.rowcount == 1


def _verify_and_apply_decision(
    *, item: Any, pubkey_hex: str, own_instance_id: str, conn: sqlite3.Connection,
) -> str:
    """Verify ONE decision fail-closed; apply it iff every check passes.

    Returns an outcome code for observability (never raises):
    invalid_envelope | bad_signature | wrong_instance | unknown_request |
    request_proposal_mismatch | stale_request | unknown_proposal |
    digest_mismatch | already_resolved | replayed_nonce | applied.
    See `_ACK_OUTCOMES` for which of these are ACKed to the cloud.
    """
    envelope = _extract_envelope(item)
    if envelope is None:
        return "invalid_envelope"
    signature_hex = item.get("signature_hex") if isinstance(item, dict) else None
    if not isinstance(signature_hex, str) or not signature_hex:
        return "invalid_envelope"

    # P0-2 style (mirrors config_sync's policy verify): the signature is
    # checked FIRST — no envelope field is trusted before this passes (I-1).
    payload = decision_signing_bytes(envelope)
    if not verify_bundle(
        payload_canonical=payload, signature_hex=signature_hex, pubkey_hex=pubkey_hex
    ):
        return "bad_signature"

    if envelope["instance_id"] != own_instance_id:
        return "wrong_instance"

    # Bug #2: correlate by the per-occurrence request_id, NOT the deterministic
    # proposal_id embedded in the envelope — the envelope's own proposal_id is
    # only cross-checked below (defense in depth), never trusted for lookup.
    proposal_id = _fetch_proposal_id_for_request(conn, envelope["request_id"])
    if proposal_id is None:
        return "unknown_request"
    if proposal_id != envelope["proposal_id"]:
        return "request_proposal_mismatch"
    if not _is_latest_request_for_proposal(
        conn, request_id=envelope["request_id"], proposal_id=proposal_id
    ):
        # A later occurrence of the SAME action already superseded this
        # request — applying it now would resolve the WRONG (newer) pending
        # row. ACKed anyway (see _ACK_OUTCOMES): it can never validly apply.
        return "stale_request"

    row = _fetch_pending_row(conn, proposal_id)
    if row is None:
        return "unknown_proposal"
    if (row["action_digest"] or "") != envelope["action_digest"]:
        return "digest_mismatch"
    if row["status"] != "pending":
        return "already_resolved"
    if not _nonce_seen_or_mark(conn, envelope["nonce"]):
        return "replayed_nonce"

    new_status = "approved" if envelope["decision"] == "approve" else "rejected"
    if not _resolve_pending_row(conn, proposal_id, new_status):
        return "already_resolved"

    from hermes.runtime.security_hook import signal_native_danger_approval  # noqa: PLC0415

    choice = "approved" if envelope["decision"] == "approve" else "denied"
    signal_native_danger_approval(proposal_id, choice)
    return "applied"


def _prune_stale_remote_approval_state(conn: sqlite3.Connection) -> None:
    """Bounds `remote_approval_pushed` / `_decision_nonces` growth (LOW/1 fix).

    Deletes rows older than `_STATE_RETENTION_DAYS`. `remote_approval_pushed`
    rows tied to a proposal that is STILL 'pending' locally are kept
    regardless of age — an exceptionally slow admin decision must not lose its
    request_id mapping mid-flight. Nonces are pure anti-replay history and are
    pruned by age alone: a decision this old could never resolve a still-
    pending row (bounded by `_NATIVE_DANGER_OWNER_WAIT_S` / the durable
    breaker), so forgetting the nonce cannot resurrect anything.
    """
    cutoff = (datetime.now(tz=UTC) - timedelta(days=_STATE_RETENTION_DAYS)).isoformat()
    conn.execute(
        "DELETE FROM remote_approval_decision_nonces WHERE seen_at < ?", (cutoff,)
    )
    conn.execute(
        """
        DELETE FROM remote_approval_pushed
         WHERE pushed_at < ?
           AND proposal_id NOT IN (
               SELECT proposal_id FROM pending_approvals WHERE status = 'pending'
           )
        """,
        (cutoff,),
    )


def _ack_decisions(
    *, cloud_endpoint: str, instance_secret: str, request_ids: list[str]
) -> None:
    """POST the batch of resolved request_ids so the cloud's unacked_only GET
    stops re-serving them (LOW/1 fix — the root cause of the unbounded-growth
    bug). Never raises; a failed ack is safely retried next tick — an
    un-acked, already-applied decision is a pure no-op on re-delivery (the
    status/nonce guards in `_verify_and_apply_decision` make re-application
    idempotent), it just keeps costing bytes until acked."""
    if not request_ids:
        return
    try:
        resp = httpx.post(
            f"{cloud_endpoint.rstrip('/')}/v1/approvals/ack",
            headers={"Authorization": f"Bearer {instance_secret}"},
            json={"request_ids": request_ids},
            timeout=_HTTP_TIMEOUT_S,
            follow_redirects=False,  # SSRF mitigation — mirrors config_sync's fetch.
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "hermes.config_sync.remote_approvals.ack_error", extra={"reason": str(exc)}
        )
        return
    if resp.status_code not in (200, 204):
        logger.warning(
            "hermes.config_sync.remote_approvals.ack_http_error",
            extra={"status": resp.status_code},
        )


def poll_and_apply_decisions(
    *,
    db_path: Path,
    cloud_endpoint: str,
    instance_id: str,
    instance_secret: str,
    pubkey_hex: str,
) -> None:
    """POLL decisions and verify+apply each fail-closed (see module docstring
    for the exact matrix), then ACK every fully-resolved outcome (`_ACK_
    OUTCOMES`) in ONE batched request so the cloud stops re-serving it (LOW/1
    fix). One malformed/malicious item never affects another. Pruning runs
    every tick regardless of whether any decisions were returned."""
    items = _fetch_decisions(
        cloud_endpoint=cloud_endpoint, instance_id=instance_id, instance_secret=instance_secret
    )

    to_ack: list[str] = []
    conn = _connect(db_path)
    try:
        _ensure_remote_approval_schema(conn)
        _prune_stale_remote_approval_state(conn)
        for item in items:
            outcome = _verify_and_apply_decision(
                item=item, pubkey_hex=pubkey_hex, own_instance_id=instance_id, conn=conn,
            )
            proposal_id = item.get("proposal_id") if isinstance(item, dict) else None
            logger.info(
                "hermes.config_sync.remote_approvals.decision_outcome=%s",
                outcome,
                extra={"proposal_id": proposal_id, "outcome": outcome},
            )
            if outcome in _ACK_OUTCOMES and isinstance(item, dict):
                request_id = item.get("request_id")
                if isinstance(request_id, str) and request_id:
                    to_ack.append(request_id)
    finally:
        conn.close()

    if to_ack:
        _ack_decisions(
            cloud_endpoint=cloud_endpoint, instance_secret=instance_secret,
            request_ids=to_ack,
        )


# ---------------------------------------------------------------------------
# Orchestration — one tick: PUSH then POLL, both fail-soft.
# ---------------------------------------------------------------------------


def _endpoint_is_safe(endpoint: str) -> bool:
    try:
        _validate_cloud_endpoint(endpoint)
        return True
    except PairingError as exc:
        logger.error(
            "hermes.config_sync.remote_approvals.endpoint_unsafe", extra={"reason": str(exc)}
        )
        return False


def _pubkey_is_valid(pubkey_hex: str) -> bool:
    if not pubkey_hex or len(pubkey_hex) != _PUBKEY_HEX_LEN:
        return False
    try:
        bytes.fromhex(pubkey_hex)
        return True
    except ValueError:
        return False


def run_remote_approvals_once(
    *, store: "SQLiteAssociationStore", db_path: Path | None = None
) -> None:
    """One associate-initiated tick: PUSH pending enterprise rows, then POLL +
    apply signed decisions. Fail-soft end-to-end — never raises into the
    caller's loop (mirrors config_sync's own per-tick isolation).

    Pull-only/NAT: this ONLY ever opens outbound HTTPS connections; it never
    listens on a socket.
    """
    try:
        assoc = store.get()
        if assoc is None or not store.is_associated():
            return
        if not _endpoint_is_safe(assoc.cloud_endpoint):
            return
        if not _pubkey_is_valid(assoc.signing_pubkey_hex):
            return
        instance_secret = store.reveal_instance_secret()
        if not instance_secret:
            return
    except Exception as exc:  # noqa: BLE001 — fail-soft: never raise into the caller's loop
        logger.error(
            "hermes.config_sync.remote_approvals.setup_failed",
            extra={"reason": str(exc)},
            exc_info=True,
        )
        return

    resolved_db_path = db_path or _STATE_DB_PATH

    try:
        push_pending_enterprise_approvals(
            db_path=resolved_db_path,
            cloud_endpoint=assoc.cloud_endpoint,
            instance_secret=instance_secret,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.config_sync.remote_approvals.push_failed",
            extra={"reason": str(exc)},
            exc_info=True,
        )

    try:
        poll_and_apply_decisions(
            db_path=resolved_db_path,
            cloud_endpoint=assoc.cloud_endpoint,
            instance_id=assoc.instance_id,
            instance_secret=instance_secret,
            pubkey_hex=assoc.signing_pubkey_hex,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.config_sync.remote_approvals.poll_failed",
            extra={"reason": str(exc)},
            exc_info=True,
        )
