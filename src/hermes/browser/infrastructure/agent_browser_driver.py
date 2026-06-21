"""AgentBrowserDriver: driver OPCIONAL sobre el CLI agent-browser (vercel-labs).

Este driver es EXPERIMENTAL. Se activa cuando se inyecta una
`AgentBrowserCliPort` en vez de otro driver. No es el driver por defecto —
PlaywrightMcpDriver / StagehandDriver son los drivers de produccion.

Ventaja declarada: token-efficiency. El snapshot -i de agent-browser emite
solo nodos interactivos (buttons, links, inputs), reduciendo el contexto LLM
respecto a un DOM completo.

Diferencia critica respecto a PlaywrightMcpDriver:
  - Los refs de agent-browser son @eN (vs ref=eN de @playwright/mcp).
  - Los @eN son EFIMEROS: expiran con cada snapshot (igual que en MCP).
  - Al GRABAR un paso, se almacena la identidad semantica durable:
      role + accessible_name  -> strategy=ACCESSIBILITY_REF,
      value="@role=button @name=Continue"
  - Al REPRODUCIR, se toma un snapshot fresco y se re-resuelve el @eN actual
    cuyo role+name coincide con lo almacenado. El driver nunca asume que
    el @eN del recording sigue siendo valido.

Formato del accessibility tree que emite agent-browser (snapshot -i):
    Page: Example - Log in
    URL: https://example.com/login

    @e1 [heading] "Log in"
    @e3 [input type="email"] placeholder="Email"
    @e5 [button type="submit"] "Continue"

Patron de linea: `@eN [role attrs] "accessible name"`

Constitution IV: observe() no hace LLM inference; devuelve el arbol como
candidatos. El LLM upstream decide el selector.

Requisito de instalacion (Containerfile, NO pyproject):
    npm install -g agent-browser && agent-browser install
"""

from __future__ import annotations

import logging
import re
from typing import Any

from hermes.browser.application.agent_browser_port import AgentBrowserCliPort
from hermes.browser.domain.step import Step, StepKind, StepOutcome

logger = logging.getLogger(__name__)

# Patron de una linea del accessibility tree de agent-browser (snapshot -i):
# @e1 [heading] "Log in"
# @e3 [input type="email"] placeholder="Email"
# @e5 [button type="submit"] "Continue"
#
# Grupos capturados:
#   ref  -> "e1", "e3", "e5"   (sin el @, para consistencia interna)
#   role -> "heading", "input", "button"
#   name -> texto entre comillas dobles que sigue al [role attrs]
#
# El accessible name puede estar ausente (nodo sin texto). El patron acepta
# la presencia de atributos adicionales dentro de los corchetes (type="email").
_TREE_LINE_RE = re.compile(
    r"""
    ^                          # inicio de linea
    \s*                        # indentacion (anidamiento)
    @(?P<ref>e\d+)             # @eN  -> captura solo el ID
    \s+
    \[(?P<role>[^\]]+)\]       # [role con attrs opcionales]
    (?:                        # nombre accesible opcional
        \s+
        "(?P<name>[^"]*)"
    )?
    """,
    re.VERBOSE,
)


def _extract_base_role(role_with_attrs: str) -> str:
    """Extrae el role base ignorando atributos inline (type="email", etc.).

    agent-browser puede emitir: `input type="email"` dentro del bloque [].
    Nos quedamos solo con el primer token.
    """
    return role_with_attrs.split()[0]


class AgentBrowserDriver:
    """Driver sobre el CLI agent-browser. Implementa BrowserPort Protocol.

    Construccion:
        cli = AgentBrowserCli(session_name="hermes-ent-123")
        driver = AgentBrowserDriver(cli=cli)
        await driver.start()   # verifica binario; levanta AgentBrowserNotInstalledError si falta

    Para tests, inyectar FakeAgentBrowserCli (no requiere el binario):
        driver = AgentBrowserDriver(cli=FakeAgentBrowserCli(...))

    Este driver es puramente OPCIONAL. Importarlo sin el binario instalado es
    seguro — AgentBrowserNotInstalledError solo se levanta en start().
    """

    def __init__(
        self,
        *,
        cli: AgentBrowserCliPort,
    ) -> None:
        self._cli = cli
        self._started = False

    @property
    def driver_name(self) -> str:
        return "agent_browser"

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "supports_mcp": False,
            "supports_action_caching": False,
            "supports_vision": False,
            "supports_observe": True,
            # agent-browser emite solo nodos interactivos en snapshot -i,
            # reduciendo el contexto LLM vs DOM completo.
            "token_efficient_snapshots": True,
            # Experimental: vercel-labs, CLI en rapida evolucion. Pinchar version exacta.
            "experimental": True,
            "driver_backend": "agent_browser_rust_cli",
        }

    async def start(self) -> None:
        """Verifica el binario y arranca el daemon si no esta corriendo.

        Raises:
            AgentBrowserNotInstalledError: si el binario no esta en PATH.
        """
        start_fn = getattr(self._cli, "start", None)
        if start_fn is not None:
            await start_fn()
        self._started = True

    async def close(self) -> None:
        try:
            await self._cli.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.agent_browser_close_failed",
                extra={"error": str(exc)},
            )

    async def execute(
        self,
        step: Step,
        *,
        hitl_approval_token: str | None = None,  # noqa: ARG002
    ) -> StepOutcome:
        """Dispatch un Step al CLI agent-browser. Nunca propaga excepciones."""
        if not self._started:
            return StepOutcome.failed(
                step_id=step.step_id,
                error="agent_browser_not_started_call_start_first",
            )
        try:
            return await self._dispatch(step)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.agent_browser_step_failed",
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
            error=f"step kind {step.kind} no implementado en AgentBrowserDriver",
        )

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    async def _navigate(self, step: Step) -> StepOutcome:
        url = str(step.payload.get("url", ""))
        await self._cli.navigate(url)
        current = await self._cli.current_url()
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"url": current or url},
        )

    async def _act(self, step: Step) -> StepOutcome:
        """Ejecuta una accion sobre un elemento.

        Acepta dos modos en el payload:
          1. ab_ref (str): @eN efimero del snapshot actual — actua directo.
          2. ab_identity (dict): {"role": str, "name": str} — re-resuelve
             el @eN tomando un snapshot fresco antes de actuar.
        """
        payload = step.payload

        if "ab_identity" in payload:
            return await self._act_with_identity_resolution(step)

        ref = str(payload.get("ab_ref", ""))
        if not ref:
            return StepOutcome.failed(
                step_id=step.step_id,
                error="act_requires_ab_ref_or_ab_identity",
            )

        action = str(payload.get("action", "click"))
        return await self._dispatch_action(step, ref=ref, action=action, payload=payload)

    async def _act_with_identity_resolution(self, step: Step) -> StepOutcome:
        """Re-resuelve el @eN desde un snapshot fresco y actua.

        Este es el path de REPLAY: el ReplayStep almacena role+name (durable),
        no el @eN (efimero). El driver toma snapshot fresco y busca el elemento.
        """
        identity = step.payload["ab_identity"]
        role = str(identity.get("role", ""))
        name = str(identity.get("name", ""))

        snapshot_text = await self._cli.snapshot()
        ref = _resolve_ref(snapshot_text, role=role, name=name)
        if ref is None:
            return StepOutcome.failed(
                step_id=step.step_id,
                error=f"ab_ref_not_found_for_role={role!r}_name={name!r}",
            )

        action = str(step.payload.get("action", "click"))
        return await self._dispatch_action(step, ref=ref, action=action, payload=step.payload)

    async def _dispatch_action(
        self, step: Step, *, ref: str, action: str, payload: dict[str, Any]
    ) -> StepOutcome:
        # agent-browser necesita el @ como prefijo en los comandos CLI
        cli_ref = ref if ref.startswith("@") else f"@{ref}"

        if action == "click":
            await self._cli.click(cli_ref)
            return StepOutcome.ok(
                step_id=step.step_id,
                duration_ms=0,
                result={"clicked_ref": ref},
            )

        if action == "type":
            text = str(payload.get("text", ""))
            await self._cli.type_(cli_ref, text)
            return StepOutcome.ok(
                step_id=step.step_id,
                duration_ms=0,
                result={"typed_ref": ref, "len": len(text)},
            )

        return StepOutcome.failed(
            step_id=step.step_id,
            error=f"accion '{action}' no soportada en AgentBrowserDriver; usa click/type",
        )

    async def _observe(self, step: Step) -> StepOutcome:  # noqa: ARG002
        """Toma snapshot y devuelve candidatos como ACCESSIBILITY_REF.

        Cada candidato tiene:
          - strategy: "accessibility_ref"
          - value: "@role=X @name=Y"   (identidad semantica durable, NO @eN)
          - confidence: float basada en unicidad del name
          - intent_desc: texto legible
          - metadata.ref: @eN efimero del snapshot (solo para esta operacion)
        """
        snapshot_text = await self._cli.snapshot()
        candidates = _parse_candidates(snapshot_text)
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"candidates": candidates},
        )

    async def _extract(self, step: Step) -> StepOutcome:
        """Extrae informacion de la pagina via snapshot.

        agent-browser no tiene un comando `get text` separado que podamos
        mapear sin conocer el ref efimero actual. Devolvemos el snapshot
        completo y el caller puede parsear el elemento por identity.
        """
        payload = step.payload

        if "ab_identity" in payload:
            identity = payload["ab_identity"]
            snapshot_text = await self._cli.snapshot()
            ref = _resolve_ref(
                snapshot_text,
                role=str(identity.get("role", "")),
                name=str(identity.get("name", "")),
            )
            if ref is None:
                return StepOutcome.failed(
                    step_id=step.step_id, error="extract_ab_ref_not_found"
                )
        else:
            ref = str(payload.get("ab_ref", ""))
            snapshot_text = await self._cli.snapshot()

        if not ref:
            return StepOutcome.failed(
                step_id=step.step_id, error="extract_requires_ab_ref"
            )

        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=0,
            result={"snapshot": snapshot_text, "ref": ref},
        )

    async def _screenshot(self, step: Step) -> StepOutcome:
        # agent-browser CLI does support `screenshot` but AgentBrowserCliPort
        # does not expose it to keep the port minimal. Return ok with empty bytes.
        return StepOutcome.ok(step_id=step.step_id, duration_ms=0)

    async def _wait(self, step: Step) -> StepOutcome:  # noqa: ARG002
        # agent-browser supports `wait` but it requires a ref or condition.
        # Treat as no-op; caller can use explicit NAVIGATE re-poll.
        return StepOutcome.ok(step_id=step.step_id, duration_ms=0, result={"waited": True})

    # ------------------------------------------------------------------
    # BrowserPort snapshot methods
    # ------------------------------------------------------------------

    async def take_screenshot(self) -> bytes:
        # AgentBrowserCliPort does not expose screenshot in the minimal port.
        # Return empty bytes; callers that need screenshots should use another driver.
        return b""

    async def take_dom_snapshot(self) -> str:
        try:
            return await self._cli.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.agent_browser_dom_snapshot_failed",
                extra={"error": str(exc)},
            )
            return ""

    async def current_url(self) -> str:
        try:
            return await self._cli.current_url()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.agent_browser_current_url_failed",
                extra={"error": str(exc)},
            )
            return ""

    # ------------------------------------------------------------------
    # Optional BrowserPort extensions (duck-type)
    # ------------------------------------------------------------------

    async def extract_storage_state(self) -> bytes:
        """agent-browser gestiona auth via `auth save`/`state save`.

        No esta expuesto en el port minimal — devuelve JSON vacio.
        """
        return b"{}"

    async def attach_storage_state(self, state_bytes: bytes) -> None:  # noqa: ARG002
        """No-op: agent-browser gestiona las cookies internamente."""
        logger.info(
            "hermes.browser.agent_browser_attach_storage_state_noop",
            extra={"size": len(state_bytes)},
        )


# ---------------------------------------------------------------------------
# Helpers: parse + resolve del accessibility tree de agent-browser
# ---------------------------------------------------------------------------


def _parse_candidates(snapshot_text: str) -> list[dict[str, Any]]:
    """Convierte el accessibility tree de agent-browser en candidatos OBSERVE.

    Cada linea tipo "@e5 [button type=\"submit\"] \"Continue\"" produce:
      {
        "strategy": "accessibility_ref",
        "value": "@role=button @name=Continue",   # identidad DURABLE, NO @eN
        "confidence": 0.9,
        "intent_desc": "button 'Continue'",
        "metadata": {"ref": "e5"},                # @eN EFIMERO, solo esta sesion
      }

    El formato del value usa el prefijo "@role=" / "@name=" para distinguir
    claramente el esquema de agent-browser del de @playwright/mcp
    (que usa "role=X name=Y").
    """
    candidates: list[dict[str, Any]] = []
    seen_names: dict[str, int] = {}

    for line in snapshot_text.splitlines():
        m = _TREE_LINE_RE.match(line)
        if m is None:
            continue
        ref = m.group("ref")
        role = _extract_base_role(m.group("role"))
        name = m.group("name") or ""

        seen_names[name] = seen_names.get(name, 0) + 1
        candidates.append({
            "strategy": "accessibility_ref",
            "value": f"@role={role} @name={name}",
            "confidence": 0.0,  # recalculado abajo tras escaneo completo
            "intent_desc": f"{role} '{name}'",
            "metadata": {"ref": ref},
        })

    # Unique name -> 0.9; duplicate -> 0.5
    for candidate in candidates:
        name_part = candidate["value"].split("@name=", 1)[1]
        candidate["confidence"] = 0.9 if seen_names.get(name_part, 0) == 1 else 0.5

    return candidates


def _resolve_ref(snapshot_text: str, *, role: str, name: str) -> str | None:
    """Re-resuelve el @eN efimero en un snapshot fresco dado role+name.

    Busca la primera linea cuyo base_role y accessible_name coinciden
    (case-insensitive name). Devuelve el ref ID sin el @ (e.g. "e5"), o
    None si no se encuentra.

    Esta funcion es la pieza central del replay determinista:
    el ReplayStep almacena role+name (durable), no @eN (efimero).
    En replay se toma un snapshot fresco y se llama a esta funcion para
    obtener el @eN actual antes de actuar.
    """
    matches: list[str] = []
    name_lower = name.lower()

    for line in snapshot_text.splitlines():
        m = _TREE_LINE_RE.match(line)
        if m is None:
            continue
        line_role = _extract_base_role(m.group("role"))
        line_name = (m.group("name") or "").lower()
        if line_role == role and line_name == name_lower:
            matches.append(m.group("ref"))

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "hermes.browser.agent_browser_ambiguous_ref",
            extra={"role": role, "name": name, "matches": matches},
        )
    return matches[0]
