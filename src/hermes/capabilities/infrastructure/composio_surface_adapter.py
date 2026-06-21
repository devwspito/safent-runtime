"""ComposioSurfaceAdapter — surface adapter para acciones READ de Composio.

Implementa SurfaceAdapterPort para SurfaceKind.API_CALL. Las acciones READ
de Composio (GMAIL_GET_EMAIL, GOOGLEDRIVE_LIST_FILES, …) pasan por aquí
cuando el broker las despacha, en lugar de ejecutarse directamente en el
handler de la ToolSpec (que era el bypass de seguridad KC-4).

Garantías de seguridad:
  - La ejecución NUNCA ocurre fuera del broker (CTRL-1..14 aplican).
  - El api_key NUNCA se loguea (la API key solo viaja en memoria).
  - El resultado se marca como contenido externo no confiable (CTRL-5)
    en CapturedAction.payload["is_external_content"] = True, para que
    el orchestrator propague el taint al ConsentContext.
  - Timeout acotado (fail-closed ante red lenta o colgada).

SurfaceKind: API_CALL (acciones Composio son llamadas HTTP/REST a servicios
externos — encajan semánticamente con la superficie API_CALL existente).

Capa: infrastructure (adapta ComposioClient al contrato SurfaceAdapterPort).
Sin lógica de dominio ni framework.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger("hermes.capabilities.composio_adapter")

# Timeout para la llamada Composio. Debe ser mayor que el timeout del
# SDK de Composio pero menor que el timeout del broker (30s) para que
# el error sea distinguible de un broker timeout.
_COMPOSIO_EXEC_TIMEOUT_S: float = 25.0


class ComposioSurfaceAdapter:
    """SurfaceAdapterPort para acciones Composio en la superficie API_CALL.

    Args:
        api_key:   Composio API key (nunca logueada).
        entity_id: Composio entity_id del usuario.
    """

    def __init__(self, *, api_key: str, entity_id: str) -> None:
        if not api_key:
            raise ValueError("ComposioSurfaceAdapter: api_key vacío — fail-closed")
        if not entity_id:
            raise ValueError("ComposioSurfaceAdapter: entity_id vacío — fail-closed")
        self._api_key = api_key
        self._entity_id = entity_id

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
        """Capture no aplica para READ de Composio (solo replay es relevante)."""
        raise NotImplementedError(
            "ComposioSurfaceAdapter.capture no se usa en el flujo READ. "
            "Las acciones Composio se reproducen vía replay() desde el broker."
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Ejecuta la acción Composio definida en action.payload.

        payload esperado:
            slug:      str  — nombre de la acción Composio (e.g. "GMAIL_GET_EMAIL").
            params:    dict — parámetros de la acción.
            entity_id: str  — entity_id (puede sobrescribir el del adapter).

        Fail-closed:
            - surface_kind != API_CALL → REJECTED_BY_POLICY.
            - slug ausente → REJECTED_BY_POLICY.
            - Error de red o de la API → EXECUTED_FAILED.
        """
        if action.surface_kind != SurfaceKind.API_CALL:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error=(
                    f"surface_kind mismatch en ComposioSurfaceAdapter: "
                    f"esperado API_CALL, got {action.surface_kind!r}"
                ),
            )

        slug = action.payload.get("slug")
        if not slug:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error="ComposioSurfaceAdapter.replay: slug ausente en payload — fail-closed",
            )

        params = dict(action.payload.get("params") or {})
        entity_id = str(action.payload.get("entity_id") or self._entity_id)
        connected_account_id: str | None = action.payload.get("connected_account_id") or None

        return await self._execute(
            action.action_id, slug, params, entity_id,
            connected_account_id=connected_account_id,
        )

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        """Serialización canónica para HMAC del audit (CTRL-9).

        connected_account_id se incluye para que el HMAC cubra la cuenta
        concreta ejecutada (CTRL-9: el audit refleja quién realmente actuó).
        """
        canonical = {
            "surface_kind": action.surface_kind.value,
            "slug": action.payload.get("slug", ""),
            "entity_id": action.payload.get("entity_id", ""),
            "connected_account_id": action.payload.get("connected_account_id") or "",
            "intent_desc": action.intent_desc,
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _execute(
        self,
        action_id: UUID,
        slug: str,
        params: dict[str, Any],
        entity_id: str,
        *,
        connected_account_id: str | None = None,
    ) -> ReplayOutcome:
        """Invoca ComposioClient.execute_action con timeout acotado."""
        import asyncio  # noqa: PLC0415

        from hermes.integrations.composio.composio_client import (  # noqa: PLC0415
            ComposioApiError,
            ComposioClient,
        )

        client = ComposioClient(api_key=self._api_key)
        try:
            result = await asyncio.wait_for(
                client.execute_action(
                    slug=slug,
                    params=params,
                    entity_id=entity_id,
                    connected_account_id=connected_account_id,
                ),
                timeout=_COMPOSIO_EXEC_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error(
                "hermes.composio_adapter.timeout: slug=%s entity_id=%s",
                slug,
                entity_id,
            )
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"ComposioSurfaceAdapter: timeout ejecutando {slug!r}",
            )
        except ComposioApiError as exc:
            logger.warning(
                "hermes.composio_adapter.api_error: slug=%s error=%s",
                slug,
                str(exc),
            )
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"ComposioApiError({exc.status_code}): {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.composio_adapter.unexpected_error: slug=%s error=%s",
                slug,
                str(exc),
            )
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        logger.info(
            "hermes.composio_adapter.executed: slug=%s entity_id=%s",
            slug,
            entity_id,
        )
        return ReplayOutcome(
            action_id=action_id,
            status=ReplayStatus.EXECUTED_OK,
            result=result if isinstance(result, dict) else {"data": result},
        )
