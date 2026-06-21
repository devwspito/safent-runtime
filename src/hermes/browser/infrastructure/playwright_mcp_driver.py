"""PlaywrightMcpDriver: driver de aprendizaje sobre el servidor @playwright/mcp.

Este driver es OPCIONAL. Se activa cuando se inyecta una `McpSessionPort`
en vez de un driver Playwright/Stagehand. Util para la fase de RECORDING de
skills, donde el agente necesita navegar y actuar sobre paginas web sin LLM
inference en el driver mismo.

Diferencia critica respecto a PlaywrightDriver/StagehandDriver:
  - Usa el accessibility-tree de @playwright/mcp (browser_snapshot).
  - Los 'ref' (e5, e12) son EFIMEROS: expiran con el snapshot. NO se persisten.
  - Al GRABAR un skill, se almacena la identidad semantica del elemento:
      role + accessible_name + intent_desc (e.g. "button/'Presentar'/...")
      -> `SelectorStrategy.ACCESSIBILITY_REF` con value="role=button name=Presentar"
  - Al REPRODUCIR, se toma un snapshot fresco y se re-resuelve el ref actual
    que corresponde al role+name almacenado. El driver no asume que el ref
    del recording sigue valido.

Integracion con SelectorRegistry:
  - execute(OBSERVE)  -> devuelve candidates con strategy=ACCESSIBILITY_REF,
                         value="role=X name=Y", confidence segun unicidad.
  - execute(ACT)      -> acepta payload['mcp_ref'] (ref efimero para ese
                         snapshot) O payload['mcp_identity'] (role+name para
                         re-resolución). Si se da mcp_identity, hace snapshot
                         fresco + re-resolución antes de actuar.

Constitution IV: observe() no hace LLM inference; devuelve el arbol como
candidatos. El LLM upstream decide el selector.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from hermes.browser.application.mcp_session_port import McpSessionPort
from hermes.browser.domain.step import Step, StepKind, StepOutcome

logger = logging.getLogger(__name__)

# Patron de una linea del accessibility tree de @playwright/mcp:
# role=button name="Presentar definitivo" ref=e5
# role=link   name="Inicio"              ref=e12
_TREE_LINE_RE = re.compile(
    r"""role=(?P<role>\S+)\s+name=(?P<name>"[^"]*"|'[^']*'|\S+)\s+ref=(?P<ref>\S+)""",
    re.VERBOSE,
)


class PlaywrightMcpDriver:
    """Driver sobre @playwright/mcp. Implementa BrowserPort Protocol.

    Construccion:
        driver = PlaywrightMcpDriver(session=StdioMcpSession(...))
        await driver.start()   # abre la sesion MCP

    Para tests, inyectar FakeMcpSession (no requiere Node/npx):
        driver = PlaywrightMcpDriver(session=FakeMcpSession(...))
    """

    def __init__(
        self,
        *,
        session: McpSessionPort,
    ) -> None:
        self._session = session
        self._started = False

    @property
    def driver_name(self) -> str:
        return "playwright_mcp"

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "supports_mcp": True,
            "supports_action_caching": False,
            "supports_vision": False,
            "supports_observe": True,
        }

    async def start(self) -> None:
        """Abre la sesion MCP (lanza npx si es StdioMcpSession)."""
        await self._session.close.__func__ if False else None  # type hint aid
        # McpSessionPort.start() is not in the protocol (optional lifecycle);
        # StdioMcpSession has it — call via duck-type.
        start_fn = getattr(self._session, "start", None)
        if start_fn is not None:
            await start_fn()
        self._started = True

    async def close(self) -> None:
        try:
            await self._session.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.playwright_mcp_close_failed", extra={"error": str(exc)}
            )

    async def execute(
        self,
        step: Step,
        *,
        hitl_approval_token: str | None = None,  # noqa: ARG002
    ) -> StepOutcome:
        """Dispatch un Step al servidor MCP. Nunca propaga excepciones."""
        if not self._started:
            return StepOutcome.failed(
                step_id=step.step_id,
                error="playwright_mcp_not_started_call_start_first",
            )
        try:
            return await self._dispatch(step)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.playwright_mcp_step_failed",
                extra={
                    "step_id": str(step.step_id),
                    "kind": step.kind,
                    "error": str(exc),
                },
            )
            return StepOutcome.failed(step_id=step.step_id, error=str(exc))

    async def _dispatch(self, step: Step) -> StepOutcome:
        if step.kind == StepKind.NAVIGATE:
            return await self._navigate(step)
        if step.kind == StepKind.ACT:
            return await self._act(step)
        if step.kind == StepKind.OBSERVE:
            return await self._observe(step)
        if step.kind == StepKind.EXTRACT:
            return await self._extract(step)
        if step.kind == StepKind.SCREENSHOT:
            return await self._screenshot(step)
        if step.kind == StepKind.WAIT:
            return await self._wait(step)
        return StepOutcome.failed(
            step_id=step.step_id,
            error=f"step kind {step.kind} no implementado en PlaywrightMcpDriver",
        )

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    async def _navigate(self, step: Step) -> StepOutcome:
        url = str(step.payload.get("url", ""))
        await self._session.navigate(url)
        current = await self._session.current_url()
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"url": current or url},
        )

    async def _act(self, step: Step) -> StepOutcome:
        """Ejecuta una accion sobre un elemento.

        Acepta dos modos en el payload:
          1. mcp_ref (str): ref efimero del snapshot actual — actua directo.
          2. mcp_identity (dict): {"role": str, "name": str} — re-resuelve
             el ref tomando un snapshot fresco antes de actuar.
        """
        payload = step.payload

        if "mcp_identity" in payload:
            return await self._act_with_identity_resolution(step)

        ref = str(payload.get("mcp_ref", ""))
        if not ref:
            return StepOutcome.failed(
                step_id=step.step_id,
                error="act_requires_mcp_ref_or_mcp_identity",
            )

        action = str(payload.get("action", "click"))
        return await self._dispatch_action(step, ref=ref, action=action, payload=payload)

    async def _act_with_identity_resolution(self, step: Step) -> StepOutcome:
        """Re-resuelve el ref desde un snapshot fresco y actua."""
        identity = step.payload["mcp_identity"]
        role = str(identity.get("role", ""))
        name = str(identity.get("name", ""))

        snapshot_text = await self._session.snapshot()
        ref = _resolve_ref(snapshot_text, role=role, name=name)
        if ref is None:
            return StepOutcome.failed(
                step_id=step.step_id,
                error=f"mcp_ref_not_found_for_role={role!r}_name={name!r}",
            )

        action = str(step.payload.get("action", "click"))
        return await self._dispatch_action(step, ref=ref, action=action, payload=step.payload)

    async def _dispatch_action(
        self, step: Step, *, ref: str, action: str, payload: dict[str, Any]
    ) -> StepOutcome:
        if action == "click":
            await self._session.click(ref)
            return StepOutcome.ok(step_id=step.step_id, duration_ms=0, result={"clicked_ref": ref})

        if action == "type":
            text = str(payload.get("text", ""))
            await self._session.type_(ref, text)
            return StepOutcome.ok(
                step_id=step.step_id,
                duration_ms=0,
                result={"typed_ref": ref, "len": len(text)},
            )

        if action == "press":
            key = str(payload.get("key", "Enter"))
            await self._session.press(key)
            return StepOutcome.ok(step_id=step.step_id, duration_ms=0, result={"key": key})

        return StepOutcome.failed(
            step_id=step.step_id,
            error=f"accion '{action}' no soportada; usa click/type/press",
        )

    async def _observe(self, step: Step) -> StepOutcome:
        """Toma un snapshot y devuelve candidatos de elementos como ACCESSIBILITY_REF.

        Cada candidato tiene:
          - strategy: "accessibility_ref"
          - value: "role=X name=Y"   (identidad semantica durable, NO ref efimero)
          - confidence: float basada en unicidad del name
          - intent_desc: texto legible
          - metadata.ref: ref efimero del snapshot actual (solo para esta sesion)
        """
        snapshot_text = await self._session.snapshot()
        candidates = _parse_candidates(snapshot_text)
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"candidates": candidates},
        )

    async def _extract(self, step: Step) -> StepOutcome:
        """Extrae texto del elemento identificado por mcp_ref o mcp_identity."""
        payload = step.payload

        if "mcp_identity" in payload:
            identity = payload["mcp_identity"]
            snapshot_text = await self._session.snapshot()
            ref = _resolve_ref(
                snapshot_text,
                role=str(identity.get("role", "")),
                name=str(identity.get("name", "")),
            )
            if ref is None:
                return StepOutcome.failed(
                    step_id=step.step_id, error="extract_ref_not_found"
                )
        else:
            ref = str(payload.get("mcp_ref", ""))

        if not ref:
            return StepOutcome.failed(step_id=step.step_id, error="extract_requires_mcp_ref")

        # The MCP server doesn't expose get_text; snapshot contains all visible text.
        # We return the full snapshot text so upstream can parse.
        snapshot_text = await self._session.snapshot()
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"snapshot": snapshot_text, "ref": ref},
        )

    async def _screenshot(self, step: Step) -> StepOutcome:
        await self._session.screenshot()
        return StepOutcome.ok(step_id=step.step_id, duration_ms=0)

    async def _wait(self, step: Step) -> StepOutcome:
        # @playwright/mcp doesn't expose wait_for_load_state.
        # Treat as a no-op; caller can add explicit waits via NAVIGATE re-poll.
        return StepOutcome.ok(step_id=step.step_id, duration_ms=0, result={"waited": True})

    # ------------------------------------------------------------------
    # BrowserPort snapshot methods
    # ------------------------------------------------------------------

    async def take_screenshot(self) -> bytes:
        try:
            return await self._session.screenshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.browser.mcp_screenshot_failed", extra={"error": str(exc)})
            return b""

    async def take_dom_snapshot(self) -> str:
        try:
            return await self._session.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.browser.mcp_dom_snapshot_failed", extra={"error": str(exc)})
            return ""

    async def current_url(self) -> str:
        try:
            return await self._session.current_url()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.browser.mcp_current_url_failed", extra={"error": str(exc)})
            return ""

    # ------------------------------------------------------------------
    # Optional BrowserPort extensions (duck-type)
    # ------------------------------------------------------------------

    async def extract_storage_state(self) -> bytes:
        """@playwright/mcp no expone storage_state; devuelve JSON vacio."""
        return b"{}"

    async def attach_storage_state(self, state_bytes: bytes) -> None:  # noqa: ARG002
        """No-op: @playwright/mcp gestiona las cookies internamente."""
        logger.info(
            "hermes.browser.mcp_attach_storage_state_noop",
            extra={"size": len(state_bytes)},
        )


# ---------------------------------------------------------------------------
# Helpers: parse + resolve del accessibility tree
# ---------------------------------------------------------------------------


def _strip_quotes(s: str) -> str:
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def _parse_candidates(snapshot_text: str) -> list[dict[str, Any]]:
    """Convierte el accessibility tree en candidatos para OBSERVE.

    Cada linea tipo "role=button name="Presentar" ref=e5" produce:
      {
        "strategy": "accessibility_ref",
        "value": "role=button name=Presentar",    # identidad DURABLE
        "confidence": 0.9,
        "intent_desc": "button 'Presentar'",
        "metadata": {"ref": "e5"},               # ref EFIMERO, solo para esta sesion
      }
    """
    candidates: list[dict[str, Any]] = []
    seen_names: dict[str, int] = {}

    for line in snapshot_text.splitlines():
        m = _TREE_LINE_RE.search(line)
        if m is None:
            continue
        role = m.group("role")
        name = _strip_quotes(m.group("name"))
        ref = m.group("ref")

        seen_names[name] = seen_names.get(name, 0) + 1
        candidates.append({
            "strategy": "accessibility_ref",
            "value": f"role={role} name={name}",
            "confidence": 0.0,  # recalculated below after full scan
            "intent_desc": f"{role} '{name}'",
            "metadata": {"ref": ref},
        })

    # Assign confidence: unique name = 0.9, duplicate = 0.5
    for candidate in candidates:
        name_in_value = candidate["value"].split(" name=", 1)[1]
        candidate["confidence"] = 0.9 if seen_names.get(name_in_value, 0) == 1 else 0.5

    return candidates


def _resolve_ref(snapshot_text: str, *, role: str, name: str) -> str | None:
    """Re-resuelve el ref efimero en un snapshot fresco dado role+name.

    Busca la primera linea cuyo role y name coinciden (case-insensitive name).
    Si hay multiples coincidencias devuelve el primero y loguea warning.
    Si no hay ninguna devuelve None.

    Esta funcion es la pieza central que hace el replay determinista:
    el ReplayStep almacena role+name (durable), no el ref (efimero).
    """
    matches: list[str] = []
    name_lower = name.lower()

    for line in snapshot_text.splitlines():
        m = _TREE_LINE_RE.search(line)
        if m is None:
            continue
        line_role = m.group("role")
        line_name = _strip_quotes(m.group("name")).lower()
        if line_role == role and line_name == name_lower:
            matches.append(m.group("ref"))

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "hermes.browser.mcp_ambiguous_ref",
            extra={"role": role, "name": name, "matches": matches},
        )
    return matches[0]
