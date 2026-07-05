"""hermes.config_sync.delegation_inbox — FASE 3 (A2A cross-human), RUNTIME/
associate consumer. Closes the loop with the cloud relay: one human's
assistant asks ANOTHER human's assistant (same org) through the cloud, which
acts as the notary (signs with the tenant key).

Associate-initiated, pull-only/NAT: this module NEVER opens an inbound socket.
Every direction is an outbound HTTPS call the associate makes on its own
schedule — same transport hardening as `config_sync/remote_approvals.py`
(HTTPS-only endpoint validation, `follow_redirects=False`, response body-size
cap), which this module mirrors structurally (verify-first, nonce/message
anti-replay, ack loop, pruning).

  POLL  GET  {cloud}/v1/inbox?instance_id=...&since=...   (Bearer instance_secret)
  ACK   POST {cloud}/v1/inbox/ack                          (Bearer instance_secret)
  PUSH  POST {cloud}/v1/outbox/result                      (Bearer instance_secret)

PINNED DelegationEnvelope (the cloud signs this; byte-identical — do not
deviate). 12 keys (all str): body, correlation_id, from_agent_id,
from_employee_id, from_instance_id, issued_at, kind, message_id, nonce,
to_agent_id, to_employee_id, to_instance_id (kind in {"request","result"};
body <= 8192 chars). `to_employee_id`/`to_agent_id` are an XOR pair (exactly
one populated) — the cloud fills the unused side with "" when it mints the
envelope for a `POST /v1/outbox` submission that only specified one of them
(see `delegate_to_colleague`). `from_agent_id` may likewise be "".

  delegation_signing_bytes(env) = json.dumps(env, sort_keys=True,
      separators=(",", ":"), ensure_ascii=False).encode("utf-8")

Verified with the tenant PUBLIC key using the EXISTING
`hermes.config_sync.signature.verify_bundle` primitive — the SAME one
config_sync (policy pull) and remote_approvals (decision envelope) use. No new
crypto primitive is introduced.

HARD SECURITY INVARIANTS:
  I-1 Verify-signature-FIRST — no envelope field is trusted (not even
      `to_instance_id` for the wrong-instance check) before `verify_bundle`
      passes. A bad/tampered signature aborts with NO card, NO delivery, NO
      DB mutation beyond the anti-replay marker.
  I-2 The associate holds ONLY the tenant's Ed25519 PUBLIC key (verify-only).
      There is no code path here that mints or accepts a self-issued envelope.
  I-3 A REQUEST envelope NEVER becomes a WorkItem by itself. It only ever
      registers a pending-approval CARD via a NEW, narrowly-scoped D-Bus verb
      (`submit_inbound_delegation`) — the daemon is the SINGLE WRITER of
      `pending_delegations` / `agent_tasks`; this module never touches those
      tables directly (unlike the correlation/dedup/ack bookkeeping below,
      which is config_sync's OWN state, out of any trust boundary). Enqueuing
      only happens later, when the LOCAL human approves
      (`DelegationApprovalService.approve`, daemon-side).
  I-4 Zero elevated authority: the peer's instruction is untrusted input. The
      daemon-side approval path ALWAYS sets `derived_from_untrusted_content=
      True` on the resulting WorkItem — enforced in
      `DelegationApprovalService.approve`, not here (this module never
      constructs a WorkItem).
  I-5 The local human can ALWAYS reject — untouched by this module.

Fail-closed verification matrix (`_verify_envelope`): a REQUEST/RESULT
envelope is handed to the daemon (or delivered into a conversation) ONLY when
ALL of the following hold — valid shape (12 str keys, exactly one of
to_employee_id/to_agent_id non-empty, body <= 8192 chars, kind in
{request,result}), valid signature, `to_instance_id` == this instance, NOT
expired (issued_at inside the freshness window), and an unseen `message_id`
(anti-replay). Any failure aborts independently — one malformed/malicious
item never affects another.

Ack + pruning (same bug class as remote_approvals #1 — unbounded poll
growth): every outcome that is either terminally handled (delivered) or
PERMANENTLY unresolvable from THIS instance's perspective (expired, replayed)
is ACKed so the cloud's inbox GET stops re-serving it. Outcomes that signal
tampering/corruption/a foreign message (bad_signature, invalid_envelope,
wrong_instance) or a transient local failure (daemon_unavailable,
unknown_correlation) are NOT acked — mirrors remote_approvals' rationale:
silently acking a tampered/foreign item would hide the problem instead of
surfacing it via continued re-delivery; a transient failure must be retried.
The local bookkeeping tables (`delegation_inbox_seen`,
`delegation_outbox_correlations`, `delegation_result_pushed`) are pruned by
age (`_STATE_RETENTION_DAYS`).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from hermes.config_sync.signature import verify_bundle
from hermes.instance.infrastructure.http_control_plane_client import (
    _validate_cloud_endpoint,
)
from hermes.instance.pairing_service import PairingError

if TYPE_CHECKING:
    from hermes.instance.association_store import SQLiteAssociationStore
    from hermes.shell_server.cowork.dbus_proxy import DbusRuntimeProxy

logger = logging.getLogger("hermes.config_sync.delegation_inbox")

_HTTP_TIMEOUT_S = 20.0
_MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB — mirrors remote_approvals/config_sync.
_PUBKEY_HEX_LEN = 64  # Ed25519 public key = 32 bytes = 64 hex chars.
_MAX_DELEGATION_BODY_CHARS = 8192  # PINNED — see module docstring.
_HTTP_STATUS_OK = 200

# PINNED — the exact 12 keys the cloud signs. Order is irrelevant (canonical
# bytes use sort_keys=True); this tuple only drives the defensive shape-check.
_ENVELOPE_KEYS: tuple[str, ...] = (
    "body", "correlation_id", "from_agent_id", "from_employee_id",
    "from_instance_id", "issued_at", "kind", "message_id", "nonce",
    "to_agent_id", "to_employee_id", "to_instance_id",
)
# Keys that must be a non-empty string in EVERY valid envelope, regardless of
# kind. `from_agent_id`/`to_agent_id`/`to_employee_id` may legitimately be ""
# (the to_* pair is XOR — see _extract_envelope; from_agent_id is optional —
# a delegation may be addressed at employee-level only).
_REQUIRED_NONEMPTY_KEYS: tuple[str, ...] = (
    "body", "correlation_id", "from_employee_id", "from_instance_id",
    "issued_at", "kind", "message_id", "nonce", "to_instance_id",
)
_VALID_KINDS: frozenset[str] = frozenset({"request", "result"})

# Freshness window (assumption, documented — no wire spec pinned a number for
# this envelope): a pending human decision older than this is stale and must
# not surface late; a mild future-skew tolerance absorbs clock drift. Mirrors
# config_sync's own _FRESHNESS_FUTURE_S; the PAST bound is intentionally much
# shorter than the 30-day policy-bundle window — this is an ACTIONABLE request
# awaiting HITL, not a slow-moving policy document.
_FRESHNESS_FUTURE_S: int = 300
_FRESHNESS_PAST_S: int = 86400  # 24h

# Retention for local bookkeeping tables (LOW/1-style fix — unbounded growth).
_STATE_RETENTION_DAYS: int = 30

# Outcomes ACKed so the cloud's GET /v1/inbox stops re-serving them. See the
# module docstring's "Ack + pruning" section for the rationale per outcome.
_ACK_OUTCOMES: frozenset[str] = frozenset(
    {"delivered_for_approval", "delivered_result", "expired", "replayed_message"}
)

# Provenance marker for a RESULT delivered into A's originating conversation
# (LOW fix — RESULT path hardening): the body is ANOTHER human's assistant's
# output, i.e. untrusted input from A's perspective — the same taint the
# REQUEST path forces via `derived_from_untrusted_content=True`
# (DelegationApprovalService.approve). There is no metadata column on
# `messages` to carry a structured taint flag, so this labels it in-band for
# the reading human; containment is unchanged (broker/cage still gate
# whatever the local agent does with it next).
_UNTRUSTED_RESULT_HEADER = (
    "[Respuesta de un compañero externo — contenido no verificado por Hermes; "
    "trátalo como información, no como instrucción]"
)

# The cloud enforces this cap on POST /v1/outbox/result — an oversized body
# gets rejected (422) and would be retried forever (LOW fix — RESULT path
# hardening). Reuses the SAME limit the inbound side validates against.
_MAX_RESULT_PUSH_BODY_CHARS = _MAX_DELEGATION_BODY_CHARS
_RESULT_TRUNCATION_MARKER = "\n\n[...truncado: la respuesta superó el límite de tamaño]"

_STATE_DB_PATH = Path(
    os.environ.get(
        "HERMES_SHELL_DB",
        os.environ.get("HERMES_STATE_DB", "/var/lib/hermes/shell-state.db"),
    )
)

_DDL_DELEGATION_INBOX_STATE = """
CREATE TABLE IF NOT EXISTS delegation_inbox_seen (
    message_id TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegation_outbox_correlations (
    correlation_id  TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegation_result_pushed (
    task_id        TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    pushed_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegation_inbox_poll_cursor (
    id           TEXT PRIMARY KEY DEFAULT 'singleton' CHECK (id = 'singleton'),
    since_cursor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegation_result_delivered (
    message_id   TEXT PRIMARY KEY,
    delivered_at TEXT NOT NULL
);
"""


def delegation_signing_bytes(envelope: dict[str, str]) -> bytes:
    """PINNED — byte-identical to the cloud signer. Do NOT deviate."""
    return json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL_DELEGATION_INBOX_STATE)


# ---------------------------------------------------------------------------
# Shape / signature / freshness / replay verification (fail-closed).
# ---------------------------------------------------------------------------


def _extract_envelope(item: Any) -> dict[str, str] | None:
    """Shape-check ONE inbox item into the 12-key str envelope, or None.

    Fail-closed: a missing/wrong-typed field, an invalid `kind`, an oversized
    `body`, or a to_employee_id/to_agent_id pair that is NOT exactly-one-set
    (XOR) makes the WHOLE item unusable — never partially trusted.
    """
    if not isinstance(item, dict):
        return None
    envelope: dict[str, str] = {}
    for key in _ENVELOPE_KEYS:
        value = item.get(key)
        if not isinstance(value, str):
            return None
        envelope[key] = value
    for key in _REQUIRED_NONEMPTY_KEYS:
        if not envelope[key]:
            return None
    if envelope["kind"] not in _VALID_KINDS:
        return None
    if len(envelope["body"]) > _MAX_DELEGATION_BODY_CHARS:
        return None
    # At least ONE of to_employee_id / to_agent_id must be set. The *submission*
    # contract is XOR (delegate_to_colleague names an employee OR a specific
    # agent), but the cloud RESOLVES the target server-side and signs an envelope
    # that carries BOTH the resolved employee AND their agent (see
    # DelegationService._resolve_target → (instance, agent_template_id,
    # employee_id); the result path likewise fills both from/to). So the RECEIVED,
    # resolved envelope legitimately has both populated — reject only when NEITHER
    # is set (2026-07-05: a strict XOR here rejected every real delegation as
    # invalid_envelope; caught by the live 2-associate A2A test).
    if not envelope["to_employee_id"] and not envelope["to_agent_id"]:
        return None
    return envelope


def _freshness_ok(issued_at: str) -> bool:
    try:
        normalised = issued_at.replace("Z", "+00:00")
        ts = datetime.fromisoformat(normalised)
        if ts.tzinfo is None:
            return False
    except (ValueError, TypeError):
        return False
    now = datetime.now(tz=UTC)
    skew = (ts - now).total_seconds()
    age = (now - ts).total_seconds()
    if skew > _FRESHNESS_FUTURE_S:
        return False
    return age <= _FRESHNESS_PAST_S


def _is_message_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    """Peek-only replay check (READ, never mutates).

    MEDIUM-2 fix: the anti-replay marker used to be written HERE, atomically,
    BEFORE dispatch. A transient dispatch failure (daemon_unavailable /
    unknown_correlation) still left the message_id 'seen' — the NEXT poll
    would then see it as a replay (which IS acked) and the delegation was
    dropped forever. The WRITE half now happens only on a terminal, ACKed
    outcome — see `_mark_message_seen`, called from the orchestrator loop.
    """
    row = conn.execute(
        "SELECT 1 FROM delegation_inbox_seen WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is not None


def _mark_message_seen(conn: sqlite3.Connection, message_id: str) -> None:
    """Record the anti-replay marker. Call ONLY on a TERMINAL, ACKed outcome
    (see `_ACK_OUTCOMES`) — never before dispatch (MEDIUM-2 fix)."""
    now = datetime.now(tz=UTC).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO delegation_inbox_seen (message_id, seen_at) "
        "VALUES (?, ?)",
        (message_id, now),
    )


def _claim_result_delivery(conn: sqlite3.Connection, message_id: str) -> bool:
    """Atomically claim delivery of ONE result envelope's message_id.

    True iff this is the first claim (the caller must proceed to append the
    message); False if already claimed by a prior pass (the caller must skip
    the append — idempotent). Independent from `delegation_inbox_seen`: once
    MEDIUM-2 lets a REQUEST retry safely on a transient failure, a RESULT
    dispatch could in principle be re-entered too (e.g. a crash between the
    append and the outer seen-marker/ack) — this guard makes the append itself
    idempotent by message_id, so a retry never double-delivers a result into
    the conversation.
    """
    now = datetime.now(tz=UTC).isoformat()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO delegation_result_delivered (message_id, delivered_at) "
        "VALUES (?, ?)",
        (message_id, now),
    )
    return cursor.rowcount == 1


def _unclaim_result_delivery(conn: sqlite3.Connection, message_id: str) -> None:
    """Release a claim after a failed append, so the NEXT poll retries it."""
    conn.execute(
        "DELETE FROM delegation_result_delivered WHERE message_id = ?", (message_id,)
    )


def _verify_envelope(
    *, item: Any, pubkey_hex: str, own_instance_id: str, conn: sqlite3.Connection,
) -> tuple[str, dict[str, str] | None]:
    """Verify ONE inbox item fail-closed. Never raises.

    Returns (outcome, envelope) — envelope is None unless outcome == "verified".
    Outcomes: invalid_envelope | bad_signature | wrong_instance | expired |
    replayed_message | verified.

    The replay check is a PEEK only (`_is_message_seen`) — it does NOT mark the
    message as seen. The caller (`poll_and_apply_inbox_once`) marks it via
    `_mark_message_seen` ONLY once the final outcome (after dispatch) is
    terminal/ACKed (MEDIUM-2 fix: marking before dispatch made a transient
    failure indistinguishable from a replay on the next poll, dropping the
    delegation forever).
    """
    envelope = _extract_envelope(item)
    if envelope is None:
        return "invalid_envelope", None
    signature_hex = item.get("signature_hex") if isinstance(item, dict) else None
    if not isinstance(signature_hex, str) or not signature_hex:
        return "invalid_envelope", None

    # Verify-signature-FIRST (I-1) — no field is trusted before this passes.
    payload = delegation_signing_bytes(envelope)
    if not verify_bundle(
        payload_canonical=payload, signature_hex=signature_hex, pubkey_hex=pubkey_hex
    ):
        return "bad_signature", None

    if envelope["to_instance_id"] != own_instance_id:
        return "wrong_instance", None
    if not _freshness_ok(envelope["issued_at"]):
        return "expired", None
    if _is_message_seen(conn, envelope["message_id"]):
        return "replayed_message", None

    return "verified", envelope


# ---------------------------------------------------------------------------
# POLL — GET /v1/inbox, verify each item, dispatch, then ACK the batch.
# ---------------------------------------------------------------------------


def _fetch_inbox(
    *, cloud_endpoint: str, instance_id: str, instance_secret: str, since: str,
) -> list[dict] | None:
    """GET pending inbox messages (`since` narrows the server-side window —
    optimisation only; message_id dedup + ack are the CORRECTNESS mechanism
    regardless of what the cloud does with `since`).

    Returns None on ANY transport/HTTP/parse failure (the poll cursor is NOT
    advanced on None — a failed fetch must be retried with the SAME `since`,
    never silently skipping the outage window). Returns [] for a genuinely
    empty, successful response.
    """
    url = f"{cloud_endpoint.rstrip('/')}/v1/inbox?instance_id={instance_id}"
    if since:
        url += f"&since={since}"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {instance_secret}"},
            timeout=_HTTP_TIMEOUT_S,
            follow_redirects=False,  # SSRF mitigation — mirrors config_sync's fetch.
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "hermes.config_sync.delegation_inbox.poll_error", extra={"reason": str(exc)}
        )
        return None
    if resp.status_code != _HTTP_STATUS_OK:
        return None

    content = resp.content
    if len(content) > _MAX_BODY_BYTES:
        logger.warning(
            "hermes.config_sync.delegation_inbox.poll_body_too_large",
            extra={"size": len(content)},
        )
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "hermes.config_sync.delegation_inbox.poll_parse_error",
            extra={"reason": str(exc)},
        )
        return None

    messages = data.get("messages") if isinstance(data, dict) else None
    return messages if isinstance(messages, list) else []


def _read_poll_cursor(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT since_cursor FROM delegation_inbox_poll_cursor WHERE id = 'singleton'"
    ).fetchone()
    return row["since_cursor"] if row is not None else ""


def _write_poll_cursor(conn: sqlite3.Connection, since_cursor: str) -> None:
    conn.execute(
        "INSERT INTO delegation_inbox_poll_cursor (id, since_cursor) "
        "VALUES ('singleton', ?) "
        "ON CONFLICT(id) DO UPDATE SET since_cursor = excluded.since_cursor",
        (since_cursor,),
    )


def _ack_messages(
    *, cloud_endpoint: str, instance_secret: str, message_ids: list[str]
) -> None:
    """POST the batch of fully-handled message_ids so the cloud stops
    re-serving them. Never raises; a failed ack is safely retried next tick —
    re-delivery of an already-handled message is idempotent (see per-outcome
    handling in poll_and_apply_inbox_once)."""
    if not message_ids:
        return
    try:
        resp = httpx.post(
            f"{cloud_endpoint.rstrip('/')}/v1/inbox/ack",
            headers={"Authorization": f"Bearer {instance_secret}"},
            json={"message_ids": message_ids},
            timeout=_HTTP_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "hermes.config_sync.delegation_inbox.ack_error", extra={"reason": str(exc)}
        )
        return
    if resp.status_code not in (200, 204):
        logger.warning(
            "hermes.config_sync.delegation_inbox.ack_http_error",
            extra={"status": resp.status_code},
        )


async def _dispatch_request(
    *, envelope: dict[str, str], signature_hex: str, proxy: DbusRuntimeProxy
) -> str:
    """Hand a verified REQUEST envelope to the daemon via the NEW, narrowly
    scoped D-Bus verb `submit_inbound_delegation` — single writer (I-3): this
    module never inserts into `pending_delegations` itself.

    `signature_hex` travels alongside the envelope (LOW fix — defense in
    depth): the daemon RE-VERIFIES the Ed25519 tenant signature at its own
    boundary before registering the card, rather than trusting this process's
    prior verification unconditionally.

    Returns 'delivered_for_approval' on success (idempotent — the daemon's
    repo keys by message_id, so a re-delivery before ack is a safe no-op) or
    'daemon_unavailable' on any transport/D-Bus failure (retried next tick).
    """
    try:
        result = await proxy.call_dict(
            "submit_inbound_delegation",
            json.dumps({**envelope, "signature_hex": signature_hex}),
        )
    except Exception as exc:  # noqa: BLE001 — never raise into the poll loop
        logger.warning(
            "hermes.config_sync.delegation_inbox.submit_failed",
            extra={"message_id": envelope["message_id"], "reason": str(exc)},
        )
        return "daemon_unavailable"
    if not isinstance(result, dict) or not result.get("ok", False):
        return "daemon_unavailable"
    return "delivered_for_approval"


def _dispatch_result(
    *, envelope: dict[str, str], conn: sqlite3.Connection, db_path: Path
) -> str:
    """Deliver a verified RESULT envelope into A's ORIGINATING conversation
    (correlation_id -> conversation_id, recorded by `record_delegation_
    correlation` when `delegate_to_colleague` originally posted to
    /v1/outbox). Lower-risk than the REQUEST path — it only appends a message
    to a conversation the local human is ALREADY in, granting no new
    authority — so it is handled directly here (no new D-Bus verb needed).

    Idempotent by message_id (`_claim_result_delivery`) — MEDIUM-2 fix:
    now that a transient failure no longer marks the message as globally
    'seen' before dispatch, this envelope could in principle be re-dispatched;
    the claim guard makes the append itself safe to retry (never double-posts
    the same result into the conversation).

    Returns 'delivered_result' or 'unknown_correlation' (this instance never
    issued this correlation_id, or the local mapping was lost).
    """
    row = conn.execute(
        "SELECT conversation_id FROM delegation_outbox_correlations "
        "WHERE correlation_id = ?",
        (envelope["correlation_id"],),
    ).fetchone()
    if row is None:
        logger.warning(
            "hermes.config_sync.delegation_inbox.unknown_correlation",
            extra={"correlation_id": envelope["correlation_id"]},
        )
        return "unknown_correlation"

    message_id = envelope["message_id"]
    if not _claim_result_delivery(conn, message_id):
        return "delivered_result"  # already delivered on a prior pass — idempotent skip

    from uuid import UUID  # noqa: PLC0415

    from hermes.tasks.infrastructure.sqlite_conversation_repo import (  # noqa: PLC0415
        SQLiteConversationRepository,
    )

    try:
        repo = SQLiteConversationRepository(db_path=db_path)
        repo.append_message(
            conversation_id=UUID(row["conversation_id"]),
            role="assistant",
            content=f"{_UNTRUSTED_RESULT_HEADER}\n\n{envelope['body']}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.config_sync.delegation_inbox.result_delivery_failed",
            extra={"correlation_id": envelope["correlation_id"], "reason": str(exc)},
        )
        _unclaim_result_delivery(conn, message_id)
        return "unknown_correlation"
    return "delivered_result"


def _prune_stale_delegation_state(conn: sqlite3.Connection) -> None:
    """Bounds the local bookkeeping tables (LOW/1-style fix)."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=_STATE_RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM delegation_inbox_seen WHERE seen_at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM delegation_outbox_correlations WHERE created_at < ?", (cutoff,)
    )
    conn.execute(
        "DELETE FROM delegation_result_pushed WHERE pushed_at < ?", (cutoff,)
    )
    conn.execute(
        "DELETE FROM delegation_result_delivered WHERE delivered_at < ?", (cutoff,)
    )


async def poll_and_apply_inbox_once(
    *,
    db_path: Path,
    cloud_endpoint: str,
    instance_id: str,
    instance_secret: str,
    pubkey_hex: str,
    proxy: DbusRuntimeProxy,
) -> None:
    """POLL inbox messages, verify+dispatch each fail-closed (see module
    docstring for the exact matrix), then ACK every fully-resolved outcome in
    ONE batched request. One malformed/malicious item never affects another.
    Pruning runs every tick regardless of whether any messages were returned.

    `since` cursor: advances to the instant THIS poll started, but ONLY on a
    successful fetch (`_fetch_inbox` returning None — transport/HTTP/parse
    failure — never advances it, so a retried poll after an outage re-asks
    for the SAME window instead of silently skipping it) AND only when EVERY
    item in this batch reached a terminal, ACKed outcome (MEDIUM-2 fix: an
    item left un-acked — daemon_unavailable/unknown_correlation/bad_signature/
    wrong_instance — must keep being re-served by the cloud, so the cursor
    must never advance past it). `since` is a scoping optimisation only —
    message_id dedup + ack remain the actual correctness mechanism regardless
    of what the cloud does with it.

    The anti-replay marker (`_mark_message_seen`) is written HERE, per item,
    ONLY for a terminal/ACKed outcome — never before dispatch (see
    `_verify_envelope`/`_is_message_seen` docstrings).
    """
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        _prune_stale_delegation_state(conn)
        since = _read_poll_cursor(conn)
        poll_started_at = datetime.now(tz=UTC).isoformat()
        items = _fetch_inbox(
            cloud_endpoint=cloud_endpoint, instance_id=instance_id,
            instance_secret=instance_secret, since=since,
        )
        if items is None:
            return  # transport/HTTP failure — retry next tick with the SAME since

        to_ack: list[str] = []
        any_unacked = False
        for item in items:
            outcome, envelope = _verify_envelope(
                item=item, pubkey_hex=pubkey_hex, own_instance_id=instance_id, conn=conn,
            )
            message_id = item.get("message_id") if isinstance(item, dict) else None
            if outcome == "verified" and envelope is not None:
                if envelope["kind"] == "request":
                    signature_hex = (
                        item.get("signature_hex") if isinstance(item, dict) else None
                    )
                    outcome = await _dispatch_request(
                        envelope=envelope, signature_hex=signature_hex or "",
                        proxy=proxy,
                    )
                else:
                    outcome = _dispatch_result(
                        envelope=envelope, conn=conn, db_path=db_path
                    )
            logger.info(
                "hermes.config_sync.delegation_inbox.outcome=%s",
                outcome,
                extra={"message_id": message_id, "outcome": outcome},
            )
            if outcome in _ACK_OUTCOMES and isinstance(message_id, str) and message_id:
                _mark_message_seen(conn, message_id)
                to_ack.append(message_id)
            else:
                any_unacked = True
        if not any_unacked:
            _write_poll_cursor(conn, poll_started_at)
    finally:
        conn.close()

    if to_ack:
        _ack_messages(
            cloud_endpoint=cloud_endpoint, instance_secret=instance_secret,
            message_ids=to_ack,
        )


# ---------------------------------------------------------------------------
# Outbound correlation bookkeeping — written by the DAEMON (delegate_to_
# colleague tool, at request time), read by THIS process (result delivery).
# ---------------------------------------------------------------------------


def record_delegation_correlation(
    *, db_path: Path, correlation_id: str, conversation_id: str
) -> None:
    """Persist correlation_id -> conversation_id right after `delegate_to_
    colleague` posts to /v1/outbox (daemon process). Read later by
    `_dispatch_result` (config_sync process, separate OS process — same
    shell-state.db file, WAL-safe) to route the eventual kind=result envelope
    back into A's originating conversation. Idempotent (INSERT OR IGNORE — a
    fresh correlation_id is minted per delegate_to_colleague call, never
    reused, so a collision only happens on an exact retry of the same call).
    """
    now = datetime.now(tz=UTC).isoformat()
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO delegation_outbox_correlations "
            "(correlation_id, conversation_id, created_at) VALUES (?, ?, ?)",
            (correlation_id, conversation_id, now),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PUSH — B's side, once an approved external_delegation task completes: POST
# the result to /v1/outbox/result so A's inbox consumer can deliver it back.
# ---------------------------------------------------------------------------


def _fetch_unpushed_delegation_results(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Completed external_delegation tasks whose final assistant answer has
    not yet been pushed to the cloud. Joins agent_tasks (trigger_kind /
    status) with the conversation's `messages` table — both live in the SAME
    shell-state.db file.

    LOW fix (RESULT path hardening): a task's conversation can carry MULTIPLE
    assistant turns (tool back-and-forth, streaming commits, etc.) all tagged
    with the same task_id — a plain `JOIN ... role='assistant'` (no ordering/
    LIMIT) could return several rows per task_id and push an INTERMEDIATE
    turn instead of the final answer. The correlated subquery picks exactly
    ONE row per task: the assistant message with the LATEST created_at
    (ISO-8601, so lexicographic order == chronological order), tie-broken by
    message_id for full determinism.
    """
    return conn.execute(
        """
        SELECT t.task_id, t.payload_json, m.content AS result_body
        FROM agent_tasks t
        JOIN messages m ON m.message_id = (
            SELECT m2.message_id FROM messages m2
            WHERE m2.task_id = t.task_id AND m2.role = 'assistant'
            ORDER BY m2.created_at DESC, m2.message_id DESC
            LIMIT 1
        )
        LEFT JOIN delegation_result_pushed p ON p.task_id = t.task_id
        WHERE t.trigger_kind = 'external_delegation'
          AND t.status = 'completed'
          AND p.task_id IS NULL
        ORDER BY t.created_at ASC
        """
    ).fetchall()


def _clamp_result_push_body(body: str) -> str:
    """Clamp to the cloud's enforced cap before POST /v1/outbox/result (LOW
    fix — RESULT path hardening): an oversized body gets a 422 from the cloud
    and is retried forever (the 'pushed' marker is only written on 2xx).
    Truncation is marked so the receiving human sees the answer was cut."""
    if len(body) <= _MAX_RESULT_PUSH_BODY_CHARS:
        return body
    keep = _MAX_RESULT_PUSH_BODY_CHARS - len(_RESULT_TRUNCATION_MARKER)
    if keep <= 0:
        return _RESULT_TRUNCATION_MARKER[:_MAX_RESULT_PUSH_BODY_CHARS]
    return body[:keep] + _RESULT_TRUNCATION_MARKER


def _post_delegation_result(
    *, cloud_endpoint: str, instance_secret: str, correlation_id: str, body: str,
) -> bool:
    """POST ONE result. Never raises; False on ANY transport/HTTP failure
    (retried next tick — the 'pushed' marker is only written on success)."""
    try:
        resp = httpx.post(
            f"{cloud_endpoint.rstrip('/')}/v1/outbox/result",
            headers={"Authorization": f"Bearer {instance_secret}"},
            json={"correlation_id": correlation_id, "body": body},
            timeout=_HTTP_TIMEOUT_S,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "hermes.config_sync.delegation_inbox.push_result_error",
            extra={"correlation_id": correlation_id, "reason": str(exc)},
        )
        return False
    if resp.status_code not in (200, 201, 204):
        logger.warning(
            "hermes.config_sync.delegation_inbox.push_result_http_error",
            extra={"correlation_id": correlation_id, "status": resp.status_code},
        )
        return False
    return True


def push_pending_delegation_results_once(
    *, db_path: Path, cloud_endpoint: str, instance_secret: str,
) -> None:
    """PUSH every completed external_delegation task's result not yet pushed.
    Fail-soft per-row: one failed push never blocks the others and is
    retried on the next tick (no marker written on failure)."""
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        for row in _fetch_unpushed_delegation_results(conn):
            correlation_id = _extract_correlation_id(row["payload_json"])
            if not correlation_id:
                continue  # not a delegated task after all (defensive) — skip
            pushed = _post_delegation_result(
                cloud_endpoint=cloud_endpoint,
                instance_secret=instance_secret,
                correlation_id=correlation_id,
                body=_clamp_result_push_body(row["result_body"] or ""),
            )
            if pushed:
                now = datetime.now(tz=UTC).isoformat()
                conn.execute(
                    "INSERT OR IGNORE INTO delegation_result_pushed "
                    "(task_id, correlation_id, pushed_at) VALUES (?, ?, ?)",
                    (row["task_id"], correlation_id, now),
                )
    finally:
        conn.close()


def _extract_correlation_id(payload_json: str | None) -> str:
    if not payload_json:
        return ""
    try:
        payload = json.loads(payload_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("delegation_correlation_id") or "")


# ---------------------------------------------------------------------------
# Orchestration — one tick: PUSH pending results, then POLL the inbox. Both
# fail-soft, mirroring remote_approvals.run_remote_approvals_once.
# ---------------------------------------------------------------------------


def _endpoint_is_safe(endpoint: str) -> bool:
    try:
        _validate_cloud_endpoint(endpoint)
        return True
    except PairingError as exc:
        logger.error(
            "hermes.config_sync.delegation_inbox.endpoint_unsafe", extra={"reason": str(exc)}
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


async def run_delegation_inbox_once(
    *, store: SQLiteAssociationStore, proxy: DbusRuntimeProxy, db_path: Path | None = None,
) -> None:
    """One associate-initiated tick: PUSH pending delegation results, then
    POLL + verify + dispatch inbox messages. Fail-soft end-to-end — never
    raises into the caller's loop.

    Pull-only/NAT: this ONLY ever opens outbound HTTPS connections (+ ONE
    local D-Bus call to the daemon for the REQUEST path); it never listens on
    a socket.
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
            "hermes.config_sync.delegation_inbox.setup_failed",
            extra={"reason": str(exc)},
            exc_info=True,
        )
        return

    resolved_db_path = db_path or _STATE_DB_PATH

    try:
        push_pending_delegation_results_once(
            db_path=resolved_db_path,
            cloud_endpoint=assoc.cloud_endpoint,
            instance_secret=instance_secret,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.config_sync.delegation_inbox.push_failed",
            extra={"reason": str(exc)},
            exc_info=True,
        )

    try:
        await poll_and_apply_inbox_once(
            db_path=resolved_db_path,
            cloud_endpoint=assoc.cloud_endpoint,
            instance_id=assoc.instance_id,
            instance_secret=instance_secret,
            pubkey_hex=assoc.signing_pubkey_hex,
            proxy=proxy,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.config_sync.delegation_inbox.poll_failed",
            extra={"reason": str(exc)},
            exc_info=True,
        )
