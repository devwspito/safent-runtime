"""REST endpoints for remote-control sessions.

POST /api/v1/remote-control/sessions
  → issues a session (operator approved), returns session_id + bearer token
    in a single-use HTTP 302 redirect to /redeem/{token} (FR-055 a).
GET  /api/v1/remote-control/sessions
  → list active sessions (UI indicator).
POST /api/v1/remote-control/sessions/{id}/revoke
  → operator local terminates the session.
GET  /api/v1/remote-control/redeem/{redeem_id}
  → single-use token redemption. Sets HttpOnly Secure SameSite=Strict cookie.
    Pins IP + UA + tenant + operator binding hash at first use.
POST /api/v1/remote-control/sessions/{id}/binding-violation
  → invoked by signaling service when DTLS fingerprint changes mid-session.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from hermes.agents_os.application.remote_control_orchestrator import (
    HumanConsentMissingError,
    RemoteControlEndReason,
    RemoteControlOrchestrator,
    RemoteControlScope,
    TokenCipher,
    TtlTooLongError,
)

from .binding import compute_binding
from .repo import RemoteControlRow, SQLiteRemoteControlRepo

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 15 * 60
_REDEEM_TTL_SECONDS = 120


class _RedeemToken(BaseModel):
    redeem_id: str
    session_id: UUID
    bearer: bytes
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


class _PendingRedemptions:
    """In-process registry of unredeemed bearer tokens (single-use).

    The DB stores ciphertext at-rest; this is the volatile cleartext for
    the 2-minute redeem window (FR-055).
    """

    def __init__(self) -> None:
        self._items: dict[str, _RedeemToken] = {}

    def add(self, token: _RedeemToken) -> None:
        self._items[token.redeem_id] = token

    def consume(self, redeem_id: str) -> _RedeemToken | None:
        item = self._items.get(redeem_id)
        if item is None:
            return None
        if item.consumed_at is not None:
            return None
        now = datetime.now(tz=UTC)
        if now > item.expires_at:
            return None
        self._items[redeem_id] = item.model_copy(update={"consumed_at": now})
        return self._items[redeem_id]

    def gc(self) -> None:
        now = datetime.now(tz=UTC)
        stale = [k for k, v in self._items.items() if v.expires_at < now]
        for k in stale:
            self._items.pop(k, None)


class IssueRequest(BaseModel):
    node_installation_id: UUID
    tenant_id: UUID
    operator_id: UUID
    scope: str = Field(pattern="^(os_full_desktop|workspace_browser_only)$")
    dtls_fingerprint: str = Field(min_length=32, max_length=512)
    consent_id: UUID
    local_operator_approved: bool
    ttl_seconds: int = Field(default=_DEFAULT_TTL_SECONDS, ge=60, le=3600)


class IssueResponse(BaseModel):
    session_id: UUID
    state: str
    redeem_url: str
    expires_at: datetime
    signaling_ws_url: str


class SessionDTO(BaseModel):
    session_id: UUID
    node_installation_id: UUID
    tenant_id: UUID
    operator_id: UUID
    scope: str
    state: str
    issued_at: datetime
    accepted_at: datetime | None
    ended_at: datetime | None
    end_reason: str | None
    token_expires_at: datetime
    redeem_ip: str | None
    # El DTLS fingerprint NO es secreto (se intercambia en el SDP). El daemon
    # de signaling lo necesita para fijar el binding del primer peer (FR-056).
    dtls_fingerprint: str


def _row_to_dto(row: RemoteControlRow) -> SessionDTO:
    return SessionDTO(
        session_id=row.session_id,
        node_installation_id=row.node_installation_id,
        tenant_id=row.tenant_id,
        operator_id=row.operator_id,
        scope=row.scope,
        state=row.state,
        issued_at=row.issued_at,
        accepted_at=row.accepted_at,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
        token_expires_at=row.token_expires_at,
        redeem_ip=row.redeem_ip,
        dtls_fingerprint=row.dtls_fingerprint,
    )


def create_remote_control_router(
    *,
    db_path: Path,
    cipher_key: bytes,
    cipher_kid: str,
    signaling_ws_base: str = "ws://127.0.0.1:7518/rc",
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/remote-control",
        tags=["remote-control"],
    )

    repo = SQLiteRemoteControlRepo(db_path)
    cipher = TokenCipher(key=cipher_key, kid=cipher_kid)
    orchestrator = RemoteControlOrchestrator(cipher=cipher)
    pending = _PendingRedemptions()

    @router.post(
        "/sessions",
        response_model=IssueResponse,
        status_code=201,
    )
    async def issue(req: IssueRequest) -> IssueResponse:
        try:
            session = orchestrator.issue(
                node_installation_id=req.node_installation_id,
                tenant_id=req.tenant_id,
                operator_id=req.operator_id,
                scope=RemoteControlScope(req.scope),
                dtls_fingerprint=req.dtls_fingerprint,
                consent_id=req.consent_id,
                local_operator_approved=req.local_operator_approved,
                ttl_seconds=req.ttl_seconds,
            )
        except HumanConsentMissingError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TtlTooLongError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        repo.insert(
            RemoteControlRow(
                session_id=session.session_id,
                node_installation_id=session.node_installation_id,
                tenant_id=session.tenant_id,
                operator_id=session.operator_id,
                scope=session.scope.value,
                token_ciphertext=session.token_ciphertext,
                token_kid=session.token_kid,
                token_expires_at=session.token_expires_at,
                dtls_fingerprint=session.dtls_fingerprint,
                binding_hash=session.binding_hash_hex,
                consent_id=session.consent_id,
                state=session.state.value,
                issued_at=session.issued_at,
                accepted_at=None,
                ended_at=None,
                end_reason=None,
                redeemed_at=None,
                redeem_ip=None,
                redeem_user_agent=None,
            )
        )

        redeem_id = secrets.token_urlsafe(32)
        bearer = secrets.token_bytes(32)
        now = datetime.now(tz=UTC)
        pending.add(
            _RedeemToken(
                redeem_id=redeem_id,
                session_id=session.session_id,
                bearer=bearer,
                issued_at=now,
                expires_at=now + timedelta(seconds=_REDEEM_TTL_SECONDS),
                consumed_at=None,
            )
        )
        pending.gc()

        return IssueResponse(
            session_id=session.session_id,
            state=session.state.value,
            redeem_url=f"/api/v1/remote-control/redeem/{redeem_id}",
            expires_at=session.token_expires_at,
            signaling_ws_url=f"{signaling_ws_base}/{session.session_id}",
        )

    @router.get("/sessions", response_model=list[SessionDTO])
    async def list_sessions() -> list[SessionDTO]:
        return [_row_to_dto(r) for r in repo.list_active(limit=50)]

    @router.get("/sessions/{session_id}", response_model=SessionDTO)
    async def get_session(session_id: UUID) -> SessionDTO:
        row = repo.get(session_id)
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        return _row_to_dto(row)

    @router.post(
        "/sessions/{session_id}/revoke",
        response_model=SessionDTO,
    )
    async def revoke(session_id: UUID) -> SessionDTO:
        row = repo.get(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        if row.state == "ended":
            return _row_to_dto(row)
        repo.transition_state(
            session_id,
            "ended",
            from_states=("issued", "accepted", "active"),
            end_reason=RemoteControlEndReason.LOCAL_OPERATOR_ENDED.value,
        )
        return _row_to_dto(repo.get(session_id))  # type: ignore[arg-type]

    @router.post(
        "/sessions/{session_id}/binding-violation",
        response_model=SessionDTO,
    )
    async def binding_violation(session_id: UUID) -> SessionDTO:
        repo.transition_state(
            session_id,
            "ended",
            from_states=("issued", "accepted", "active"),
            end_reason=RemoteControlEndReason.BINDING_VIOLATED.value,
        )
        row = repo.get(session_id)
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        return _row_to_dto(row)

    @router.get("/redeem/{redeem_id}")
    async def redeem(redeem_id: str, request: Request) -> Response:
        token = pending.consume(redeem_id)
        if token is None:
            raise HTTPException(
                status_code=410, detail="token expired or already consumed"
            )

        row = repo.get(token.session_id)
        if row is None or row.state != "issued":
            raise HTTPException(
                status_code=410, detail="session no longer redeemable"
            )

        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")[:512]

        binding_hex = compute_binding(
            ip=client_ip,
            user_agent=user_agent,
            tenant_id=row.tenant_id,
            operator_id=row.operator_id,
        )
        n = repo.mark_redeemed(
            token.session_id, ip=client_ip, user_agent=user_agent
        )
        if n != 1:
            raise HTTPException(
                status_code=409, detail="session already redeemed"
            )

        # max_age en SEGUNDOS (int), no un timedelta — set_cookie lo serializa
        # crudo si le pasas el objeto, produciendo "Max-Age=0:14:59.88" inválido.
        max_age_s = max(
            0,
            int((row.token_expires_at - datetime.now(tz=UTC)).total_seconds()),
        )

        response = RedirectResponse(
            url=f"/remote-control/viewer.html?sid={token.session_id}",
            status_code=302,
        )
        response.set_cookie(
            key="hermes_rc_bearer",
            value=token.bearer.hex(),
            max_age=max_age_s,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/api/v1/remote-control",
        )
        response.headers["X-RC-Binding"] = binding_hex[:16]
        return response

    return router
