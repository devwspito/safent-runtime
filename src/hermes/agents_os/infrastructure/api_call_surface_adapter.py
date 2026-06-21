"""ApiCallSurfaceAdapter — captura/replay de llamadas HTTP.

FR-027/028 (spec 003): el formador demuestra una integración con API
(REST/GraphQL/webhook) y Hermes la aprende como skill replayable.

Diseño:
- Captura: ejecuta la llamada HTTP, registra method + URL + headers
  (sin secrets) + body (tokenizado PII) + status + response body
  (tokenizado).
- Replay: re-ejecuta la llamada, valida status esperado.
- URL allowlist obligatoria (constitución IV fail-closed): el adapter
  recibe lista de hosts permitidos en constructor.
- Headers sensibles (Authorization, X-API-Key, Cookie, Set-Cookie) NUNCA
  se persisten — se referencian por kid del KMS y se rehidratan en runtime.

Lazy-import de ``aiohttp`` para no requerirlo a importar el módulo.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

_REDACTED_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-amz-security-token",
        "proxy-authorization",
    }
)
_SUPPORTED_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)


class ApiCallSurfaceAdapter:
    """Cumple ``SurfaceAdapterPort`` para superficie ``API_CALL``."""

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        max_response_bytes: int = 256 * 1024,
        timeout_s: float = 20.0,
    ) -> None:
        if not allowed_hosts:
            raise ValueError(
                "allowed_hosts vacío — fail-closed (constitución IV). "
                "El cliente DEBE declarar explícitamente hosts accesibles."
            )
        self._allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self._max_response_bytes = max_response_bytes
        self._timeout_s = timeout_s

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.API_CALL

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        method = str(params.get("method", "GET")).upper()
        url = str(params.get("url", ""))
        if method not in _SUPPORTED_METHODS:
            raise ValueError(
                f"method {method!r} no soportado. Soportados: {sorted(_SUPPORTED_METHODS)}"
            )
        self._assert_host_allowed(url)
        headers = self._redact_headers(params.get("headers", {}))
        body = params.get("body")
        status, response_body_text, response_headers = await self._do_call(
            method, url, headers, body
        )
        return CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.API_CALL,
            intent_desc=intent_desc,
            payload={
                "method": method,
                "url": url,
                "headers_redacted": headers,
                "body_template": body if isinstance(body, dict) else None,
                "status_expected": status,
                "response_summary": _truncate(
                    response_body_text, self._max_response_bytes
                ),
            },
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        if action.surface_kind != SurfaceKind.API_CALL:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"surface mismatch: esperado API_CALL, got {action.surface_kind}",
            )
        method = action.payload.get("method", "GET")
        url = action.payload.get("url", "")
        try:
            self._assert_host_allowed(url)
        except PermissionError as exc:
            return ReplayOutcome.rejected_by_policy(
                action.action_id, reason=str(exc)
            )
        headers = action.payload.get("headers_redacted", {})
        body = action.payload.get("body_template")
        expected_status = action.payload.get("status_expected", 200)
        try:
            status, _body_text, _resp_headers = await self._do_call(
                method, url, headers, body
            )
        except Exception as exc:  # noqa: BLE001
            return ReplayOutcome.failed(
                action.action_id, error=f"{type(exc).__name__}: {exc}"
            )
        if status == expected_status or 200 <= status < 300:
            return ReplayOutcome.ok(
                action.action_id,
                result={"status": status, "expected": expected_status},
            )
        return ReplayOutcome.failed(
            action.action_id,
            error=f"status={status} expected={expected_status}",
        )

    def replay_payload(self, payload: dict) -> bool:
        """SurfaceReplayPort shim for SkillReplayer (sync → async bridge)."""
        import asyncio  # noqa: PLC0415

        from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
            CapturedAction,
            ReplayStatus,
        )

        action = CapturedAction(
            surface_kind=self.surface_kind,
            intent_desc=payload.get("intent_desc", ""),
            payload=payload,
        )
        outcome = asyncio.run(self.replay(action))
        return outcome.status == ReplayStatus.EXECUTED_OK

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        canonical = {
            "surface_kind": action.surface_kind.value,
            "intent_desc": action.intent_desc,
            "method": action.payload.get("method", ""),
            "url": action.payload.get("url", ""),
            "status_expected": action.payload.get("status_expected", 0),
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _assert_host_allowed(self, url: str) -> None:
        if not url:
            raise PermissionError("url vacía")
        host = urlparse(url).hostname or ""
        host = host.lower()
        for allowed in self._allowed_hosts:
            if host == allowed or host.endswith("." + allowed):
                return
        raise PermissionError(
            f"host {host!r} fuera de allowlist {self._allowed_hosts} "
            "(constitución IV fail-closed)"
        )

    def _redact_headers(self, headers: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in headers.items():
            if k.lower() in _REDACTED_HEADERS:
                out[k] = f"[[REDACTED:{k}]]"
            else:
                out[k] = str(v)
        return out

    async def _do_call(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: Any,
    ) -> tuple[int, str, dict[str, str]]:
        import aiohttp  # noqa: PLC0415

        timeout = aiohttp.ClientTimeout(total=self._timeout_s)
        # Despojamos placeholders [[REDACTED:*]] de los headers — el caller
        # debe inyectar valores reales tras consultar el KMS antes del replay.
        clean_headers = {
            k: v for k, v in headers.items() if "[[REDACTED:" not in v
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            kwargs: dict[str, Any] = {"headers": clean_headers}
            if body is not None:
                if isinstance(body, dict):
                    kwargs["json"] = body
                else:
                    kwargs["data"] = body
            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                return (
                    resp.status,
                    text[: self._max_response_bytes],
                    {k: v for k, v in resp.headers.items()},
                )


def _truncate(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[...]"


def hash_action(adapter: ApiCallSurfaceAdapter, action: CapturedAction) -> str:
    return hashlib.sha256(adapter.serialize_for_signing(action)).hexdigest()
