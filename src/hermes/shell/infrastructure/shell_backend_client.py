"""ShellBackendClient — HTTP + WebSocket client al hermes-shell-server local.

Vive en infrastructure/ — los widgets GTK4 no hablan HTTP directo.
Usa requests para REST (sync, simple) y websocket-client para WS streaming
en un thread aparte. Los eventos cruzan al main loop GTK via GLib.idle_add.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_DEFAULT_BACKEND = "http://127.0.0.1:7517"


def _backend_url() -> str:
    return os.environ.get("HERMES_SHELL_BACKEND_URL", _DEFAULT_BACKEND)


@dataclass(frozen=True, slots=True)
class ProviderDTO:
    provider_id: str
    alias: str
    kind: str
    base_url: str | None
    default_model: str
    enabled: bool
    is_active: bool
    has_api_key: bool
    connectivity: str

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderDTO":
        return cls(
            provider_id=d["provider_id"],
            alias=d["alias"],
            kind=d["kind"],
            base_url=d.get("base_url"),
            default_model=d["default_model"],
            enabled=d.get("enabled", True),
            is_active=d.get("is_active", False),
            has_api_key=d.get("has_api_key", False),
            connectivity=d.get("connectivity", "unknown"),
        )


class ShellBackendClient:
    """REST + WS client al backend localhost."""

    def __init__(self, *, base_url: str | None = None) -> None:
        self._base = base_url or _backend_url()

    # ------------------------------------------------------------------
    # REST helpers (urllib, sin deps externas)
    # ------------------------------------------------------------------
    def _request(
        self,
        *,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        timeout: float = 5.0,
    ) -> Any:
        url = self._base + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 204:
                    return None
                payload = resp.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            logger.warning("HTTP %s %s -> %s: %s", method, path, exc.code, body_text)
            raise
        except urllib.error.URLError as exc:
            logger.warning("URL error %s %s: %s", method, path, exc)
            raise

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------
    def list_providers(self) -> list[ProviderDTO]:
        data = self._request(path="/api/v1/providers") or []
        return [ProviderDTO.from_dict(d) for d in data]

    def create_provider(
        self,
        *,
        alias: str,
        kind: str,
        default_model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        set_active: bool = False,
    ) -> ProviderDTO:
        payload = {
            "alias": alias,
            "kind": kind,
            "default_model": default_model,
            "set_active": set_active,
        }
        if base_url:
            payload["base_url"] = base_url
        if api_key:
            payload["api_key"] = api_key
        d = self._request(path="/api/v1/providers", method="POST", body=payload)
        return ProviderDTO.from_dict(d)

    def update_provider(
        self, *, provider_id: str, **fields: Any
    ) -> ProviderDTO:
        d = self._request(
            path=f"/api/v1/providers/{provider_id}",
            method="PATCH",
            body=fields,
        )
        return ProviderDTO.from_dict(d)

    def delete_provider(self, *, provider_id: str) -> None:
        self._request(
            path=f"/api/v1/providers/{provider_id}", method="DELETE"
        )

    def activate_provider(self, *, provider_id: str) -> ProviderDTO:
        d = self._request(
            path=f"/api/v1/providers/{provider_id}/activate", method="POST"
        )
        return ProviderDTO.from_dict(d)

    def test_provider(self, *, provider_id: str) -> dict:
        return self._request(
            path=f"/api/v1/providers/{provider_id}/test",
            method="POST",
            timeout=15.0,
        )

    def list_models(self, *, provider_id: str) -> list[str]:
        return self._request(
            path=f"/api/v1/providers/{provider_id}/models", timeout=10.0
        ) or []

    def auto_detect_local(self) -> list[ProviderDTO]:
        data = self._request(
            path="/api/v1/providers/auto-detect",
            method="POST",
            timeout=10.0,
        ) or []
        return [ProviderDTO.from_dict(d) for d in data]

    def get_active(self) -> ProviderDTO | None:
        data = self._request(path="/api/v1/providers/active")
        if not data:
            return None
        return ProviderDTO.from_dict(data)

    # ------------------------------------------------------------------
    # Audit / Skills / Consents
    # ------------------------------------------------------------------
    def list_audit(self, *, limit: int = 200) -> list[dict]:
        return self._request(path=f"/api/v1/audit?limit={limit}") or []

    def list_skills(self) -> list[dict]:
        return self._request(path="/api/v1/skills") or []

    def deprecate_skill(self, *, package_id: str) -> dict:
        return self._request(
            path=f"/api/v1/skills/{package_id}/deprecate", method="POST"
        ) or {}

    def start_teaching(
        self,
        *,
        skill_name: str,
        description: str | None = None,
        surface_kind: str = "browser",
        site_id: str = "",
    ) -> dict:
        """POST /api/v1/training with teaching context fields.

        Returns {session_id, state, teaching_context:{context_id,
        isolation_key, surface_kind, input_owner}}.
        Raises urllib.error.HTTPError 409 if input_owner_conflict.
        """
        body: dict[str, Any] = {
            "skill_name": skill_name,
            "surface_kind": surface_kind,
            "site_id": site_id,
        }
        if description:
            body["description"] = description
        return self._request(path="/api/v1/training", method="POST", body=body) or {}

    def promote_skill(self, *, package_id: str) -> dict:
        """POST /api/v1/skills/{package_id}/promote — transitions validated → autonomous.

        Returns SkillDTO with state="autonomous", promoted_at, promoted_by.
        """
        return (
            self._request(
                path=f"/api/v1/skills/{package_id}/promote",
                method="POST",
                body={"confirm": True},
            )
            or {}
        )

    def create_composio_skill(
        self,
        *,
        skill_name: str,
        toolkit_slug: str,
        intent_text: str,
    ) -> dict:
        """POST /api/v1/skills/composio — create a validated Composio skill.

        Args:
            skill_name:    Human-readable name for the skill (1–120 chars).
            toolkit_slug:  Composio toolkit identifier (must be ACTIVE-connected).
            intent_text:   Natural-language description of what the skill does
                           (1–2000 chars, no control characters).

        Returns:
            SkillPackageDTO dict with skill_kind="composio" and toolkit_slug set.

        Raises:
            urllib.error.HTTPError: 400 if toolkit not connected or invalid input;
                                    409 if the skill name+version already exists;
                                    503 if Composio credentials are absent.
        """
        return (
            self._request(
                path="/api/v1/skills/composio",
                method="POST",
                body={
                    "skill_name": skill_name,
                    "toolkit_slug": toolkit_slug,
                    "intent_text": intent_text,
                },
                timeout=15.0,
            )
            or {}
        )

    def list_consents(self, *, include_revoked: bool = False) -> list[dict]:
        suffix = "?include_revoked=true" if include_revoked else ""
        return self._request(path=f"/api/v1/consents{suffix}") or []

    def grant_consent(
        self, *, capability: str, scope: str = "session"
    ) -> dict:
        return self._request(
            path="/api/v1/consents",
            method="POST",
            body={"capability": capability, "scope": scope},
        ) or {}

    def revoke_consent(self, *, consent_id: str) -> None:
        self._request(path=f"/api/v1/consents/{consent_id}", method="DELETE")

    # ------------------------------------------------------------------
    # Remote control (F11)
    # ------------------------------------------------------------------
    def list_remote_control_sessions(self) -> list[dict]:
        return self._request(path="/api/v1/remote-control/sessions") or []

    def revoke_remote_control_session(self, *, session_id: str) -> dict:
        return self._request(
            path=f"/api/v1/remote-control/sessions/{session_id}/revoke",
            method="POST",
        ) or {}

    # ------------------------------------------------------------------
    # First-boot wizard (F12)
    # ------------------------------------------------------------------

    def wizard_status(self) -> dict:
        """GET /api/v1/wizard/status -> {first_boot_complete, completed_at}."""
        return self._request(path="/api/v1/wizard/status") or {}

    def wizard_start(self) -> dict:
        """POST /api/v1/wizard/start (201) -> {session_id, state, assistant_message, snapshot}."""
        return self._request(path="/api/v1/wizard/start", method="POST") or {}

    def wizard_send(self, *, session_id: str, msg: str) -> dict:
        """POST /api/v1/wizard/{sid}/message -> {session_id, state, assistant_message, snapshot, done}."""
        return (
            self._request(
                path=f"/api/v1/wizard/{session_id}/message",
                method="POST",
                body={"user_message": msg},
                timeout=60.0,
            )
            or {}
        )

    def wizard_finalize(self, *, session_id: str) -> dict:
        """POST /api/v1/wizard/{sid}/finalize -> {session_id, node_installation_id}."""
        return (
            self._request(
                path=f"/api/v1/wizard/{session_id}/finalize",
                method="POST",
                timeout=30.0,
            )
            or {}
        )

    # ------------------------------------------------------------------
    # First-boot wizard — DETERMINISTA por formulario (sin LLM)
    # ------------------------------------------------------------------

    def wizard_form_start(self) -> dict:
        """POST /api/v1/wizard/form/start (201) -> {session_id, state}."""
        return self._request(path="/api/v1/wizard/form/start", method="POST") or {}

    def wizard_form_get(self, *, session_id: str) -> dict:
        """GET /api/v1/wizard/form/{sid} -> {session_id, state, snapshot}."""
        return self._request(path=f"/api/v1/wizard/form/{session_id}") or {}

    def wizard_set_profile(self, *, session_id: str, profile_kind: str) -> dict:
        """POST .../form/{sid}/profile -> {session_id, state}."""
        return (
            self._request(
                path=f"/api/v1/wizard/form/{session_id}/profile",
                method="POST",
                body={"profile_kind": profile_kind},
            )
            or {}
        )

    def wizard_set_locale(
        self,
        *,
        session_id: str,
        language_code: str,
        keyboard_layout: str,
        timezone: str,
    ) -> dict:
        """POST .../form/{sid}/locale -> {session_id, state}."""
        return (
            self._request(
                path=f"/api/v1/wizard/form/{session_id}/locale",
                method="POST",
                body={
                    "language_code": language_code,
                    "keyboard_layout": keyboard_layout,
                    "timezone": timezone,
                },
            )
            or {}
        )

    def wizard_set_network(self, *, session_id: str, decision: str) -> dict:
        """POST .../form/{sid}/network -> {session_id, state}. decision: connected|offline_continue."""
        return (
            self._request(
                path=f"/api/v1/wizard/form/{session_id}/network",
                method="POST",
                body={"decision": decision},
            )
            or {}
        )

    def wizard_set_tenant(
        self,
        *,
        session_id: str,
        decision: str,
        tenant_endpoint_url: str | None = None,
        enrollment_token: str | None = None,
    ) -> dict:
        """POST .../form/{sid}/tenant -> {session_id, state}. decision: bind_now|defer."""
        body: dict[str, Any] = {"decision": decision}
        if tenant_endpoint_url:
            body["tenant_endpoint_url"] = tenant_endpoint_url
        if enrollment_token:
            body["enrollment_token"] = enrollment_token
        return (
            self._request(
                path=f"/api/v1/wizard/form/{session_id}/tenant",
                method="POST",
                body=body,
            )
            or {}
        )

    def wizard_set_consents(
        self, *, session_id: str, granted: list[tuple[str, str]]
    ) -> dict:
        """POST .../form/{sid}/consents -> {session_id, state}. granted: [(capability, scope)]."""
        return (
            self._request(
                path=f"/api/v1/wizard/form/{session_id}/consents",
                method="POST",
                body={"granted": [list(pair) for pair in granted]},
            )
            or {}
        )

    def wizard_exposed_services(self) -> list[dict]:
        """GET .../form/exposed-services -> [{service_name, interface, protocol, human_description}]."""
        data = self._request(path="/api/v1/wizard/form/exposed-services") or {}
        return data.get("services", [])

    def wizard_review_services(self, *, session_id: str, acknowledged: bool) -> dict:
        """POST .../form/{sid}/services -> {session_id, state}."""
        return (
            self._request(
                path=f"/api/v1/wizard/form/{session_id}/services",
                method="POST",
                body={"acknowledged": acknowledged},
            )
            or {}
        )

    def wizard_form_finalize(self, *, session_id: str) -> dict:
        """POST .../form/{sid}/finalize -> {session_id, node_installation_id}."""
        return (
            self._request(
                path=f"/api/v1/wizard/form/{session_id}/finalize",
                method="POST",
                timeout=30.0,
            )
            or {}
        )

    # ------------------------------------------------------------------
    # Integrations — Composio
    # ------------------------------------------------------------------

    def composio_status(self) -> dict:
        """GET /api/v1/integrations/composio/status -> {has_key, enabled, entity_id}."""
        return self._request(path="/api/v1/integrations/composio/status") or {}

    def set_composio_key(self, *, api_key: str, entity_id: str = "default") -> dict:
        """POST /api/v1/integrations/composio/key -> {has_key, enabled, entity_id}."""
        return (
            self._request(
                path="/api/v1/integrations/composio/key",
                method="POST",
                body={"api_key": api_key, "entity_id": entity_id},
            )
            or {}
        )

    def composio_toolkits(self, *, search: str = "", limit: int = 50) -> list[dict]:
        """GET /api/v1/integrations/composio/toolkits -> list of toolkit dicts."""
        params = f"limit={limit}"
        if search:
            import urllib.parse  # noqa: PLC0415

            params += f"&search={urllib.parse.quote(search)}"
        return (
            self._request(path=f"/api/v1/integrations/composio/toolkits?{params}") or []
        )

    def composio_connected(self) -> list[dict]:
        """GET /api/v1/integrations/composio/connected -> list of connected account dicts."""
        return self._request(path="/api/v1/integrations/composio/connected") or []

    def composio_connect(self, *, slug: str) -> dict:
        """POST /api/v1/integrations/composio/connect -> {connected_account_id, redirect_url}."""
        return (
            self._request(
                path="/api/v1/integrations/composio/connect",
                method="POST",
                body={"toolkit_slug": slug},
            )
            or {}
        )

    def composio_disconnect(self, *, account_id: str) -> dict:
        """DELETE /api/v1/integrations/composio/connected/{id} -> {status, connection_id}."""
        return (
            self._request(
                path=f"/api/v1/integrations/composio/connected/{account_id}",
                method="DELETE",
            )
            or {}
        )

    # ------------------------------------------------------------------
    # Remote-access tunnel control (consent-gated disable / free enable)
    # ------------------------------------------------------------------

    def remote_access_status(self) -> dict:
        """GET /api/v1/remote-access/status -> {active: bool}."""
        return self._request(path="/api/v1/remote-access/status") or {}

    def remote_access_enable(self) -> dict:
        """POST /api/v1/remote-access/enable -> {staged: bool}.

        No password required — enabling is safe.
        """
        return self._request(path="/api/v1/remote-access/enable", method="POST") or {}

    def remote_access_disable(self, *, password: str) -> dict:
        """POST /api/v1/remote-access/disable -> {staged: bool}.

        Requires the device account password.
        Raises urllib.error.HTTPError 403 on wrong password,
        429 on rate-limit, 400 on invalid password chars.

        SECURITY: password is sent over localhost HTTP only (127.0.0.1:7517).
        """
        return (
            self._request(
                path="/api/v1/remote-access/disable",
                method="POST",
                body={"password": password},
            )
            or {}
        )

    def set_account(self, username: str, password: str) -> dict:
        """POST /api/v1/setup/account — apply OS username + password on first boot.

        # TODO: endpoint /api/v1/setup/account (aplica user/passwd via helper privilegiado)
        # — pendiente backend. El cliente tolera 404 para que el wizard no bloquee
        # en entornos donde el endpoint aún no existe.
        """
        import urllib.error  # noqa: PLC0415

        _NOT_IMPLEMENTED = 404  # noqa: N806
        try:
            return (
                self._request(
                    path="/api/v1/setup/account",
                    method="POST",
                    body={"username": username, "password": password},
                )
                or {}
            )
        except urllib.error.HTTPError as exc:
            if exc.code == _NOT_IMPLEMENTED:
                logger.debug(
                    "set_account: endpoint not yet implemented (404), skipping"
                )
                return {}
            raise

    # ------------------------------------------------------------------
    # Tasks dashboard (F007)
    # ------------------------------------------------------------------

    def list_configured_tasks(self, *, limit: int = 200) -> dict:
        """GET /api/v1/tasks/configured → {available, tasks: [...]}

        Returns the response dict as-is.  `available` is False when the
        runtime daemon is not reachable (shell renders disconnected state).
        Raises urllib.error.URLError if the shell-server itself is down.
        """
        return (
            self._request(path=f"/api/v1/tasks/configured?limit={limit}") or {}
        )

    def list_recent_tasks(self, *, limit: int = 50) -> dict:
        """GET /api/v1/tasks/recent → {available, tasks: [...]}

        Returns the response dict as-is.  `available` is False when the
        runtime daemon is not reachable.
        Raises urllib.error.URLError if the shell-server itself is down.
        """
        return (
            self._request(path=f"/api/v1/tasks/recent?limit={limit}") or {}
        )

    # ------------------------------------------------------------------
    # Chat streaming (REST + WS en thread)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Conversations history
    # ------------------------------------------------------------------
    def list_conversations(self) -> list[dict]:
        return self._request(path="/api/v1/chat/conversations") or []

    def get_conversation(self, *, conversation_id: str) -> dict:
        return self._request(
            path=f"/api/v1/chat/conversations/{conversation_id}"
        ) or {}

    def delete_conversation(self, *, conversation_id: str) -> None:
        self._request(
            path=f"/api/v1/chat/conversations/{conversation_id}",
            method="DELETE",
        )

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def start_chat(
        self, *, user_message: str, conversation_id: str | None = None
    ) -> tuple[str, str]:
        payload: dict[str, Any] = {"user_message": user_message}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        data = self._request(
            path="/api/v1/chat", method="POST", body=payload
        )
        return data["conversation_id"], data["ws_url"]

    def stream_chat_in_thread(
        self,
        *,
        ws_path: str,
        on_chunk: Callable[[dict], None],
        on_done: Callable[[], None],
    ) -> threading.Thread:
        """Abre WS al backend, llama on_chunk(dict) por cada mensaje + on_done al cerrar.

        Las callbacks se llaman desde el thread del WS — los widgets GTK deben
        usar GLib.idle_add internamente.
        """

        def _runner() -> None:
            try:
                from websocket import create_connection  # type: ignore
            except ImportError:
                logger.error("websocket-client no instalado")
                on_chunk({"kind": "error", "error": "websocket-client missing"})
                on_done()
                return
            host = self._base.replace("http://", "").replace("https://", "")
            ws_url = f"ws://{host}{ws_path}"
            try:
                ws = create_connection(ws_url, timeout=120)
            except Exception as exc:  # noqa: BLE001
                on_chunk({"kind": "error", "error": f"ws connect: {exc}"})
                on_done()
                return
            try:
                while True:
                    msg = ws.recv()
                    if not msg:
                        break
                    try:
                        chunk = json.loads(msg)
                    except json.JSONDecodeError:
                        chunk = {"kind": "error", "error": "bad json"}
                    on_chunk(chunk)
                    if chunk.get("kind") in ("done", "error"):
                        break
            except Exception as exc:  # noqa: BLE001
                on_chunk({"kind": "error", "error": str(exc)})
            finally:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
                on_done()

        t = threading.Thread(target=_runner, daemon=True, name="hermes-chat-ws")
        t.start()
        return t
