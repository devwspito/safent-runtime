"""CapturingToolHost: clasifica las tool_calls del LLM en {execute, propose, malformed}.

Diseno (vs NousResearch hermes-agent que usaba hook pre_tool_call):
  - Recibimos una lista de tool_calls al estilo OpenAI/LiteLLM (estructura
    `{id, function: {name, arguments_json}}`).
  - Por cada tool call:
      * Si NO existe en el registry  -> malformed (warning, no executa nada).
      * Si la tool es READ_ONLY      -> Hermes la EJECUTA (handler async).
      * Si la tool es WRITE_*        -> NO ejecuta. Captura como ToolCallProposal.
  - Devuelve:
      * proposals: a propuestas para HITL del consumer.
      * tool_results: resultados de las READ_ONLY tools (se devuelven al LLM
        en el siguiente turn del loop).
      * malformed:  tool calls que no se pudieron procesar.

Garantia: Hermes NUNCA invoca un handler de WRITE_*. El handler de WRITE_* es
None por construccion (ToolSpec.__post_init__).

CTRL-5 / TOP-1 — Taint de procedencia:
  Cualquier tool READ_ONLY que ingiera contenido externo no confiable (web,
  Composio, fichero fuera del allowlist de confianza) activa
  CapturedRound.ingested_untrusted_content=True. El motor lo propaga a
  CycleOutput.read_external_content para que el orchestrator taintee el
  ConsentContext de TODAS las proposals del ciclo.

Rutas que marcan untrusted (allow-list de confianza en negativo):
  - Tags "composio"  → contenido de internet, siempre untrusted.
  - Tags "browser"   → snapshot/read_url, siempre untrusted.
  - read_file con path fuera de _TRUSTED_PATH_PREFIXES → untrusted.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from hermes.domain.proposal import ToolCallProposal
from hermes.domain.tool_spec import ToolRisk, ToolSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allow-list de rutas de fichero que se consideran CONFIABLES (operador).
# Cualquier read_file cuyo path NO empiece por uno de estos prefijos se
# trata como untrusted (activa el taint del ciclo).
# Justificación conservadora:
#   /etc/hermes/         → config del operador bakeada en imagen.
#   /etc/systemd/        → units del SO (read-only post-bootc).
# NO incluidos (untrusted por defecto):
#   /home/               → directorio de usuario; puede contener payloads LLM.
#   /var/lib/hermes/     → credenciales, skills, datos del agente.
#   /tmp/                → mundo externo puede escribir aquí.
#   Cualquier otro path  → conservador.
# ---------------------------------------------------------------------------
_TRUSTED_PATH_PREFIXES: frozenset[str] = frozenset(
    {"/etc/hermes/", "/etc/systemd/"}
)


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    """Mensaje a inyectar de vuelta al LLM con el resultado de una READ_ONLY tool."""

    tool_call_id: str
    name: str
    content: str  # JSON-serializado del resultado del handler


@dataclass(frozen=True, slots=True)
class CapturedRound:
    """Resultado de procesar una ronda de tool_calls del LLM."""

    proposals: tuple[ToolCallProposal, ...] = ()
    tool_results: tuple[ToolResultMessage, ...] = ()
    malformed: tuple[dict[str, Any], ...] = ()
    # CTRL-5: True si al menos una READ_ONLY de esta ronda ingirió contenido
    # externo no confiable. El motor lo acumula en CycleOutput.read_external_content.
    ingested_untrusted_content: bool = False


class CapturingToolHost:
    """Host de tools: ejecuta READ_ONLY, captura WRITE_* como propuestas.

    Construccion:
        host = CapturingToolHost(
            specs=tuple_of_ToolSpec,
            tenant_id=context.tenant_id,
        )

    Uso en cada iteracion del loop LiteLLM:
        round_result = await host.process_round(tool_calls_from_llm)
        # proposals -> a CycleOutput. tool_results -> a `messages=[...]` siguiente turn.
    """

    def __init__(
        self,
        *,
        specs: tuple[ToolSpec, ...],
        tenant_id: UUID,
    ) -> None:
        if not specs:
            raise ValueError("CapturingToolHost: specs cannot be empty")
        seen: set[str] = set()
        for spec in specs:
            if spec.name in seen:
                raise ValueError(f"duplicate tool name: {spec.name!r}")
            seen.add(spec.name)
        self._specs_by_name: dict[str, ToolSpec] = {spec.name: spec for spec in specs}
        self._tenant_id = tenant_id

    @property
    def openai_function_specs(self) -> list[dict[str, Any]]:
        """Tools serializadas para `litellm.acompletion(tools=...)`."""
        return [spec.to_openai_function() for spec in self._specs_by_name.values()]

    async def process_round(
        self, tool_calls: list[Mapping[str, Any]]
    ) -> CapturedRound:
        """Procesa una lista de tool_calls (formato OpenAI/LiteLLM)."""
        proposals: list[ToolCallProposal] = []
        results: list[ToolResultMessage] = []
        malformed: list[dict[str, Any]] = []
        round_untrusted = False

        for call in tool_calls:
            parsed = _parse_call(call)
            if parsed is None:
                malformed.append({"raw": dict(call), "reason": "parse_error"})
                continue
            call_id, tool_name, args = parsed

            spec = self._specs_by_name.get(tool_name)
            if spec is None:
                malformed.append(
                    {"call_id": call_id, "tool_name": tool_name, "reason": "unknown_tool"}
                )
                continue

            if spec.risk == ToolRisk.READ_ONLY:
                result, is_untrusted = await self._execute_read(spec, args)
                if is_untrusted:
                    round_untrusted = True
                _content = _safe_json(result)
                if _EXTERNAL_READ_TAGS & set(spec.tags):
                    _content = _cap_external_result(_content, tool_name)
                results.append(
                    ToolResultMessage(
                        tool_call_id=call_id, name=tool_name, content=_content
                    )
                )
            else:
                proposal = self._capture_write(spec, args)
                if proposal is None:
                    malformed.append(
                        {
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "reason": "missing_entity_id",
                            "args": args,
                        }
                    )
                else:
                    proposals.append(proposal)
                    logger.info(
                        "hermes.write_tool_captured",
                        extra={
                            "tool_name": tool_name,
                            "entity_id": proposal.entity_id,
                            "entity_type": proposal.entity_type,
                            "tenant_id": str(self._tenant_id),
                        },
                    )

        return CapturedRound(
            proposals=tuple(proposals),
            tool_results=tuple(results),
            malformed=tuple(malformed),
            ingested_untrusted_content=round_untrusted,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _execute_read(
        self, spec: ToolSpec, args: dict[str, Any]
    ) -> tuple[Any, bool]:
        """Ejecuta el handler READ_ONLY. Returns (result, is_untrusted).

        is_untrusted=True cuando el resultado ingiere contenido externo no
        confiable (web, Composio, fichero fuera del allowlist de confianza).
        El caller acumula este flag en CapturedRound.ingested_untrusted_content.
        """
        assert spec.handler is not None  # noqa: S101  (invariante de ToolSpec.__post_init__)
        try:
            result = await spec.handler(args)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.read_tool_failed",
                extra={"tool_name": spec.name, "error": str(exc)},
            )
            return {"error": "tool_handler_failed", "tool_name": spec.name}, False

        is_untrusted = _is_untrusted_read(spec, args)
        return result, is_untrusted

    def _capture_write(
        self, spec: ToolSpec, args: dict[str, Any]
    ) -> ToolCallProposal | None:
        entity_id = str(args.get("entity_id", "")).strip()
        # OS-native tools (entity_type=="os_surface") don't expose entity_id in
        # their schema — the LLM never supplies one. Use a stable sentinel so
        # the WRITE_PROPOSAL path is reachable and the HITL/consent flow is
        # exercised (finding #13).
        if not entity_id:
            fallback_entity_type = str(spec.entity_type or "").strip()
            if fallback_entity_type == "os_surface":
                entity_id = "os_surface"
            else:
                return None
        entity_type = str(args.get("entity_type", spec.entity_type) or "").strip()
        if not entity_type:
            return None
        justification = str(args.get("justification", ""))
        return ToolCallProposal(
            proposal_id=uuid4(),
            tool_name=spec.name,
            tenant_id=self._tenant_id,
            entity_id=entity_id,
            entity_type=entity_type,
            parameters=dict(args),
            justification=justification,
        )


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _parse_call(call: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """Extrae (call_id, tool_name, arguments_dict) de un OpenAI tool_call."""
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or not isinstance(function, Mapping):
        return None
    name = function.get("name")
    arguments_raw = function.get("arguments")
    if not isinstance(name, str) or not name:
        return None
    args: dict[str, Any] = {}
    if isinstance(arguments_raw, str):
        try:
            parsed = json.loads(arguments_raw)
            if isinstance(parsed, dict):
                args = parsed
        except json.JSONDecodeError:
            return None
    elif isinstance(arguments_raw, Mapping):
        args = dict(arguments_raw)
    return call_id, name, args


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"_serialize_error": True})


# Token-safe cap for EXTERNAL read results (composio / mcp / browser). These
# tools return arbitrarily large, token-DENSE JSON (e.g. gmail_fetch_emails can
# be ~85 KB ≈ >65 K tokens on a 65 K-context model). Admitted verbatim, the next
# model call 400s ("prompt exceeds context") and CANNOT be compressed — the single
# tool message already blows the window — so the task fails and RETRIES in a loop
# with no reply. We cap the SERIALIZED result here: the one choke point before it
# enters the message context. Dense JSON tokenizes at ~1.5 chars/token (not the
# 4:1 prose ratio), so we size pessimistically for the smallest supported window.
# Overridable so larger-context deployments can raise it.
_EXTERNAL_READ_MAX_CHARS: int = int(os.environ.get("HERMES_EXTERNAL_READ_MAX_CHARS", "24000"))
_EXTERNAL_READ_TAGS: frozenset[str] = frozenset({"composio", "mcp", "browser"})


def _cap_external_result(payload: str, tool_name: str) -> str:
    """Cap a serialized EXTERNAL read result to a token-safe size.

    Truncation is on the SERIALIZED string (never on the object) so any result
    shape is handled uniformly. A machine- and LLM-legible marker is appended so
    the model KNOWS it was reduced and can re-query with a smaller page/filter.
    """
    if len(payload) <= _EXTERNAL_READ_MAX_CHARS:
        return payload
    marker = (
        f'\n\n[TRUNCATED: "{tool_name}" returned {len(payload)} chars; kept the '
        f"first {_EXTERNAL_READ_MAX_CHARS} to fit the model context. Re-query with "
        "a smaller page size / tighter filter (e.g. max_results, ids, a query) "
        "for the rest.]"
    )
    logger.warning(
        "hermes.tool_host.external_result_capped tool=%s original_chars=%d cap=%d",
        tool_name, len(payload), _EXTERNAL_READ_MAX_CHARS,
    )
    return payload[:_EXTERNAL_READ_MAX_CHARS] + marker


def _is_untrusted_read(spec: ToolSpec, args: dict[str, Any]) -> bool:
    """True si esta tool READ ingiere contenido externo no confiable.

    Reglas (allow-list de confianza en negativo — conservador):
      1. Tag "composio" → siempre untrusted (proviene de internet vía API).
      2. Tag "browser"  → siempre untrusted (snapshot/read_url de web abierta).
      3. Tag "mcp"      → siempre untrusted. Un servidor MCP es una integración
         externa que devuelve contenido arbitrario (de su API/servidor, fuera de
         nuestro control); un MCP malicioso o comprometido puede inyectar
         instrucciones en su salida. Sin esto, actuar sobre la salida de un MCP NO
         tainteaba el ciclo → una acción HIGH derivada se auto-ejecutaba sin HITL
         (tool-poisoning / prompt-injection vía MCP). Mismo trato que "composio".
         (red-team 2026-06-19.)
      4. "read_file" con path fuera de _TRUSTED_PATH_PREFIXES → untrusted.
    Todo lo demás → confiable (tools nativas del SO que leen config local).
    """
    tags = set(spec.tags)
    if "composio" in tags or "browser" in tags or "mcp" in tags:
        return True

    if spec.name == "read_file":
        path = str(args.get("path", ""))
        return not _path_is_trusted(path)

    return False


def _path_is_trusted(path: str) -> bool:
    """True si el path pertenece al allowlist de rutas confiables de operador."""
    if not path:
        return False
    try:
        normalized = str(PurePosixPath(path))
    except Exception:  # noqa: BLE001
        return False
    return any(normalized.startswith(prefix) for prefix in _TRUSTED_PATH_PREFIXES)
