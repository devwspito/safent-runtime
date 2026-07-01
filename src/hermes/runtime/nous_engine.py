"""NousReasoningEngine: motor agentico Hermes delegando en hermes-agent (NousResearch).

Adapta el Protocol ReasoningEngine sobre AIAgent de NousResearch v0.15.1.

Seams de intercepción (THREE paths, one gate each):
  GovernedAIAgent intercepta en TRES puntos:

  PATH A — CONCURRENT (≥2 tool_calls):
    agent/tool_executor.py::execute_tool_calls_concurrent → agent._invoke_tool
    → GovernedAIAgent._invoke_tool → broker.dispatch (WRITE) o nativo (READ).
    Gate: _invoke_tool. Fires ONCE. Covers native + external tools.

  PATH B — SEQUENTIAL REGISTRY (=1 tool_call, quiet_mode=True → dominant case):
    agent/tool_executor.py::execute_tool_calls_sequential
    → _ra().handle_function_call → model_tools.handle_function_call
    → tools.registry.dispatch → handler.
    Gate: registry wrapper installed by:
      - _wire_sequential_gate(): native WRITE tools (write_file, terminal, …)
      - _register_external_specs_in_nous(): external Composio/MCP tools
    Both sets now have broker-dispatching wrappers. Fires ONCE per tool.

  PATH C — INLINE BRANCH (memory, clarify — intercepted BEFORE registry):
    agent/tool_executor.py::execute_tool_calls_sequential has hardcoded
    if/elif branches for memory/clarify/todo/delegate_task that call the
    handler function DIRECTLY before handle_function_call/registry.dispatch.
    Gate: _wire_inline_branch_gates() monkeypatches tools.memory_tool.memory_tool
    and tools.clarify_tool.clarify_tool at GovernedAIAgent build time.
    todo/delegate_task: documented residuals (see _wire_inline_branch_gates).

  Paths A and B are MUTUALLY EXCLUSIVE (selector: len(tool_calls)<=1 →
  sequential; else concurrent). Path C fires before the B selector runs.
  All three paths reach the broker EXACTLY ONCE per effectful tool call.

F2: _invoke_tool implementado con 3 caminos:
  READ  → ejecuta handler del ToolSpec (broker-dispatching) o nativo Nous.
  WRITE → captura ToolCallProposal + dispatch al broker.
  UNKNOWN → fail-closed BLOCKED.

F3 — DISCOVERY (Composio + MCP):
    Per-cycle, ToolSpecs externos (Composio + MCP) son resueltos desde
    tools_source y registrados en el Nous tools.registry (toolset "composio"
    / "mcp-<slug>"). El handler de cada ToolSpec despacha a través del broker.
    classify_nous_tool() se consulta primero; si devuelve None se busca en el
    catálogo externo (_ExternalToolCatalog). Esto asegura:
      1. Única fuente de gate = CapabilityBroker (sin double-gate ni bypass).
      2. Nous no usa su MCP nativo (mcp_tool.py) — TODOS los MCP pasan por
         nuestro McpCapabilityRegistry + McpSurfaceAdapter + broker.
      3. LLM recibe el schema de las tools externas en function-calling.
      4. El sequential path para tools EXTERNAS también despacha al broker
         vía _register_external_specs_in_nous (registro con wrapper broker-aware).

Import lazy obligatorio: hermes-agent NO se importa a nivel de módulo.
El repositorio importa y los tests pasan sin hermes-agent instalado.

Activación:
    HERMES_ENGINE=nous python3 -m hermes.runtime

HERMES_YOLO_MODE:
    Desactiva el gate de Nous (tools.approval._YOLO_MODE_FROZEN). DEBE
    estar en os.environ ANTES de que tools.approval se importe. Lo seteamos
    al construir GovernedAIAgent porque nuestro broker (F2) es el único gate
    real para WRITEs. El broker no lee YOLO_MODE; es un gate propio.

PUENTE ASYNC (F2):
    run_conversation de Nous es SÍNCRONO y se corre en loop.run_in_executor.
    _tool_gate se ejecuta en ese hilo de executor. Para llamar al broker
    (coroutine) desde ahí se usa asyncio.run_coroutine_threadsafe con el
    event loop principal. El broker devuelve PENDING_APPROVAL sin bloquear
    el HITL (el humano no está en el bucle de espera), por lo que esto es
    seguro y no congela el event loop principal.

PROPOSALS PENDIENTES:
    Las proposals que vuelven PENDING_APPROVAL se acumulan en
    GovernedAIAgent._pending_proposals durante run_conversation. Después,
    _map_result_to_output las extrae y las pone en CycleOutput.tool_call_proposals
    para que el AgentLoopOrchestrator las encole en el buzón de aprobaciones
    y las re-dispatche tras aprobación humana. Las EXECUTED/REJECTED están ya
    resueltas in-loop y NO se re-surfacean.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from hermes.domain.cycle_output import CycleOutput, TokenUsage
from hermes.domain.decision_context import DecisionContext
from hermes.domain.proposal import ToolCallProposal
from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.prompts.builder import DefaultPromptBuilder, PromptBuilder, _sanitize_untrusted
from hermes.prompts.persona import PersonaSpec
from hermes.runtime.conversation_task_registry import (
    bump_write_tool_failure,
    resolve_conversation,
    write_tool_failure_count,
)

# Circuit breaker for broker-routed gated tools (install_mcp/skill_manage/...): after
# this many failures of the SAME tool in one cycle, refuse to re-propose it (each
# retry would otherwise mint a fresh HITL card). Stops the "retry-spam" and lets the
# turn end so the chat message finalizes instead of streaming forever.
_MAX_WRITE_TOOL_FAILURES = 5


def _write_result_is_failure(result: str) -> bool:
    """True if a write-tool result string signals failure (rejected / error / not-ok)."""
    if not result:
        return False
    low = result[:600].lower()
    return (
        '"error"' in low
        or '"success": false' in low
        or '"success":false' in low
        or "blocked:" in low
        or result.startswith("Error")
    )


def _write_circuit_broken_msg(tool_name: str, count: int) -> str:
    return json.dumps(
        {
            "error": (
                f"BLOQUEADO: '{tool_name}' ya falló {count} veces en este turno. "
                "NO lo reintentes (ni con los mismos ni con otros argumentos): "
                "explícale al usuario con honestidad qué falla y qué necesitas, o propón otra vía."
            )
        },
        ensure_ascii=False,
    )
from hermes.runtime.cycle_cdp_context import cerebro_cdp_scope, install_thread_local_cdp_override
from hermes.runtime.model_config import ModelConfig, _replace_context
from hermes.runtime.nous_tool_risk_map import NousRisk, classify_nous_tool
from hermes.tokenizer.pii import DefaultPIITokenizer, PIITokenizer, UnknownPlaceholderError

if TYPE_CHECKING:
    from hermes.capabilities.domain.ports import (
        CapabilityBrokerPort,
        ConsentContext,
        ExecutionStatus,
    )

logger = logging.getLogger(__name__)

# Timeout en segundos para el puente async broker.dispatch desde el hilo executor.
# El broker devuelve PENDING_APPROVAL sin esperar al humano → timeout generoso
# pero acotado para detectar deadlocks.
_BROKER_DISPATCH_TIMEOUT_S: float = 30.0

# SECURITY (red-team 2026-06-19): native WRITE tools whose EXECUTION must be delegated
# to the exec-launcher cage (hermes-sandbox + netns egress jail + InaccessiblePaths)
# instead of running subprocess IN the daemon process. "terminal" is THE one: it is
# where the agent works, and natively it ran in-daemon (owns master.key, host netns).
# Native Nous exec/file tools that run IN-DAEMON (User=hermes, owns master.key, host
# netns) unless caged. RED-TEAM 2026-06-19: caging ONLY "terminal" left execute_code/
# process + read_file/search_files/write_file/patch as in-daemon backdoors; ALL route
# through the cage now (OpenShell ExecSandbox → uid 999, landlock, egress proxy; or the
# exec-launcher fallback). The set is the SINGLE SOURCE in tool_delicacy (also consumed
# by the Policies catalog) — imported here, never re-listed. Names match the native
# nous_tool_risk_map exactly (an unclassified name like "shell" → default-deny, not cage).
from hermes.capabilities.tool_delicacy import (  # noqa: E402
    CAGED_NATIVE_EXEC_TOOLS as _CAGED_NATIVE_EXEC_TOOLS,
    CAGED_NATIVE_FILE_TOOLS as _CAGED_NATIVE_FILE_TOOLS,
    CAGED_NATIVE_TOOLS as _CAGED_NATIVE_TOOLS,
)
_CAGED_TERMINAL_TIMEOUT_S: int = 60
_CAGED_TERMINAL_FRAME_MAX: int = 2 * 1024 * 1024


def _build_caged_command(function_name: str, function_args: dict[str, Any]) -> str | None:
    """Map a native exec/file tool call to a single shell command run INSIDE the cage.

    Code and file contents are base64-wrapped so arbitrary payloads survive intact
    through `bash -lc` with zero quoting/injection surface. Returns None when the
    args don't yield a runnable operation (→ caller DENIES, fail-closed).
    """
    import base64 as _b64  # noqa: PLC0415
    import shlex as _shlex  # noqa: PLC0415

    def _b(s: str) -> str:
        return _b64.b64encode(s.encode("utf-8")).decode("ascii")

    def _path() -> str:
        for k in ("path", "filename", "file"):
            v = function_args.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return ""

    if function_name in ("terminal", "process"):
        cmd = function_args.get("command") or function_args.get("cmd") or ""
        return cmd if isinstance(cmd, str) and cmd.strip() else None
    if function_name == "execute_code":
        code = function_args.get("code") or function_args.get("command") or ""
        if not (isinstance(code, str) and code.strip()):
            return None
        lang = str(function_args.get("language") or "python").lower()
        interp = "python3" if lang.startswith("py") else "bash"
        return f"printf %s {_b(code)} | base64 -d | {interp}"
    if function_name == "search_files":
        pattern = (
            function_args.get("query") or function_args.get("pattern")
            or function_args.get("q") or function_args.get("search") or ""
        )
        if not (isinstance(pattern, str) and pattern.strip()):
            return None
        where = function_args.get("path") or function_args.get("dir") or "."
        # grep INSIDE the sandbox: matches only sandbox files; the daemon's
        # secrets are not on this filesystem, so they can never appear in results.
        return f"grep -rIn -e {_shlex.quote(pattern)} -- {_shlex.quote(str(where))}"
    path = _path()
    if not path:
        return None
    if function_name == "read_file":
        return f"cat -- {_shlex.quote(path)}"
    if function_name == "write_file":
        content = (
            function_args.get("content") or function_args.get("text")
            or function_args.get("contents") or function_args.get("data") or ""
        )
        if not isinstance(content, str):
            content = str(content)
        return f"printf %s {_b(content)} | base64 -d > {_shlex.quote(path)}"
    if function_name == "patch":
        diff = (
            function_args.get("patch") or function_args.get("diff")
            or function_args.get("content") or ""
        )
        if not (isinstance(diff, str) and diff.strip()):
            return None
        return f"printf %s {_b(diff)} | base64 -d | patch -p0"
    return None

# OpenShell substrate (P2): when HERMES_OPENSHELL_SANDBOX names the per-agent
# sandbox, the terminal routes through `openshell sandbox exec` (uid 999, landlock,
# egress proxy) instead of the exec-launcher. Unset → exec-launcher fallback. Both
# fail-closed.
_OPENSHELL_SANDBOX_ENV: str = "HERMES_OPENSHELL_SANDBOX"
_OPENSHELL_CLI_DEFAULT: str = "/usr/libexec/hermes/openshell"
_OPENSHELL_HOME_DEFAULT: str = "/var/lib/hermes/openshell"
_OPENSHELL_LIB_DIR: str = "/usr/lib/hermes/openshell"


def _resolve_agent_workspace() -> str:
    """The writable dir the caged terminal may use (exec-launcher ReadWritePaths)."""
    raw = os.environ.get("HERMES_FS_ALLOWLIST", "").strip()
    if raw:
        first = raw.split(",")[0].strip()
        if first:
            return first
    return "/var/lib/hermes/workspace"


def _recv_exactly_sync(sock: Any, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("exec-launcher closed connection early")
        buf += chunk
    return buf

# ---------------------------------------------------------------------------
# Per-cycle streaming — FIX B.1/B.2
# ---------------------------------------------------------------------------

# Tools whose side effect is writing/modifying files in the workspace.
# Used by FIX E to skip the workspace snapshot when no file tools ran.
# ---------------------------------------------------------------------------
# FIX D — per-engine caches for invariant-per-cycle computations
# ---------------------------------------------------------------------------
# These are module-level to survive across cycles of the same NousReasoningEngine.
# Each NousReasoningEngine instance gets its own namespace key via id(self).
# TTL guards stale reads after provider/memory changes.

import threading as _threading
import time as _time

_CACHE_LOCK = _threading.Lock()

# _chat_system_prompt cache: keyed by (engine_id, persona_key).
# persona_key = id of persona (identity is stable for the engine lifetime).
_SYSTEM_PROMPT_CACHE: dict[tuple[int, int], str] = {}

# _enrich_prompt_with_memory_snapshot cache: keyed by (tenant_id_str, base_hash).
# TTL 20s — memory can change but a 20s window is safe for chat latency.
_MEMORY_PROMPT_CACHE: dict[str, tuple[float, str]] = {}  # key → (expires_at, value)
_MEMORY_PROMPT_TTL_S: float = 20.0

# resolve_runtime_provider cache: keyed by engine_id. TTL 30s — mirrors the
# ActiveProviderService TTL so a provider switch takes effect within one period.
_RUNTIME_PROVIDER_CACHE: dict[int, tuple[float, tuple]] = {}  # key → (expires_at, (rt, bare))
_RUNTIME_PROVIDER_TTL_S: float = 30.0


def _cached_chat_system_prompt(engine_id: int, persona: Any) -> str:
    """Return _chat_system_prompt(persona) from cache keyed by (engine_id, id(persona)).

    FIX D: the system prompt is pure / deterministic for a given persona.
    Cache is keyed by (engine_id, id(persona)) so different agents with
    different personas get isolated entries.
    """
    key = (engine_id, id(persona))
    with _CACHE_LOCK:
        cached = _SYSTEM_PROMPT_CACHE.get(key)
    if cached is not None:
        return cached
    value = NousReasoningEngine._chat_system_prompt(persona)
    with _CACHE_LOCK:
        _SYSTEM_PROMPT_CACHE[key] = value
    return value


def _cached_enrich_prompt(base_prompt: str, tenant_id: "UUID") -> str:
    """Return _enrich_prompt_with_memory_snapshot with a 20s TTL.

    FIX D: avoids re-reading the memory store on every message.
    Cache key includes the base_prompt hash to handle system-prompt changes.
    """
    now = _time.monotonic()
    cache_key = f"{tenant_id}:{hash(base_prompt)}"
    with _CACHE_LOCK:
        entry = _MEMORY_PROMPT_CACHE.get(cache_key)
    if entry is not None and now < entry[0]:
        return entry[1]
    value = _enrich_prompt_with_memory_snapshot(base_prompt, tenant_id)
    with _CACHE_LOCK:
        _MEMORY_PROMPT_CACHE[cache_key] = (now + _MEMORY_PROMPT_TTL_S, value)
    return value


def _cached_resolve_hermes_runtime(engine_id: int, model_config: "ModelConfig") -> "tuple[dict, str]":
    """Return _resolve_hermes_runtime with a 30s TTL keyed by engine_id.

    FIX D: resolving the provider reads disk / an in-memory registry. Caching
    for 30s avoids re-reading per-message while still reacting to provider
    changes within one TTL window (same as ActiveProviderService).
    """
    now = _time.monotonic()
    with _CACHE_LOCK:
        entry = _RUNTIME_PROVIDER_CACHE.get(engine_id)
    if entry is not None and now < entry[0]:
        return entry[1]  # type: ignore[return-value]
    value = _resolve_hermes_runtime(model_config)
    with _CACHE_LOCK:
        _RUNTIME_PROVIDER_CACHE[engine_id] = (now + _RUNTIME_PROVIDER_TTL_S, value)
    return value


# ---------------------------------------------------------------------------
# FIX E — detect file-writing tool invocations from CycleOutput
# ---------------------------------------------------------------------------

_WORKSPACE_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "patch",
    "create_file",
    "delete_file",
    "move_file",
    "computer_use",
    "execute_code",
    "shell",
    "terminal",
})


def _did_cycle_write_files(output: "CycleOutput") -> bool:
    """Return True if any proposal in output used a file-writing tool.

    FIX E: skips the workspace diff for "hello world" style messages where no
    tool ran (the common case). Only pay the filesystem cost when warranted.
    """
    for proposal in output.tool_call_proposals:
        if proposal.tool_name in _WORKSPACE_WRITE_TOOLS:
            return True
    return False


# ---------------------------------------------------------------------------
# D-Bus chat streaming coalescing constants (spec streaming-dbus)
# ---------------------------------------------------------------------------

# Emit a ChatDelta signal when accumulated text reaches this many chars OR
# when _DBUS_FLUSH_INTERVAL_S seconds have elapsed since the last flush.
# This prevents spamming the system bus with one signal per LLM token while
# keeping latency below a perceptible threshold (~40 ms at 60 Hz).
_DBUS_BATCH_CHARS: int = 24
_DBUS_FLUSH_INTERVAL_S: float = 0.045  # ≈ 45 ms


def _describe_tool_call(function_name: str, function_args: dict[str, Any]) -> dict[str, Any]:
    """Derive a human-readable {tool, label, target} descriptor for a tool invocation.

    Used only to build the payload of a tool_call StreamFrame — never touches
    security-sensitive data (no PII, no credentials).  All values are truncated
    to stay under reasonable wire-frame sizes.

    target is extracted from the most meaningful argument for each tool family:
      - browser/navigate tools → url/path
      - search tools → query/q/search_query
      - file tools → path/filename
      - terminal/shell → command
      - default → first string argument value, or ""
    """
    _LABELS: dict[str, str] = {
        "browser_navigate": "Navegando",
        "browser_click": "Haciendo clic",
        "browser_type": "Escribiendo",
        "browser_snapshot": "Capturando página",
        "browser_back": "Volviendo atrás",
        "web_search": "Buscando en la web",
        "search_files": "Buscando archivos",
        "session_search": "Buscando en sesión",
        "read_file": "Leyendo archivo",
        "write_file": "Escribiendo archivo",
        "patch": "Aplicando parche",
        "create_file": "Creando archivo",
        "delete_file": "Eliminando archivo",
        "terminal": "Ejecutando comando",
        "shell": "Ejecutando comando",
        "execute_code": "Ejecutando código",
        "computer_use": "Usando el ordenador",
        "activate_app": "Abriendo aplicación",
        "memory": "Accediendo a memoria",
        "clarify": "Pidiendo aclaración",
        "web_extract": "Extrayendo de la web",
    }

    _TARGET_KEYS: dict[str, list[str]] = {
        "browser_navigate": ["url", "path"],
        "browser_click": ["selector", "text"],
        "browser_type": ["text", "value"],
        "web_search": ["query", "q", "search_query"],
        "search_files": ["query", "pattern"],
        "session_search": ["query"],
        "read_file": ["path", "filename", "file"],
        "write_file": ["path", "filename", "file"],
        "patch": ["path", "filename"],
        "create_file": ["path", "filename"],
        "delete_file": ["path", "filename"],
        "terminal": ["command", "cmd"],
        "shell": ["command", "cmd"],
        "execute_code": ["code", "command"],
        "computer_use": ["action"],
        "activate_app": ["app_name", "url"],
        "web_extract": ["url"],
    }

    label = _LABELS.get(function_name) or f"Usando {function_name}"

    target_keys = _TARGET_KEYS.get(function_name, [])
    target = ""
    for key in target_keys:
        val = function_args.get(key)
        if isinstance(val, str) and val.strip():
            target = val.strip()[:200]
            break

    if not target:
        # Fallback: first non-empty string arg value
        for val in function_args.values():
            if isinstance(val, str) and val.strip():
                target = val.strip()[:200]
                break

    return {"tool": function_name, "label": label, "target": target}


def _build_tool_call_emitter(
    chunk_sink: Any,
    task_id: "UUID",
    loop: "asyncio.AbstractEventLoop",
    accumulator: "list[dict[str, Any]] | None" = None,
    *,
    live_agent_id: str = "",
) -> "Callable[[str, dict[str, Any]], None]":
    """Return a sync callable that emits a tool_call StreamFrame from the executor thread.

    Signature: emitter(function_name, function_args) -> None.
    Fail-soft: any error is logged at DEBUG and silently swallowed so it never
    interrupts tool execution.

    Bridge pattern is identical to _build_stream_callback: run_coroutine_threadsafe
    bridges from the executor thread to the asyncio event loop.

    If `accumulator` is given, each descriptor is appended to it (in execution
    order) so the engine can persist the tool steps on CycleOutput.tool_steps —
    enabling a conversation reload to reconstruct the tool-step cards (the live
    frames alone are not persisted).

    `live_agent_id`: when provided, each dispatch also records the tool in the
    process-wide live_activity registry so runtime_status can surface it.
    """
    from hermes.tasks.control_plane.domain.ports import StreamChunkKind, TaskStreamChunk  # noqa: PLC0415
    from hermes.runtime import live_activity  # noqa: PLC0415

    _task_id_str = str(task_id)

    def _emitter(function_name: str, function_args: dict[str, Any]) -> None:
        descriptor = _describe_tool_call(function_name, function_args)
        if accumulator is not None:
            accumulator.append(descriptor)
        # Record real in-flight tool BEFORE emitting the frame so the registry
        # is always up-to-date by the time the frame reaches the client.
        if live_agent_id:
            activity_agent = live_agent_id
            activity_tool = function_name
            # Delegación: atribuir la actividad EN VIVO al especialista del roster que
            # mejor encaja, para que el Office muestre a ESE muñeco "trabajando"
            # (conectado) durante la sub-tarea, no al Cerebro.
            if function_name == "delegate_task":
                from hermes.agents.domain.default_roster import match_specialist  # noqa: PLC0415
                spec_text = " ".join(
                    str(function_args.get(k, ""))
                    for k in ("role", "goal", "context", "task", "instruction")
                )
                spec_id = match_specialist(spec_text)
                if spec_id:
                    activity_agent = spec_id
                    activity_tool = "trabajando"
            live_activity.record(_task_id_str, activity_agent, activity_tool)
        chunk = TaskStreamChunk(kind=StreamChunkKind.TOOL_CALL, tool_call=descriptor)
        try:
            fut = asyncio.run_coroutine_threadsafe(
                chunk_sink.emit(task_id=task_id, chunk=chunk),
                loop,
            )
            fut.result(timeout=2.0)
        except Exception:  # noqa: BLE001 — never crash tool execution for a missed frame
            logger.debug(
                "hermes.nous_engine.tool_call_frame_emit_failed tool=%s task=%s",
                function_name,
                str(task_id),
            )

    return _emitter


def _build_stream_callback(
    chunk_sink: Any,
    task_id: "UUID",
    loop: "asyncio.AbstractEventLoop",
    emit_counter: list,
    *,
    dbus_emit_delta: "Callable[[str, int, str], None] | None" = None,
    conversation_id: str = "",
) -> "Callable[..., None]":
    """Return a sync callback suitable for run_conversation(stream_callback=…).

    The callback is invoked by the Nous AIAgent in the executor thread each
    time a token / delta arrives.  It bridges to the async chunk_sink via
    asyncio.run_coroutine_threadsafe so it never blocks the main event loop.

    Argument shape that Nous calls us with (inspect the Nous source):
        callback(delta: str)                  — plain text token
        callback(delta: str, kind: str)       — kind in {"delta","thinking_delta"}

    We emit DELTA by default; THINKING_DELTA when kind=="thinking_delta".
    Any exception in this callback must NOT crash the agent loop — fail-soft.

    emit_counter: mutable [count] list; incremented per successful emit so
    the orchestrator can detect "already streamed" without touching its own
    state (FIX B.2).

    dbus_emit_delta: optional callable(conversation_id, seq, text) that emits
    ChatDelta on the system D-Bus. Called with coalesced batches to reduce
    signal frequency. Fail-soft: errors are logged at DEBUG and ignored.
    conversation_id: UUID string of the active conversation. Required when
    dbus_emit_delta is provided; signals are suppressed if empty.
    """
    import threading as _threading  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from hermes.tasks.control_plane.domain.ports import StreamChunkKind, TaskStreamChunk  # noqa: PLC0415

    # Coalescing state. Per-delta access is on the executor thread, but the final
    # force-flush from run_cycle runs on the loop thread, so the two can overlap —
    # _dbus_lock below guards the buffer + counters against that race.
    _dbus_buf: list[str] = []           # accumulated text fragments
    _dbus_buf_chars: list[int] = [0]    # total accumulated char count
    _dbus_last_flush: list[float] = [_time.monotonic()]
    _dbus_seq: list[int] = [0]          # monotonically increasing sequence number
    _dbus_lock = _threading.Lock()      # protects buffer + counters (flush called from callback thread)

    def _flush_dbus_batch(force: bool = False) -> None:
        """Flush the coalescing buffer as a single ChatDelta signal.

        Called per-delta from the executor thread, and once from run_cycle after the
        executor completes (force=True, on the loop thread). The actual D-Bus emit is
        always marshaled to the bus loop via loop.call_soon_threadsafe (see below), so
        either caller is safe.
        force=True: always flush even if threshold not yet reached (end-of-stream).
        """
        if dbus_emit_delta is None or not conversation_id:
            return
        with _dbus_lock:
            now = _time.monotonic()
            elapsed = now - _dbus_last_flush[0]
            ready = (
                force
                or _dbus_buf_chars[0] >= _DBUS_BATCH_CHARS
                or elapsed >= _DBUS_FLUSH_INTERVAL_S
            )
            if not ready or not _dbus_buf:
                return
            text = "".join(_dbus_buf)
            _dbus_buf.clear()
            _dbus_buf_chars[0] = 0
            _dbus_last_flush[0] = now
            _dbus_seq[0] += 1
            seq = _dbus_seq[0]

        # B1 fix (code-review): marshal the D-Bus emit onto the bus event loop.
        # self._iface.ChatDelta (dbus-fast aio path) calls loop.create_future() /
        # loop.add_writer() / sock.send() SYNCHRONOUSLY in the calling thread — those
        # are NOT asyncio-thread-safe. This flush runs in the run_in_executor thread
        # (per-delta), so a direct call would race/corrupt the daemon's event loop and
        # the system-bus socket. call_soon_threadsafe is the correct marshaling
        # primitive; `loop` is the same one the chunk_sink path uses above. The final
        # force-flush from run_cycle is already on the loop thread — scheduling there
        # just defers to the next iteration, still correct.
        try:
            loop.call_soon_threadsafe(dbus_emit_delta, conversation_id, seq, text)
        except Exception:  # noqa: BLE001 — never crash the agent for a missed D-Bus emit
            logger.debug(
                "hermes.nous_engine.dbus_chat_delta_failed conv=%s seq=%d",
                conversation_id, seq,
            )

    def _callback(*args: Any, **kwargs: Any) -> None:
        # Support both positional and keyword forms Nous may use.
        delta: str = ""
        kind: str = "delta"
        if args:
            delta = str(args[0]) if args[0] is not None else ""
        if len(args) >= 2:
            kind = str(args[1])
        delta = delta or str(kwargs.get("delta", ""))
        kind = kind or str(kwargs.get("kind", "delta"))

        if not delta:
            return

        chunk_kind = (
            StreamChunkKind.THINKING_DELTA
            if kind == "thinking_delta"
            else StreamChunkKind.DELTA
        )
        chunk = TaskStreamChunk(kind=chunk_kind, delta=delta)
        try:
            fut = asyncio.run_coroutine_threadsafe(
                chunk_sink.emit(task_id=task_id, chunk=chunk),
                loop,
            )
            fut.result(timeout=2.0)
            emit_counter[0] += 1
        except Exception:  # noqa: BLE001 — never crash the agent for a missed frame
            logger.debug(
                "hermes.nous_engine.stream_callback_emit_failed",
                extra={"task_id": str(task_id)},
            )

        # D-Bus coalescing — only for plain text deltas, not thinking tokens.
        # Thinking deltas are internal reasoning; the compositor only needs
        # the narrative text to display in the streaming bubble.
        if dbus_emit_delta and conversation_id and kind != "thinking_delta":
            with _dbus_lock:
                _dbus_buf.append(delta)
                _dbus_buf_chars[0] += len(delta)
            _flush_dbus_batch()

    # Expose the flush so run_cycle can call it after the executor completes.
    _callback._flush_dbus = _flush_dbus_batch  # type: ignore[attr-defined]

    return _callback


# ---------------------------------------------------------------------------
# Workspace artifact helpers — deterministic MEDIA token attachment (Change 1)
# ---------------------------------------------------------------------------

# Read from env so tests can point it at a temp dir without patching os.
_WORKSPACE_DIR: str = os.environ.get(
    "HERMES_WORKSPACE_DIR", "/var/lib/hermes/workspace"
)

# Hard cap: never flood the chat with a bulk file-creation run.
_WORKSPACE_ATTACH_CAP: int = 10


def _snapshot_workspace(workspace_dir: str = _WORKSPACE_DIR) -> dict[str, float]:
    """Return {filepath: mtime} for every regular file directly in *workspace_dir*.

    Fail-soft: any OS error (missing dir, permission denied) returns {}.
    Only direct children are snapshotted (no recursion) — we track files the
    agent explicitly places in the workspace root, not deep tree changes.
    """
    try:
        p = Path(workspace_dir)
        if not p.is_dir():
            return {}
        return {
            str(child): child.stat().st_mtime
            for child in p.iterdir()
            if child.is_file()
        }
    except Exception:  # noqa: BLE001
        return {}


def _workspace_delta(
    snapshot: dict[str, float],
    workspace_dir: str = _WORKSPACE_DIR,
) -> list[Path]:
    """Return regular files in *workspace_dir* whose mtime is NEWER than snapshot.

    A file is "new or modified" if:
      - It was absent in the snapshot, OR
      - Its current mtime > snapshot mtime.

    Excludes: dotfiles, zero-byte files, non-regular files.
    Returns files sorted newest-first (largest mtime first).
    Fail-soft: any OS error → empty list.
    """
    try:
        p = Path(workspace_dir)
        if not p.is_dir():
            return []
        results: list[tuple[float, Path]] = []
        for child in p.iterdir():
            if not child.is_file():
                continue
            if child.name.startswith("."):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            prior_mtime = snapshot.get(str(child))
            if prior_mtime is None or st.st_mtime > prior_mtime:
                results.append((st.st_mtime, child))
        results.sort(key=lambda t: t[0], reverse=True)
        return [path for _, path in results]
    except Exception:  # noqa: BLE001
        return []


def _attach_artifacts(narrative: str, paths: list[Path]) -> str:
    """Append MEDIA tokens to *narrative* for artifact paths not already referenced.

    For each path in *paths* (up to _WORKSPACE_ATTACH_CAP):
      - Skip if the absolute path string is already anywhere in narrative.
      - Otherwise append ``\\nMEDIA:/var/lib/hermes/workspace/<basename>`` exactly.

    Never mutates *narrative* on error — returns it unchanged (fail-soft).
    """
    try:
        capped = paths[:_WORKSPACE_ATTACH_CAP]
        if len(paths) > _WORKSPACE_ATTACH_CAP:
            logger.warning(
                "hermes.nous_engine.workspace_attach_capped: "
                "produced=%d cap=%d, attaching newest %d",
                len(paths), _WORKSPACE_ATTACH_CAP, _WORKSPACE_ATTACH_CAP,
            )
        import os as _os  # noqa: PLC0415
        tokens: list[str] = []
        for path in capped:
            token_path = str(path)
            # Los ficheros que crea el daemon (write_file/execute_code) suelen
            # nacer 0600 (umask del daemon) → el compositor (hermes-user, grupo
            # hermes) y el gateway no podrían leerlos y el artefacto no se vería
            # ni se descargaría. El daemon es dueño → añade lectura de GRUPO
            # (0640) a cada entregable. Best-effort: si falla, se adjunta igual.
            try:
                _mode = path.stat().st_mode
                _os.chmod(path, _mode | 0o040)
            except OSError:
                pass
            if token_path in narrative:
                continue
            tokens.append(f"\nMEDIA:{token_path}")
        if not tokens:
            return narrative
        return narrative + "".join(tokens)
    except Exception:  # noqa: BLE001
        logger.warning("hermes.nous_engine.attach_artifacts_error", exc_info=True)
        return narrative


class NousAgentNotInstalledError(ImportError):
    """hermes-agent (NousResearch v0.15.1) no está instalado.

    Instalar con:
        pip install hermes-agent==0.15.1
    O en el bake del Containerfile (ver TODO devops al final del módulo).
    """


def _import_ai_agent() -> type:
    """Import lazy de AIAgent. Falla de forma clara si hermes-agent no está."""
    try:
        from run_agent import AIAgent  # noqa: PLC0415
        return AIAgent
    except ImportError as exc:
        raise NousAgentNotInstalledError(
            "hermes-agent (NousResearch) no está instalado. "
            "Ejecuta: pip install hermes-agent==0.15.1\n"
            "O activa el engine por defecto: HERMES_ENGINE=litellm"
        ) from exc


def _import_run_conversation() -> Callable[..., dict[str, Any]]:
    """Import lazy de run_conversation."""
    try:
        from agent.conversation_loop import run_conversation  # noqa: PLC0415
        return run_conversation
    except ImportError as exc:
        raise NousAgentNotInstalledError(
            "hermes-agent (NousResearch) no está instalado — "
            "agent.conversation_loop no encontrado."
        ) from exc



class _ExternalToolCatalog:
    """Catálogo en-memoria de ToolSpecs externos (Composio + MCP) para el ciclo.

    Construido UNA VEZ por ciclo en NousReasoningEngine._build_governed_agent.
    Inmutable durante run_conversation: la clasificación de riesgo y los handlers
    son fijos para la duración de la llamada al LLM.

    Invariante de seguridad: el handler de CADA tool READ despacha a través del
    CapabilityBroker — nunca llama directamente al adaptador externo.
    """

    def __init__(self, specs: tuple[ToolSpec, ...]) -> None:
        self._by_name: dict[str, ToolSpec] = {s.name: s for s in specs}

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    def __len__(self) -> int:
        return len(self._by_name)

    def all_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._by_name.values())


def _resolve_hermes_runtime(model_config: ModelConfig) -> "tuple[dict, str]":
    """Resuelve el runtime del provider vía hermes-agent (camino canónico).

    Devuelve (runtime_dict, bare_model). El runtime trae provider/api_mode/
    base_url/api_key/credential_pool resueltos por Hermes desde su registry +
    .env + auth store (OAuth). Replica lo que hace hermes_cli/oneshot.py.

    Usa el catálogo unificado (spec 016) para mapear litellm prefix → slug de
    hermes_cli. Reemplaza _HERMES_SLUG_BY_PREFIX (borrado) que tenía
    'openai' → 'openai-api' (slug inválido → AuthError recurrente).
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: PLC0415
    except ImportError as exc:
        raise NousAgentNotInstalledError(
            "hermes_cli.runtime_provider no disponible — hermes-agent no instalado. "
            f"Detalle: {exc}"
        ) from exc

    # ── CAMINO NATIVO (el que el dueño pidió) ──────────────────────────────
    # Si config.yaml tiene model.provider configurado (CUALQUIER provider nativo
    # de la tabla de hermes_cli: openai-api directo, openai-codex/ChatGPT OAuth,
    # nous, copilot, gemini…), resolvemos DIRECTO con hermes_cli leyendo
    # .env/config.yaml/auth-store — sin el catálogo spec-016 ni el vault. Es
    # EXACTAMENTE lo que hace `hermes --provider <id>`. requested=None hace que
    # resolve_requested_provider lea config.yaml. Backward-compatible: si no hay
    # model.provider (setups vault legacy), cae al camino de abajo.
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        _cfg_model = (load_config() or {}).get("model") or {}
        _native_prov = (_cfg_model.get("provider") or "").strip()
        _native_model = (_cfg_model.get("default") or _cfg_model.get("model") or "").strip()
        if _native_prov and _native_prov != "auto":
            runtime = resolve_runtime_provider(target_model=_native_model or None)
            bare = _native_model or (
                runtime.get("model") if isinstance(runtime, dict) else ""
            ) or ""
            logger.info(
                "hermes.nous_engine.native_provider_resolved provider=%s model=%s",
                _native_prov, bare,
            )
            _align_auxiliary_with_runtime(runtime, bare, fallback_provider=_native_prov)
            return runtime, bare
    except Exception as _nexc:  # noqa: BLE001 — el camino vault sigue disponible
        logger.debug("hermes.nous_engine.native_resolve_skip: %r", _nexc)

    from hermes.providers.infrastructure.nous_provider_adapter import (  # noqa: PLC0415
        nous_request_from_model_config,
    )

    req, bare = nous_request_from_model_config(model_config)
    runtime = resolve_runtime_provider(
        requested=req.requested,
        explicit_api_key=req.explicit_api_key,
        explicit_base_url=req.explicit_base_url,
        target_model=req.target_model,
    )
    _align_auxiliary_with_runtime(
        runtime, bare, fallback_provider=req.requested,
        fallback_key=req.explicit_api_key, fallback_url=req.explicit_base_url,
    )
    return runtime, bare


def _align_auxiliary_with_runtime(
    runtime: object, bare: str, *,
    fallback_provider: str | None = None,
    fallback_key: str | None = None,
    fallback_url: str | None = None,
) -> None:
    """Alinea el cliente AUXILIAR (título/compresión/goal-judge…) con el provider
    REAL del chat. En modo "auto" el auxiliar prueba openrouter/nous (que el
    usuario no configuró) → fallan, marcan unhealthy 60s → el ciclo tarda ~50s.
    set_runtime_main + OPENAI_API_KEY/BASE_URL lo apuntan al provider activo.
    Nunca rompe el chat (best-effort).
    """
    try:
        import os as _os  # noqa: PLC0415
        _key = (runtime.get("api_key") if isinstance(runtime, dict) else None) or fallback_key
        _url = (runtime.get("base_url") if isinstance(runtime, dict) else None) or fallback_url
        _prov = (runtime.get("provider") if isinstance(runtime, dict) else None) or fallback_provider
        if _key and _url and "openai.com" in str(_url):
            _os.environ["OPENAI_API_KEY"] = str(_key)
            _os.environ["OPENAI_BASE_URL"] = str(_url)
        from agent.auxiliary_client import set_runtime_main  # noqa: PLC0415
        set_runtime_main(str(_prov or "openai"), str(bare or ""))
    except Exception as _exc:  # noqa: BLE001 — nunca romper el chat por el auxiliar
        logger.debug("hermes.nous_engine.aux_align_skip: %r", _exc)


class GovernedAIAgent:
    """Subclase de AIAgent con hook de interceptación en _invoke_tool.

    F2: _tool_gate implementado con 3 caminos:
      READ  → ejecuta handler del ToolSpec (broker-dispatching) o nativo Nous.
      WRITE → captura ToolCallProposal + dispatch al broker.
      UNKNOWN → fail-closed BLOCKED.

    F3: external_catalog (Composio + MCP ToolSpecs) se consulta DESPUÉS del
    mapa nativo Nous. Si la tool está en el catálogo externo, se usa su risk
    (READ_ONLY → _execute_external_read; WRITE_* → _dispatch_write_proposal
    con entity_type correcto).

    Las proposals PENDING_APPROVAL se acumulan en _pending_proposals
    durante run_conversation y se extraen después.
    """

    def __init__(
        self,
        *args: Any,
        broker: CapabilityBrokerPort | None = None,
        consent_context: ConsentContext | None = None,
        engine_loop: asyncio.AbstractEventLoop | None = None,
        tenant_id: UUID | None = None,
        external_catalog: _ExternalToolCatalog | None = None,
        tool_call_emitter: "Callable[[str, dict[str, Any]], None] | None" = None,
        active_agent_id: str = "",
        pii_mapping: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        AIAgent = _import_ai_agent()
        self._inner = AIAgent(*args, **kwargs)
        # Per-cycle PII placeholder→value map. The LLM sees tokenized context
        # ([[EMAIL_1]] etc.), so its tool-call ARGS carry placeholders. External
        # (composio/mcp) calls must be REHYDRATED to real values before they leave
        # for the third-party API — else e.g. gmail_send_email receives
        # "[[EMAIL_1]]" and 400s. Rehydration happens as late as possible, at the
        # external-dispatch boundary in this agent (native tools are untouched).
        self._pii_mapping: dict[str, str] = pii_mapping or {}
        self._inner._invoke_tool = self._invoke_tool

        self._broker = broker
        self._consent_context = consent_context
        self._engine_loop = engine_loop
        self._tenant_id = tenant_id or UUID(int=0)
        # Provenance: the agent_id that owns this cycle. Injected at build time
        # from DecisionContext.agent_id (resolved in run_cycle before this ctor).
        # Used to stamp memory write proposals with _provenance_agent_id.
        self._active_agent_id: str = active_agent_id or "unknown"
        # F3: catálogo de tools externas (Composio + MCP).
        self._external_catalog: _ExternalToolCatalog = (
            external_catalog or _ExternalToolCatalog(())
        )
        # Observability: emits tool_call StreamFrame before each tool dispatch.
        # None = no live progress frames (e.g. tests without a chunk_sink).
        self._tool_call_emitter: "Callable[[str, dict[str, Any]], None] | None" = (
            tool_call_emitter
        )
        # Wire the inner AIAgent's tool_start_callback to our emitter.
        # Nous fires tool_start_callback(tool_call_id, function_name, function_args)
        # on BOTH the concurrent and sequential non-inline paths — this is the
        # universal hook that covers all real agentic tools (browser, terminal,
        # skill_*, external Composio/MCP) without touching Nous internals.
        if tool_call_emitter is not None:
            _emitter_ref = tool_call_emitter

            def _tool_start_cb(
                tool_call_id: str,
                function_name: str,
                function_args: dict,
            ) -> None:
                try:
                    _emitter_ref(function_name, function_args)
                except Exception:  # noqa: BLE001
                    pass

            self._inner.tool_start_callback = _tool_start_cb

        # Mutable: acumuladas durante run_conversation, leídas después.
        self._pending_proposals: list[ToolCallProposal] = []
        # True si al menos una READ ingirió contenido externo (CTRL-5).
        self._read_external_content: bool = False

        # SECURITY FIX: gate the sequential path (len==1 → quiet_mode branch
        # in execute_tool_calls_sequential bypasses _invoke_tool and goes
        # directly to tools.registry.dispatch). Re-register WRITE-classified
        # native tools with broker-dispatching wrappers so both paths
        # hit the broker EXACTLY ONCE. Must run AFTER self is fully set up.
        _wire_sequential_gate(self)

        # SECURITY FIX (Issue 2): gate inline-branch tools (memory, clarify).
        # tool_executor.execute_tool_calls_sequential intercepts these via
        # hardcoded if/elif branches BEFORE handle_function_call/registry.dispatch,
        # so registry wrappers are dead code for them. Monkeypatch the actual
        # handler functions so broker gates even on the inline path.
        _wire_inline_branch_gates(self)

    def _invoke_tool(
        self,
        function_name: str,
        function_args: dict[str, Any],
        effective_task_id: str,
        tool_call_id: str | None = None,
        messages: list[Any] | None = None,
        pre_tool_block_checked: bool = False,
    ) -> str:
        """Gate F2/F3: clasifica la tool y enruta por READ/WRITE/UNKNOWN.

        Orden de clasificación (fail-closed):
          1. Catálogo nativo Nous (classify_nous_tool) — herramientas del core.
          2. Catálogo externo (Composio + MCP) — inyectado en este ciclo.
          3. Default-deny: BLOCKED si no está en ningún catálogo.

        INVARIANTE: un WRITE NUNCA ejecuta el handler nativo de Nous.
        INVARIANTE: toda tool externa pasa por el CapabilityBroker exactamente UNA vez.
        """
        nous_risk = classify_nous_tool(function_name)

        if nous_risk is not None:
            return self._dispatch_nous_native(
                function_name, function_args, effective_task_id,
                tool_call_id, messages, pre_tool_block_checked, nous_risk,
            )

        external_spec = self._external_catalog.get(function_name)
        if external_spec is not None:
            return self._dispatch_external(
                function_name, function_args, effective_task_id,
                tool_call_id, external_spec,
            )

        return self._blocked(function_name, "herramienta no clasificada (default-deny)")

    def _dispatch_nous_native(
        self,
        function_name: str,
        function_args: dict[str, Any],
        effective_task_id: str,
        tool_call_id: str | None,
        messages: list[Any] | None,
        pre_tool_block_checked: bool,
        risk: NousRisk,
    ) -> str:
        """Enruta una tool del catálogo nativo de Nous.

        MODELO HERMES-NATIVE (W20): tanto READ como WRITE EJECUTAN NATIVO. El
        gate ya NO es el broker para las tools nativas — es el hook
        `pre_tool_call` (kill-switch + suelo hardline + anti-autojailbreak +
        guards de approval.py), que corre ANTES en ambos paths (concurrent y
        sequential), y el hook `post_tool_call` audita. El broker rechazaba las
        nativas ('no registrado', sin surface-adapter) — por eso terminal/
        browser_navigate/computer_use salían BLOCKED. El broker queda SOLO para
        las tools custom os_surface (activate_app, services, …) que sí ejecuta
        vía surface-adapter. Postura full-autónomo: el confinamiento kernel es
        la frontera; el hardline (terminal) lo único inapelable.
        """
        # DETERMINISTIC CHOKEPOINT: every fs/exec surface routes through the cage —
        # the LLM/hooks/broker can be confused or jailbroken, but the cage doesn't
        # reason, it just confines. read_file et al. ran in-daemon natively and could
        # read master.key; now they execute INSIDE the sandbox (uid 999) like terminal.
        if function_name in _CAGED_NATIVE_TOOLS:
            return self._run_caged_tool(function_name, function_args)
        if risk is NousRisk.READ:
            return self._execute_read_native(
                function_name, function_args, effective_task_id,
                tool_call_id, messages, pre_tool_block_checked,
            )
        # WRITE nativa. SECURITY (red-team 2026-06-19 — el agujero más grave): el tool
        # "terminal" nativo de Nous ejecuta subprocess EN EL PROCESO DEL DAEMON
        # (User=hermes, dueño de master.key 0600, en el host netns = egress abierto) —
        # NUNCA estuvo confinado. Probado e2e: el agente leyó master.key y exfiltró por
        # curl. Lo redirigimos al exec-launcher: el comando corre como hermes-sandbox
        # DENTRO del netns enjaulado (egress default-deny vía proxy) con los secretos
        # InaccessiblePaths. Las MANOS del agente quedan enjauladas aunque su cerebro
        # viva en el daemon. FAIL-CLOSED: si la jaula no está, el comando NO se ejecuta
        # (nunca caemos al subprocess in-daemon).
        # (caged exec/file tools were already routed to the sandbox chokepoint above)
        result = self._call_native_invoke(
            function_name, function_args, effective_task_id,
            tool_call_id, messages, pre_tool_block_checked,
        )
        # Taint de procedencia: browser_navigate/etc. ingieren contenido web no
        # confiable → marca para que el gate eleve a HITL los WRITE subsecuentes.
        if _is_external_content_tool(function_name):
            self._read_external_content = True
        return result

    def _dispatch_external(
        self,
        function_name: str,
        function_args: dict[str, Any],
        effective_task_id: str,
        tool_call_id: str | None,
        spec: ToolSpec,
    ) -> str:
        """Enruta una tool del catálogo externo (Composio/MCP) a través del broker.

        READ_ONLY → ejecuta handler del ToolSpec (broker-dispatching closure).
        WRITE_*   → proposal con entity_type correcto → broker.dispatch.

        GARANTÍA: el handler NATIVO de Nous NUNCA se invoca para tools externas.
        """
        if spec.risk == ToolRisk.READ_ONLY:
            return self._execute_external_read(function_name, function_args, spec)
        return self._dispatch_external_write(
            function_name, function_args, effective_task_id, tool_call_id, spec
        )

    # ------------------------------------------------------------------
    # External tool paths (Composio + MCP) — F3
    # ------------------------------------------------------------------

    def _execute_external_read(
        self,
        function_name: str,
        function_args: dict[str, Any],
        spec: ToolSpec,
    ) -> str:
        """Ejecuta una tool READ externa llamando su handler (broker-dispatching closure).

        El handler en ToolSpec.handler ya despacha a través del broker —
        NO hay double-gate ni bypass. El broker aplica consent+audit+kill-switch.
        """
        if spec.handler is None:
            return self._blocked(function_name, "READ tool sin handler — misconfiguration")

        if self._engine_loop is None:
            return self._blocked(function_name, "engine_loop no configurado")

        from hermes.capabilities.domain.ports import ExecutionStatus  # noqa: PLC0415

        # Rehydrate PII placeholders before the READ leaves for the external API
        # (e.g. a search arg containing an email the LLM saw tokenized).
        function_args = _rehydrate_external_args(function_args, self._pii_mapping)

        try:
            future = asyncio.run_coroutine_threadsafe(
                spec.handler(function_args),
                self._engine_loop,
            )
            result = future.result(timeout=_BROKER_DISPATCH_TIMEOUT_S)
        except TimeoutError:
            logger.error(
                "hermes.nous_engine.external_read_timeout: tool=%s", function_name
            )
            return self._blocked(function_name, "external_read_timeout — fail-closed")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.nous_engine.external_read_error: tool=%s error=%s",
                function_name, exc,
            )
            return self._blocked(function_name, f"external_read_error: {type(exc).__name__}")

        # Mark external content taint (CTRL-5) — reads from external services
        # may contain untrusted content that should force HITL for subsequent WRITEs.
        self._read_external_content = True
        logger.debug(
            "hermes.nous_engine.external_read_executed: tool=%s", function_name
        )
        # Cap the serialized result to a token-safe size BEFORE it enters the model
        # context. External reads (composio/mcp) can return arbitrarily large,
        # token-dense JSON (gmail_fetch_emails ≈ 85 KB) that overflows a small-context
        # model on the very next call — which then "cannot compress further" and the
        # task retries in a loop with no reply. Same choke-point cap as the concurrent
        # CapturingToolHost path (single source of truth).
        from hermes.runtime.tool_host import _cap_external_result  # noqa: PLC0415
        return _cap_external_result(
            json.dumps(result, ensure_ascii=False, default=str), function_name
        )

    def _dispatch_external_write(
        self,
        function_name: str,
        function_args: dict[str, Any],
        effective_task_id: str,
        tool_call_id: str | None,
        spec: ToolSpec,
    ) -> str:
        """Captura una tool WRITE externa como ToolCallProposal y la despacha al broker.

        entity_type y entity_id son derivados del ToolSpec para que
        ComposioCapabilityRegistry / McpCapabilityRegistry puedan resolverla.
        GARANTÍA: el handler nativo de Nous NUNCA se invoca.
        """
        # Circuit breaker (same as the native path): stop re-proposing a tool that
        # keeps failing this turn — otherwise a failing Composio/MCP action re-cards
        # forever (the connect_integration 502 retry-spam).
        _fc = write_tool_failure_count(function_name)
        if _fc >= _MAX_WRITE_TOOL_FAILURES:
            logger.warning(
                "hermes.nous_engine.external_write_circuit_broken tool=%s failures=%d",
                function_name, _fc,
            )
            return _write_circuit_broken_msg(function_name, _fc)

        # Rehydrate PII placeholders in the args before the WRITE leaves for the
        # external API (composio/mcp) — the LLM emitted them tokenized.
        function_args = _rehydrate_external_args(function_args, self._pii_mapping)

        proposal = _build_external_proposal(
            function_name=function_name,
            function_args=function_args,
            tenant_id=self._tenant_id,
            effective_task_id=effective_task_id,
            spec=spec,
        )

        if self._broker is None or self._consent_context is None or self._engine_loop is None:
            logger.warning(
                "hermes.nous_engine.external_write_no_broker: tool=%s blocked",
                function_name,
            )
            return self._blocked(function_name, "broker no configurado — fail-closed")

        conversation_id = resolve_conversation(effective_task_id)
        outcome = _dispatch_via_bridge(
            proposal=proposal,
            broker=self._broker,
            consent_context=self._consent_context,
            engine_loop=self._engine_loop,
            conversation_id=conversation_id,
        )
        # Same block-and-resume as the native write path: external (Composio/MCP)
        # writes pending approval in an active chat hold the thread and re-dispatch
        # on approval, instead of the non-resuming retry queue.
        from hermes.capabilities.domain.ports import ExecutionStatus  # noqa: PLC0415
        if outcome.status is ExecutionStatus.PENDING_APPROVAL and conversation_id:
            result = self._await_owner_and_resume(proposal, conversation_id)
        else:
            result = self._handle_outcome(proposal, outcome)
        if _write_result_is_failure(result):
            bump_write_tool_failure(function_name)
        return result

    # ------------------------------------------------------------------
    # READ path (native Nous tools)
    # ------------------------------------------------------------------

    def _execute_read_native(
        self,
        function_name: str,
        function_args: dict[str, Any],
        effective_task_id: str,
        tool_call_id: str | None,
        messages: list[Any] | None,
        pre_tool_block_checked: bool,
    ) -> str:
        """Ejecuta el handler nativo de Nous para tools READ_ONLY.

        Marca taint si la tool ingiere contenido externo no confiable (CTRL-5).
        """
        result = self._call_native_invoke(
            function_name, function_args, effective_task_id,
            tool_call_id, messages, pre_tool_block_checked,
        )
        if _is_external_content_tool(function_name):
            self._read_external_content = True
            logger.debug(
                "hermes.nous_engine.read_taint: tool=%s ingiere contenido externo",
                function_name,
            )
        return result

    def _call_native_invoke(
        self,
        function_name: str,
        function_args: dict[str, Any],
        effective_task_id: str,
        tool_call_id: str | None,
        messages: list[Any] | None,
        pre_tool_block_checked: bool,
    ) -> str:
        from agent.agent_runtime_helpers import invoke_tool  # noqa: PLC0415
        return invoke_tool(
            self._inner,
            function_name,
            function_args,
            effective_task_id,
            tool_call_id,
            messages,
            pre_tool_block_checked,
        )

    def _run_caged_tool(
        self, function_name: str, function_args: dict[str, Any]
    ) -> str:
        """Route ANY native exec/file tool into the confinement substrate (chokepoint).

        terminal/shell/execute_code/read_file/write_file/… all collapse to a single
        shell command that runs INSIDE the cage. OpenShell active
        (HERMES_OPENSHELL_SANDBOX set) → the command runs in the per-agent sandbox
        (uid 999, landlock, egress proxy) via ExecSandbox. Otherwise → the exec-launcher
        cage. BOTH FAIL-CLOSED: never in the daemon process. Red-team 2026-06-19: the
        native exec/file tools read master.key / ran arbitrary code in-daemon — now the
        cage is the deterministic gate for EVERY hand the agent has.
        """
        cmd = _build_caged_command(function_name, function_args)
        if cmd is None:
            return self._blocked(
                function_name,
                f"{function_name}: argumentos insuficientes para ejecutar en la jaula "
                "(default-deny).",
            )
        sandbox = os.environ.get(_OPENSHELL_SANDBOX_ENV)
        if sandbox:
            return self._run_via_openshell(function_name, cmd, sandbox)
        return self._run_via_exec_launcher(function_name, cmd)

    def _run_via_openshell(
        self, function_name: str, cmd: str, sandbox: str
    ) -> str:
        """Execute the command in the per-agent OpenShell sandbox via the CLI.

        The CLI talks gRPC/mTLS to the gateway, which runs the command as uid 999
        inside the sandbox (landlock FS jail + all egress via the L7 proxy). The
        daemon's master.key is not in the sandbox. FAIL-CLOSED: any error DENIES.
        """
        import subprocess  # noqa: PLC0415

        cli = os.environ.get("HERMES_OPENSHELL_CLI", _OPENSHELL_CLI_DEFAULT)
        home = os.environ.get("HERMES_OPENSHELL_HOME", _OPENSHELL_HOME_DEFAULT)
        env = dict(os.environ)
        env["HOME"] = home
        env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
        env.setdefault("LD_LIBRARY_PATH", _OPENSHELL_LIB_DIR)
        argv = [cli, "sandbox", "exec", "-n", sandbox, "--", "bash", "-lc", cmd]
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                text=True,
                timeout=_CAGED_TERMINAL_TIMEOUT_S + 30,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed; never run in-daemon
            logger.error(
                "hermes.nous_engine.openshell_exec_unreachable tool=%s sandbox=%s "
                "err=%r — command DENIED (fail-closed)",
                function_name, sandbox, exc,
            )
            return self._blocked(
                function_name,
                "OpenShell sandbox unreachable — command DENIED (fail-closed; it "
                "never runs in the daemon process).",
            )
        exit_code = proc.returncode
        out = proc.stdout or ""
        err = "\n".join(
            line for line in (proc.stderr or "").splitlines()
            if ".profile" not in line
        )
        logger.info(
            "hermes.nous_engine.openshell_exec tool=%s sandbox=%s exit=%s "
            "stdout=%dB stderr=%dB",
            function_name, sandbox, exit_code, len(out), len(err),
        )
        parts = [f"exit_code={exit_code}"]
        if out:
            parts.append("stdout:\n" + out)
        if err:
            parts.append("stderr:\n" + err)
        return "\n".join(parts)

    def _run_via_exec_launcher(
        self, function_name: str, cmd: str
    ) -> str:
        """Run the agent's terminal command through the exec-launcher CAGE.

        The command runs as hermes-sandbox inside /run/netns/hermes-browser (egress
        default-deny via the audited proxy) with the daemon's secrets InaccessiblePaths
        — NOT in the daemon process. FAIL-CLOSED: any launcher/transport error DENIES
        the command — we never fall back to in-daemon execution.
        """
        import json as _json  # noqa: PLC0415
        import socket as _socket  # noqa: PLC0415
        import struct as _struct  # noqa: PLC0415

        sock_path = os.environ.get(
            "HERMES_EXEC_LAUNCHER_SOCK", "/run/hermes/exec-launch.sock"
        )
        req = {
            "argv": ["bash", "-lc", cmd],
            "workspace": _resolve_agent_workspace(),
            "timeout_s": _CAGED_TERMINAL_TIMEOUT_S,
        }
        sock = None
        try:
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(_CAGED_TERMINAL_TIMEOUT_S + 30)
            sock.connect(sock_path)
            body = _json.dumps(req).encode("utf-8")
            sock.sendall(_struct.pack(">I", len(body)) + body)
            n = _struct.unpack(">I", _recv_exactly_sync(sock, 4))[0]
            if n > _CAGED_TERMINAL_FRAME_MAX:
                raise ValueError("exec-launcher frame too large")
            resp = _json.loads(_recv_exactly_sync(sock, n).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — fail-closed; never run in-daemon
            logger.error(
                "hermes.nous_engine.caged_terminal_unreachable tool=%s err=%r "
                "— command DENIED (fail-closed)",
                function_name, exc,
            )
            return self._blocked(
                function_name,
                "terminal jail unreachable — command DENIED (fail-closed; it never "
                "runs in the daemon process).",
            )
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        if not resp.get("ok"):
            return self._blocked(
                function_name, f"terminal jail rejected: {resp.get('error', 'unknown')}"
            )

        exit_code = int(resp.get("exit_code", 1))
        out = resp.get("stdout", "") or ""
        err = resp.get("stderr", "") or ""
        logger.info(
            "hermes.nous_engine.caged_terminal tool=%s exit=%s stdout=%dB stderr=%dB",
            function_name, exit_code, len(out), len(err),
        )
        parts = [f"exit_code={exit_code}"]
        if out:
            parts.append("stdout:\n" + out)
        if err:
            parts.append("stderr:\n" + err)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # WRITE path
    # ------------------------------------------------------------------

    def _dispatch_write_proposal(
        self,
        function_name: str,
        function_args: dict[str, Any],
        effective_task_id: str,
        tool_call_id: str | None,
    ) -> str:
        """Captura un WRITE como ToolCallProposal y lo despacha al broker.

        GARANTÍA: el handler nativo de Nous NO se invoca en ningún caso.
        """
        # Circuit breaker: this tool already failed too many times this turn → refuse
        # to re-propose it (each retry mints a fresh HITL card). Stops the retry-spam
        # and lets the turn end so the chat message finalizes.
        _fc = write_tool_failure_count(function_name)
        if _fc >= _MAX_WRITE_TOOL_FAILURES:
            logger.warning(
                "hermes.nous_engine.write_circuit_broken tool=%s failures=%d",
                function_name, _fc,
            )
            return _write_circuit_broken_msg(function_name, _fc)

        proposal = _build_proposal(
            function_name=function_name,
            function_args=function_args,
            tenant_id=self._tenant_id,
            effective_task_id=effective_task_id,
        )

        if self._broker is None or self._consent_context is None or self._engine_loop is None:
            # Sin broker configurado → fail-closed. El agente verá BLOCKED.
            logger.warning(
                "hermes.nous_engine.write_no_broker: tool=%s blocked (broker not wired)",
                function_name,
            )
            return self._blocked(function_name, "broker no configurado — fail-closed")

        conversation_id = resolve_conversation(effective_task_id)
        outcome = _dispatch_via_bridge(
            proposal=proposal,
            broker=self._broker,
            consent_context=self._consent_context,
            engine_loop=self._engine_loop,
            conversation_id=conversation_id,
        )

        # Block-and-resume (Mandato 1): si quedó pendiente de aprobación Y hay una
        # conversación de chat ACTIVA mirando, retenemos el hilo aquí hasta que el dueño
        # apruebe/rechace y RE-DESPACHAMOS la MISMA llamada con el token → se ejecuta y el
        # turno continúa en el MISMO stream (sin "HECHO", sin re-encolar mudo). En autónomo/
        # scheduled (sin conversation_id) se mantiene el modelo de cola no-bloqueante.
        from hermes.capabilities.domain.ports import ExecutionStatus  # noqa: PLC0415
        if outcome.status is ExecutionStatus.PENDING_APPROVAL and conversation_id:
            result = self._await_owner_and_resume(proposal, conversation_id)
        else:
            result = self._handle_outcome(proposal, outcome)
        if _write_result_is_failure(result):
            bump_write_tool_failure(function_name)
        return result

    def _await_owner_and_resume(
        self, proposal: ToolCallProposal, conversation_id: str
    ) -> str:
        """Bloquea el hilo de la conversación hasta la decisión del dueño y reanuda.

        El broker ya registró la fila pendiente (la tarjeta aparece en el chat). Aquí
        usamos el MISMO registro de Events que el hook de native dangers — approve_action /
        reject_action señalan este proposal_id. Al aprobar, re-despachamos la MISMA propuesta
        con el token aprobado (se ejecuta una vez, idempotencia del broker protege).
        Fail-closed: gate/loop ausente, timeout, deny o error → mensaje de bloqueo (no ejecuta).
        """
        from hermes.runtime.security_hook import (  # noqa: PLC0415
            _NATIVE_DANGER_OWNER_WAIT_S,
            _register_pending_event,
            _unregister_pending_event,
        )

        gate = getattr(self._broker, "_approval_gate", None)
        engine_loop = self._engine_loop
        if gate is None or engine_loop is None:
            return json.dumps(
                {"error": "BLOCKED: buzón de aprobaciones no disponible (fail-closed)."},
                ensure_ascii=False,
            )

        pid = proposal.proposal_id
        pid_str = str(pid)

        def _await(coro: Any) -> Any:
            return asyncio.run_coroutine_threadsafe(coro, engine_loop).result(
                timeout=_BROKER_DISPATCH_TIMEOUT_S
            )

        event = _threading.Event()
        slot: dict = {"event": event, "choice": None}
        _register_pending_event(pid_str, slot)

        choice: str | None = None
        try:
            # Carrera: si el dueño aprobó entre register_pending y este registro del Event,
            # la señal se perdió pero el token ya existe → procede directo sin esperar.
            try:
                if _await(gate.approved_token_for(pid)) is not None:
                    choice = "approved"
            except Exception:  # noqa: BLE001
                pass

            if choice is None:
                logger.info(
                    "hermes.nous_engine.write_block_and_resume_wait: proposal=%s tool=%s "
                    "— retiene el hilo del chat hasta decisión del dueño (timeout=%ss)",
                    pid_str, proposal.tool_name, _NATIVE_DANGER_OWNER_WAIT_S,
                )
                resolved = event.wait(timeout=_NATIVE_DANGER_OWNER_WAIT_S)
                if not resolved:
                    # Timeout: caduca la fila para que NO quede tarjeta fantasma.
                    try:
                        _await(gate.expire(proposal_id=pid))
                    except Exception:  # noqa: BLE001
                        pass
                    return json.dumps(
                        {"error": "Tiempo de espera agotado: el dueño no aprobó la acción a tiempo."},
                        ensure_ascii=False,
                    )
                choice = slot.get("choice")
        finally:
            _unregister_pending_event(pid_str, slot)

        if choice != "approved":
            return json.dumps(
                {"error": f"El dueño rechazó la acción '{proposal.tool_name}'. No la reintentes."},
                ensure_ascii=False,
            )

        # Aprobado → recupera el token aprobado y RE-DESPACHA la MISMA propuesta (ejecuta).
        token: str | None = None
        try:
            token = _await(gate.approved_token_for(pid))
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "hermes.nous_engine.write_block_and_resume_approved: proposal=%s tool=%s "
            "— ejecutando la llamada exacta tras aprobación del dueño",
            pid_str, proposal.tool_name,
        )
        outcome = _dispatch_via_bridge(
            proposal=proposal,
            broker=self._broker,
            consent_context=self._consent_context,
            engine_loop=engine_loop,
            conversation_id=conversation_id,
            hitl_approval_token=token,
        )
        return self._handle_outcome(proposal, outcome)

    def _handle_outcome(self, proposal: ToolCallProposal, outcome: Any) -> str:
        """Traduce ExecutionOutcome → tool_result string para Nous.

        EXECUTED_OK   → resultado real como JSON (el broker ya ejecutó).
        PENDING       → BLOCKED + acumula proposal para el orquestador.
        REJECTED/FAIL → BLOCKED con razón.
        """
        from hermes.capabilities.domain.ports import ExecutionStatus  # noqa: PLC0415

        status = outcome.status

        if status is ExecutionStatus.EXECUTED:
            result_data = outcome.result or {"ok": True}
            logger.info(
                "hermes.nous_engine.write_executed: tool=%s proposal=%s",
                proposal.tool_name,
                proposal.proposal_id,
            )
            return json.dumps(result_data, ensure_ascii=False, default=str)

        if status is ExecutionStatus.PENDING_APPROVAL:
            self._pending_proposals.append(proposal)
            logger.info(
                "hermes.nous_engine.write_pending_hitl: tool=%s proposal=%s",
                proposal.tool_name,
                proposal.proposal_id,
            )
            return json.dumps({
                "error": (
                    "BLOCKED: pendiente de aprobación HITL; "
                    "se reintentará tras aprobación"
                )
            }, ensure_ascii=False)

        # REJECTED_BY_POLICY / REJECTED_BY_CONSENT / FAILED
        reason = outcome.error or str(status)
        logger.info(
            "hermes.nous_engine.write_rejected: tool=%s reason=%s",
            proposal.tool_name,
            reason,
        )
        return self._blocked(proposal.tool_name, reason)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _blocked(tool_name: str, reason: str) -> str:
        return json.dumps(
            {"error": f"BLOCKED: {reason}"},
            ensure_ascii=False,
        )

    def run_conversation(
        self,
        user_message: str,
        system_message: str | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        task_id: str | None = None,
        stream_callback: Callable[..., None] | None = None,
        persist_user_message: str | None = None,
    ) -> dict[str, Any]:
        """Delega run_conversation en el inner AIAgent."""
        return self._inner.run_conversation(
            user_message,
            system_message,
            conversation_history,
            task_id,
            stream_callback,
            persist_user_message,
        )


def _wrap_handler_with_account(
    original_handler: Any,
    connected_account_id: str,
) -> Any:
    """DEPRECATED — no longer called by _rebind_spec (B1 fix).

    Previous implementation: wrapped the original handler but delegated to it
    unchanged, so connected_account_id=None from the original closure was never
    replaced. This was the READ-path bug (the account never reached the broker).

    Replaced by: _rebind_spec now calls make_broker_read_handler directly with
    the correct connected_account_id, bypassing this wrapper entirely.

    Kept to avoid breaking any external test imports; may be removed in a
    future cleanup pass.
    """
    async def _wrapped(params: dict) -> dict:
        return await original_handler(params)

    return _wrapped


class NousReasoningEngine:
    """ReasoningEngine que delega en hermes-agent (NousResearch AIAgent).

    Implementa el Protocol ReasoningEngine. Garantiza las mismas invariantes
    que LiteLLMReasoningEngine:
      1. WRITE nunca se ejecuta nativo: el broker intercepta en AMBOS caminos
         de dispatch de tools (concurrent via _invoke_tool; sequential via
         registry wrappers instalados por _wire_sequential_gate en __init__).
      2. Tokeniza PII antes de pasar contenido al agente.
      3. domain_payload (UNTRUSTED) nunca viaja en el system prompt.
      4. Rehidrata PII placeholders → valores reales en la narrative.

    F2: CycleOutput.tool_call_proposals incluye:
      - Proposals PENDING_APPROVAL (acumuladas durante run_conversation).
      - read_external_content=True si alguna READ ingirió contenido externo.
      Proposals EXECUTED/REJECTED ya fueron resueltas in-loop; no se re-surfacean.

    Configuración del provider de Nous:
        Reutiliza HERMES_MODEL / HERMES_API_KEY / HERMES_MODEL_BASE_URL
        (mismos env vars que LiteLLMReasoningEngine, mapeados a los kwargs
        equivalentes de AIAgent: model / api_key / base_url).
        O ModelConfig explícito al construir.
    """

    def __init__(
        self,
        *,
        persona: PersonaSpec,
        prompt_builder: PromptBuilder | None = None,
        tokenizer: PIITokenizer | None = None,
        model_config: ModelConfig | None = None,
        model_config_source: Callable[[], ModelConfig | None] | None = None,
        model_config_for_alias: "Callable[[str], ModelConfig | None] | None" = None,
        enabled_toolsets: list[str] | None = None,
        broker: CapabilityBrokerPort | None = None,
        consent_context: ConsentContext | None = None,
        tenant_id: UUID | None = None,
        tools_source: Callable[[], Any] | None = None,
        capability_consent_ref: "list[ConsentContext] | None" = None,
        agent_registry: Any | None = None,
        composio_connection_repo: Any | None = None,
        capability_binding_repo: Any | None = None,
        cerebro_browser_manager: Any | None = None,
        jailed_browser_manager: Any | None = None,
    ) -> None:
        self._persona = persona
        self._prompt_builder = prompt_builder or DefaultPromptBuilder()
        # Do NOT tokenize ACTIONABLE identifiers the user hands the agent (email,
        # phone): the model must use them verbatim in tool args (send/message), and
        # a weak model won't reliably carry a [[EMAIL_1]] placeholder through — it
        # would message the wrong target. Financial/ID PII (NIF/IBAN/…) stays
        # tokenized (and is rehydrated at the external-dispatch boundary if used).
        # Overridable via HERMES_PII_UNTOKENIZED (comma-separated pattern names).
        from hermes.tokenizer.pii import actionable_pii_exclusions  # noqa: PLC0415
        self._tokenizer = tokenizer or DefaultPIITokenizer(
            exclude_patterns=actionable_pii_exclusions()
        )
        self._model_config = model_config
        # Per-cycle resolution of the ACTIVE Hermes provider (providers table +
        # SecretsVault), set in onboarding/Settings. Without this the Nous engine
        # only saw HERMES_MODEL env (unset on the desktop image) and run_cycle
        # raised model-not-configured. Mirrors LiteLLMReasoningEngine's source.
        self._model_config_source = model_config_source
        # Per-agent provider binding (Fase 3c): callable(alias) → ModelConfig|None.
        # When an agent declares a provider_alias, this callable resolves its
        # credentials from the vault. None = feature not wired (retro-compat).
        self._model_config_for_alias = model_config_for_alias
        # enabled_toolsets=None → Nous decide (usa sus defaults). En F2
        # habilitamos el catálogo completo porque el gate clasifica/controla.
        self._enabled_toolsets = enabled_toolsets
        self._broker = broker
        self._consent_context = consent_context
        self._tenant_id = tenant_id
        # F3: async callable → tuple[ToolSpec, ...] (native + composio + mcp).
        # Resuelto PER CYCLE para recoger integraciones recién conectadas.
        self._tools_source: Callable[[], Any] | None = tools_source
        # spec 014 inc. 3 (CTRL-13 fix): mutable reference shared with all
        # READ handlers built by build_capability_tool_specs. Updated per-cycle
        # with the real operator_id from the task's enqueued_by (CWE-862 safe).
        # None if capability specs are not wired (e.g. test environments).
        self._capability_consent_ref: "list[ConsentContext] | None" = capability_consent_ref
        # B4: filtrado runtime de tools por agente activo.
        # agent_registry: resuelve el agente activo cuando DecisionContext.agent_id es None.
        # composio_connection_repo: lista conexiones asignadas al agente (multi-account).
        # capability_binding_repo: lista bindings skill/mcp asignados al agente.
        # Sin repos → fail-open (todas las tools disponibles, retrocompat total).
        self._agent_registry = agent_registry
        self._composio_connection_repo = composio_connection_repo
        self._capability_binding_repo = capability_binding_repo
        # Dual-browser: headed Chromium for the Cerebro (default agent).
        # None = dual-browser disabled (CI, tests, configs without a display).
        self._cerebro_browser_manager = cerebro_browser_manager
        # Jailed headless Chromium in hermes-browser netns (terminal / jail mode).
        # When set (HERMES_BROWSER_JAIL=1), ALL agent cycles use this instead of
        # spawning an unconfined local session.
        self._jailed_browser_manager = jailed_browser_manager
        # Chat D-Bus streaming emitters (spec streaming-dbus).
        # Injected by DbusRuntimeAdapter.start() after the bus connects.
        # None = no D-Bus streaming (poller fallback works as before).
        self._dbus_emit_delta: "Callable[[str, int, str], None] | None" = None
        self._dbus_emit_end: "Callable[[str], None] | None" = None
        # Per-cycle tool_call frame emitter (set in run_cycle, cleared after).
        # None when no chunk_sink is wired (tests, non-streaming contexts).
        self._chunk_sink_emitter: "Callable[[str, dict[str, Any]], None] | None" = None
        # Install thread-local CDP override once at engine construction.
        # Idempotent: safe to call even when Nous is not installed (logs + no-op).
        install_thread_local_cdp_override()
        # Install the jail seatbelt: prevents unconfined host-netns Chromium spawns
        # when HERMES_BROWSER_JAIL=1. Idempotent; fail-soft without Nous.
        from hermes.runtime.cycle_cdp_context import (  # noqa: PLC0415
            install_jail_block_local_session,
        )
        install_jail_block_local_session()

    def set_chat_delta_emitter(
        self,
        emit_delta: "Callable[[str, int, str], None]",
        emit_end: "Callable[[str], None]",
    ) -> None:
        """Inject D-Bus chat streaming emitters (spec streaming-dbus).

        emit_delta(conversation_id, seq, text): called per coalesced batch.
        emit_end(conversation_id): called once at end of generation.
        Both callables are thread-safe; called from the executor thread.
        """
        self._dbus_emit_delta = emit_delta
        self._dbus_emit_end = emit_end

    def _resolve_model_config(self, agent_id: str | None = None) -> ModelConfig:
        # Per-agent provider binding (Fase 3c): if the agent has a provider_alias,
        # resolve that specific provider before falling back to the global path.
        if agent_id and self._agent_registry is not None and self._model_config_for_alias is not None:
            alias = self._agent_provider_alias(agent_id)
            if alias:
                per_agent_cfg = self._model_config_for_alias(alias)
                if per_agent_cfg is not None:
                    logger.info(
                        "hermes.nous_engine.per_agent_provider: "
                        "agent_id=%s alias=%s model=%s",
                        agent_id,
                        alias,
                        per_agent_cfg.model,
                    )
                    return per_agent_cfg

        if self._model_config is not None:
            return self._model_config
        # Prefer the active Hermes provider (onboarding/Settings), resolved per
        # cycle so connecting/switching a provider in the UI takes effect on the
        # next task without restarting the daemon. Fall back to env only if no
        # provider is configured and no source was wired.
        if self._model_config_source is not None:
            cfg = self._model_config_source()
            if cfg is not None:
                return cfg
        return ModelConfig.from_env()

    def _agent_provider_alias(self, agent_id: str) -> str | None:
        """Return the provider_alias for the given agent_id, or None (fail-soft)."""
        try:
            agent = self._agent_registry.get_agent(agent_id)
            return agent.provider_alias or None
        except Exception:  # noqa: BLE001 — fail-soft: broken lookup → global fallback
            return None

    @staticmethod
    def _chat_system_prompt(persona: "PersonaSpec") -> str:
        """System prompt CONVERSACIONAL para chat (no tarea autónoma).

        El system prompt por defecto fuerza "solo PROPONES acciones invocando
        tools, NUNCA en prosa" → en chat el modelo se negaba a responder y hablaba
        de la mecánica interna. Aquí construimos un asistente personal que conversa
        en prosa, usa tools solo si ayudan, y jamás filtra mecánica/IDs internos.
        """
        name = getattr(persona, "name", "") or "Lumen"
        lang = getattr(persona, "language", "") or "es-ES"
        # Framing POSITIVO: describir lo que SÍ hace. Enumerar términos prohibidos
        # ("no hables de queue_drain...") los PRIMA en la respuesta (priming inverso).
        lines = [
            f"Eres {name}, el asistente personal del usuario; vives en su propio equipo "
            "y le ayudas con lo que necesite (buscar, organizar, redactar, recordar, "
            "automatizar tareas).",
            f"Hablas el idioma del usuario (por defecto {lang}) y respondes de forma "
            "directa, cálida y natural, en prosa, como un buen asistente humano.",
            "Cuando el usuario pregunta o conversa, le respondes al grano y con criterio. "
            "Usas tus herramientas solo cuando aportan; para una pregunta simple, "
            "respondes directamente.",
            "Hablas siempre en términos del usuario y del mundo real. Tu lenguaje es el "
            "de una persona, no el de un sistema.",
            "Cuidas la privacidad: nada sale del equipo sin permiso explícito del usuario.",
            # Tool-selection rule: la distinción CRÍTICA es VISIBLE vs headless.
            # "abrir el navegador / una web para que el usuario la VEA" = activate_app
            # con url (Chromium en la sesión gráfica). browser_navigate es headless
            # (invisible) y solo sirve para que el AGENTE lea/opere la web por dentro.
            # Nombres de tools reales (no inventar): activate_app, browser_navigate,
            # browser_click, browser_type, browser_snapshot, terminal, computer_use.
            "Regla de herramientas (elige bien): para que el usuario VEA algo en "
            "pantalla —abrir una app (calculadora, editor, visor) o el navegador en "
            "una web— usa activate_app. Para el navegador pasa la url: "
            "activate_app(app_name='navegador', url='https://www.youtube.com') abre "
            "Chromium VISIBLE en esa web. 'abre el navegador', 'abre YouTube', "
            "'muéstrame X web', 'abre la calculadora' = activate_app (con url si es web). "
            "Para LEER o automatizar una web por dentro SIN mostrarla (scrapear, "
            "rellenar un form en background, comprobar un dato) usa browser_navigate + "
            "browser_click/browser_type/browser_snapshot (navegador agéntico headless, "
            "invisible — NO se ve en pantalla). Nunca uses browser_navigate ni terminal "
            "para abrir algo que el usuario deba VER. Comandos del sistema → terminal; "
            "ficheros → read_file/write_file/patch; control de pantalla → computer_use. "
            "Usa siempre la herramienta más simple y directa.",
            # Aprobaciones: cuando una herramienta requiere permiso del dueño, el sistema
            # muestra automáticamente una tarjeta de aprobación (HITL). El agente NUNCA
            # debe negarse ni proponer un rodeo: debe invocar la herramienta y, si el sistema
            # la encola para aprobación, informar al usuario de que queda pendiente de su
            # visto bueno y que la ejecutará en cuanto la apruebe. Tienes el SO entero a tu
            # disposición; siempre intenta usar la herramienta adecuada.
            "Siempre intenta invocar la herramienta adecuada para lo que el usuario pide. "
            "Si la acción requiere permiso del dueño, el sistema mostrará una tarjeta de "
            "aprobación; tú informa de que queda pendiente de su visto bueno y la "
            "completarás en cuanto la apruebe. Nunca propongas un rodeo ni digas que no "
            "puedes hacer algo.",
            # Workspace: entregables del usuario van a /var/lib/hermes/workspace/
            "Cuando generes algo PARA EL USUARIO — imagen, PDF, Word, PowerPoint, "
            "Excel, captura de pantalla, export o cualquier fichero que deba ver, "
            "abrir o descargar — guárdalo en `/var/lib/hermes/workspace/` con un "
            "nombre claro y menciona el nombre en tu respuesta. Así aparecerá en el "
            "chat y en la carpeta Works para que el usuario lo vea, abra y descargue.",
            # MCP discovery: las integraciones MCP (mcp__*) pueden ser cientos y NO
            # se listan sueltas para no saturar el contexto — viven detrás de
            # tool_search/tool_call (puente nativo del runtime). El modelo a veces
            # no las usa por desconocerlas; este recordatorio (framing positivo) las
            # ancla como herramientas reales. Memoria: feedback_positive_prompt_framing.
            "Además de las herramientas que ves directamente, tienes acceso a muchas "
            "integraciones externas conectadas (MCP) —por ejemplo orquestar enjambres "
            "de agentes, servicios y APIs— cuyas herramientas se llaman `mcp__...`. "
            "Para no saturar tu lista, esas herramientas viven DETRÁS de `tool_search`: "
            "`tool_search` y `tool_call` SON herramientas reales tuyas. Cuando necesites "
            "una capacidad que no ves directamente en tu lista, usa `tool_search` con "
            "palabras clave (p.ej. 'swarm', 'agent', el nombre del servicio) para "
            "descubrir la herramienta MCP adecuada, y luego `tool_call` para ejecutarla. "
            "Úsalas con confianza: es así como accedes a todo lo que el usuario ha "
            "conectado.",
            # Distinción CRÍTICA: search_mcp (Hub) = INSTALAR MCPs nuevos;
            # tool_search/tool_call = USAR los YA conectados. Qwen los confunde.
            "IMPORTANTE: distingue dos cosas. Para USAR una integración que YA está "
            "conectada (p.ej. el swarm 'ruflo', cuyas herramientas son `mcp__ruflo__*`): "
            "usa `tool_search`+`tool_call` — esas herramientas YA están en tu sistema, "
            "NO en ningún hub. `search_mcp` es OTRA cosa: solo sirve para INSTALAR un MCP "
            "NUEVO desde el catálogo; NUNCA uses `search_mcp` para usar ruflo u otra "
            "integración ya conectada (ahí no aparecerán). Si el usuario te pide operar "
            "ruflo/un swarm/una app conectada, ve directo a `tool_search` con su nombre.",
        ]
        golden = getattr(persona, "golden_rules", ()) or ()
        if golden:
            lines.append("Reglas:")
            lines.extend(f"- {r}" for r in golden)
        forbidden = getattr(persona, "forbidden_phrases", ()) or ()
        if forbidden:
            lines.append("Nunca uses estas frases: " + "; ".join(forbidden) + ".")
        return "\n".join(lines)

    def _resolve_cycle_persona(self, agent_id: str | None) -> PersonaSpec:
        """Resolve the PersonaSpec for this cycle from the agent_registry.

        Priority: context.agent_id → active_agent_id() → engine's base persona.
        Always returns a valid PersonaSpec (fail-soft by design — a broken
        persona resolution must never crash the reasoning cycle).
        """
        if self._agent_registry is None:
            return self._persona
        try:
            return self._agent_registry.persona_for(agent_id)
        except Exception:  # noqa: BLE001 — fail-soft
            logger.warning(
                "hermes.nous_engine.cycle_persona_fallback agent_id=%s", agent_id
            )
            return self._persona

    async def run_cycle(self, context: DecisionContext) -> CycleOutput:
        """Ejecuta un ciclo de razonamiento delegando en AIAgent headless.

        Pasos:
          1. Tokenizar PII del contexto.
          2. Mapear DecisionContext → (user_message, ephemeral_system_prompt).
             domain_payload (UNTRUSTED) va envuelto en el user_message.
          3. Resolver ToolSpecs externos (Composio + MCP) desde tools_source (F3).
          4. Construir GovernedAIAgent headless con broker + gate + external_catalog.
          5. Registrar ToolSpecs externos en el Nous registry (para que el LLM
             los descubra en function-calling). Los handlers despachan al broker.
          6. Llamar run_conversation en executor (AIAgent es síncrono).
          7. Mapear resultado → CycleOutput. Rehidratar PII.
             CycleOutput.tool_call_proposals = proposals PENDING_APPROVAL del ciclo.
        """
        # FIX MCP discovery: invalida el memo de model_tools al inicio del ciclo
        # para que el schema de function-calling del agente recompute con el
        # estado ACTUAL del registry (incluye las tools MCP registradas por el
        # servidor MCP al conectar). Sin esto, un resultado cacheado de antes de
        # conectar el MCP deja al agente sin descubrir mcp__*.
        try:
            import model_tools as _mt  # noqa: PLC0415
            if hasattr(_mt, "_tool_defs_cache"):
                _mt._tool_defs_cache.clear()
        except Exception:  # noqa: BLE001
            pass

        cycle_agent_id: str | None = context.agent_id if hasattr(context, "agent_id") else None

        model_config = self._resolve_model_config(cycle_agent_id)

        # Resolve per-cycle persona: use the agent bound to this task (from
        # DecisionContext.agent_id) or the active agent. Falls back to the
        # engine's persona (default agent) when agent_registry is absent.
        # The registry's persona_for() is fail-soft by contract: never raises.
        cycle_persona = self._resolve_cycle_persona(cycle_agent_id)

        tokenized_payload = self._tokenize_context(context)
        safe_context = _replace_context(context, tokenized_payload)
        mapping = tokenized_payload.mapping

        system_prompt, user_message = self._prompt_builder.build(
            safe_context, cycle_persona
        )

        # GATE 0 — CHAT es CONVERSACIÓN, no tarea autónoma. El prompt builder por
        # defecto envuelve el mensaje con "Trigger del ciclo: queue_drain:chat_message",
        # tenant, cycle_id y "propon acciones via tool_calls" → el modelo respondía
        # hablando de la cola/los ciclos en vez de conversar. Para un chat_message
        # presentamos el mensaje del usuario como turno conversacional directo
        # (la identidad + reglas duras del system prompt se mantienen). El texto va
        # YA tokenizado (PII protegida, Constitución III) y se rehidrata en la
        # respuesta. domain_payload["instruction"] = el mensaje del usuario.
        if "chat_message" in (safe_context.trigger or ""):
            # El texto del chat viaja en operator_instruction (CONFIABLE, el
            # usuario ES el operador) cuando la tarea NO está tainted; si lo
            # estuviera, cae dentro de domain_payload["instruction"] (untrusted).
            _chat_text = (safe_context.operator_instruction or "").strip()
            if not _chat_text:
                _dp = safe_context.domain_payload
                if isinstance(_dp, dict):
                    _chat_text = str(_dp.get("instruction", "")).strip()
            if _chat_text:
                user_message = _chat_text
                # FIX D — cache system prompt keyed by (engine_id, persona_id).
                system_prompt = _cached_chat_system_prompt(id(self), cycle_persona)

        loop = asyncio.get_event_loop()
        tenant_id = self._tenant_id or context.tenant_id

        # spec 014 inc. 3 (CTRL-13 fix): resolve per-cycle operator_id from metadata.
        # The orchestrator injects "task_operator_id" (a UUID) from
        # item.payload["enqueued_by"] — set server-side from channel.sender_uid
        # (CTRL-P1-3 / CWE-862). This overrides the daemon-level consent_context
        # that may have operator_id=None (HERMES_OPERATOR_ID absent at boot).
        # INVARIANT: the source is always the orchestrator-injected UUID, never
        # any LLM-controlled parameter. The engine cannot fabricate an operator_id.
        per_cycle_consent = _resolve_per_cycle_consent(self._consent_context, context)

        # Update the shared consent_ref so READ handlers pick up the per-cycle
        # operator_id. This propagates to all capability READ handler closures
        # (lo_open_document, list_dir, get_service_status, etc.) without
        # rebuilding the ToolSpec objects. The update happens BEFORE the agent is
        # built and BEFORE run_conversation is called — single-threaded path
        # (one cycle at a time via run_in_executor).
        if per_cycle_consent is not None and self._capability_consent_ref is not None:
            self._capability_consent_ref[0] = per_cycle_consent

        # F3: resolve external ToolSpecs filtered per active agent (B4).
        # agent_id from DecisionContext (CTRL-secure: set server-side from WorkItem).
        # Falls back to DEFAULT_AGENT_ID (CEO) — never reads the global active_agent.
        from hermes.agents.domain.agent import DEFAULT_AGENT_ID as _DEFAULT_AGENT_ID  # noqa: PLC0415

        active_agent_id = context.agent_id if hasattr(context, "agent_id") else None
        if active_agent_id is None:
            active_agent_id = _DEFAULT_AGENT_ID
        # FIX B.1 — wire streaming: extract chunk_sink injected by the orchestrator
        # and build a sync callback that emits incremental tokens to the client.
        # The counting_sink wrapper (injected by the orchestrator) increments
        # delta_count on each emit — FIX B.2 reads that count post-cycle to decide
        # whether to skip the monolithic fallback re-emit.
        #
        # spec streaming-dbus: also wire the D-Bus ChatDelta emitter when:
        #   (a) the injected metadata contains a "conversation_id" string, AND
        #   (b) self._dbus_emit_delta was injected by DbusRuntimeAdapter.start().
        # The conversation_id is injected by the orchestrator (same source as
        # item.payload["conversation_id"]) — never from LLM output (CWE-862 safe).
        #
        # Observability (tool_call frames): _chunk_sink_emitter is built here and
        # stored on self so _build_governed_agent can pass it to GovernedAIAgent.
        # It is cleared after this cycle to avoid retaining a stale reference.
        _meta = getattr(context, "metadata", None) or {}
        _chunk_sink = _meta.get("chunk_sink")
        _task_id_for_stream = _meta.get("task_id_for_stream")
        _conv_id_for_dbus: str = (_meta.get("conversation_id") or "").strip()
        _emit_counter: list[int] = [0]  # local counter — harmless, unused by orchestrator
        # Per-cycle accumulator of tool-call descriptors (same shape as the live
        # TOOL_CALL frame), filled by the emitter and surfaced on CycleOutput.tool_steps
        # so the orchestrator can persist them for conversation reloads.
        _tool_steps_acc: list[dict[str, Any]] = []
        _stream_cb = None
        if _chunk_sink is not None and _task_id_for_stream is not None:
            _stream_cb = _build_stream_callback(
                _chunk_sink, _task_id_for_stream, loop, _emit_counter,
                dbus_emit_delta=self._dbus_emit_delta if _conv_id_for_dbus else None,
                conversation_id=_conv_id_for_dbus,
            )
            # Build the tool_call frame emitter from the same chunk_sink/task_id/loop.
            # Pass live_agent_id so each dispatch also updates the live_activity registry.
            self._chunk_sink_emitter: "Callable[[str, dict[str, Any]], None] | None" = (
                _build_tool_call_emitter(
                    _chunk_sink, _task_id_for_stream, loop, accumulator=_tool_steps_acc,
                    live_agent_id=active_agent_id or "",
                )
            )
        else:
            self._chunk_sink_emitter = None

        # Intent-based tool retrieval: stamp THIS turn's message so _tools_source
        # (resolved just below, in THIS event-loop thread) can rank connected-
        # integration tools by intent and surface only the top-K. Must be set here,
        # not in the run_in_executor cycle body — that runs in a different thread the
        # ContextVar would not reach, and after specs are already resolved.
        from hermes.runtime.conversation_task_registry import (  # noqa: PLC0415
            set_current_message as _set_current_message,
        )
        _set_current_message(user_message or "")

        external_specs = await self._resolve_external_specs(active_agent_id)
        external_catalog = _ExternalToolCatalog(external_specs)

        agent = self._build_governed_agent(
            model_config, system_prompt, loop, tenant_id, external_catalog,
            consent_context=per_cycle_consent,
            active_agent_id=str(active_agent_id) if active_agent_id else "",
            pii_mapping=mapping,
        )
        _register_external_specs_in_nous(external_specs, agent)
        # COLD-CYCLE FIX: agent_init captured agent.tools + agent.valid_tool_names
        # from the Nous registry BEFORE the line above registered this cycle's
        # externals (Composio/MCP). The Nous registry is process-global, so a warm
        # daemon happens to see them (registered by a PRIOR cycle) — but the FIRST
        # request after every daemon boot builds the agent against an empty external
        # set and the model NEVER receives the tools ("no las veo"), until the next
        # cycle warms the registry. Sync the just-registered specs into THIS agent so
        # the tools reach the model on the same cycle they were resolved. Idempotent:
        # warm cycles already have them and skip.
        _sync_agent_tools_with_external(agent, external_specs)

        # FIX "Hermes se presenta cada mensaje": el orchestrator inyecta el
        # historial de la conversación en metadata; lo pasamos a run_conversation
        # para que el agente responda EN CONTEXTO (antes history=0 → saludo cada vez).
        _history = None
        try:
            _md = getattr(context, "metadata", None)
            if isinstance(_md, dict):
                _history = _md.get("conversation_history")
        except Exception:  # noqa: BLE001
            _history = None

        # Dual-browser: Cerebro (default agent) → headed browser via CDP, lanzado
        # LAZY (solo si el agente usa una tool de navegador; ver provider).
        # Workers (non-default agents) → headless isolated session (unchanged).
        cerebro_cdp_provider = self._make_cerebro_cdp_provider(active_agent_id, loop)

        # Artifact auto-attach (chat_message path only): snapshot workspace BEFORE
        # the agent runs so the delta is precisely what THIS cycle created/modified.
        # FIX E — skip snapshot for cycles that cannot produce workspace files.
        # We detect file tools POST-run from output.tool_call_proposals, but we
        # still need the pre-run snapshot when the tool MIGHT write (chat path).
        # The skip happens POST-run: if no file-writing tool ran, we skip delta.
        is_chat_cycle = "chat_message" in (safe_context.trigger or "")
        workspace_snapshot: dict[str, float] = (
            _snapshot_workspace() if is_chat_cycle else {}
        )

        result = await loop.run_in_executor(
            None,
            lambda: _run_conversation_with_cdp(
                agent, user_message, _history, cerebro_cdp_provider,
                stream_callback=_stream_cb,
                conversation_id=_conv_id_for_dbus,
            ),
        )

        # spec streaming-dbus: flush any remaining coalesced text and emit
        # ChatStreamEnd to signal completion to the compositor.
        if _stream_cb is not None and _conv_id_for_dbus:
            _flush = getattr(_stream_cb, "_flush_dbus", None)
            if callable(_flush):
                try:
                    _flush(force=True)
                except Exception:  # noqa: BLE001
                    logger.debug("hermes.nous_engine.dbus_flush_failed conv=%s", _conv_id_for_dbus)
            if self._dbus_emit_end is not None:
                try:
                    self._dbus_emit_end(_conv_id_for_dbus)
                except Exception:  # noqa: BLE001
                    logger.debug("hermes.nous_engine.dbus_stream_end_failed conv=%s", _conv_id_for_dbus)

        output = self._map_result_to_output(
            result, mapping, model_config.model, agent,
            tool_steps=tuple(_tool_steps_acc),
        )

        # Post-run: compute delta and deterministically attach MEDIA tokens for any
        # files the agent created/modified that the LLM didn't mention in its reply.
        # FIX E — skip workspace diff when no file-writing tool ran in this cycle.
        if is_chat_cycle and _did_cycle_write_files(output):
            try:
                new_paths = _workspace_delta(workspace_snapshot)
                enriched = _attach_artifacts(output.narrative, new_paths)
                if enriched is not output.narrative:
                    import dataclasses as _dc  # noqa: PLC0415
                    output = _dc.replace(output, narrative=enriched)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "hermes.nous_engine.artifact_attach_error", exc_info=True
                )

        # Clear per-cycle chunk_sink emitter reference so it does not hold
        # the chunk_sink alive after the cycle completes.
        self._chunk_sink_emitter = None

        return output

    def _make_cerebro_cdp_provider(self, agent_id: str | None, loop: Any):
        """Devuelve un proveedor LAZY de CDP para el ciclo activo, o None.

        Precedence (first match wins):
          1. Jailed browser (HERMES_BROWSER_JAIL=1 + jailed_browser_manager set):
             applies to ALL agents — the jail is the single confined browser for
             every cycle in terminal/server form-factors. The eager start in
             __main__ means ensure_running() is a fast no-op most of the time;
             we call it defensively in case the browser crashed.
          2. Cerebro headed browser (cerebro_browser_manager set, agent is Cerebro):
             desktop form-factor, visible on Wayland.
          3. None — headless fallback (browser_tool spawns its own session).

        El provider corre en el hilo del executor; ensure_running es async →
        puente al event loop del engine vía run_coroutine_threadsafe.
        """
        import os as _os  # noqa: PLC0415 — avoid module-level OS coupling

        # ── Priority 1: jailed browser (all agents, terminal / no-display) ────
        if (
            self._jailed_browser_manager is not None
            and _os.environ.get("HERMES_BROWSER_JAIL", "1") == "1"
        ):
            mgr = self._jailed_browser_manager

            def _jailed_provider() -> str | None:
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        mgr.ensure_running(), loop
                    )
                    fut.result(timeout=25.0)
                    cdp_url = mgr.cdp_url
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "hermes.nous_engine.jailed_browser.lazy_start_failed "
                        "— headless fallback blocked by seatbelt",
                        exc_info=True,
                    )
                    return None
                if cdp_url is None:
                    logger.warning(
                        "hermes.nous_engine.jailed_browser.no_cdp_url "
                        "— headless fallback blocked by seatbelt"
                    )
                return cdp_url

            return _jailed_provider

        # ── Priority 2: Cerebro headed browser (desktop form-factor) ──────────
        if self._cerebro_browser_manager is None:
            return None
        if not self._is_cerebro_agent(agent_id):
            return None
        mgr = self._cerebro_browser_manager

        def _provider() -> str | None:
            # Corre en el hilo del executor; ensure_running es async → puente al
            # loop del engine. Idempotente: si el navegador ya está vivo, retorna
            # rápido (no relanza). Fail-soft: cualquier error → headless fallback.
            try:
                fut = asyncio.run_coroutine_threadsafe(mgr.ensure_running(), loop)
                fut.result(timeout=25.0)  # cold-start del chromium (poll ~20s)
                cdp_url = mgr.cdp_url
            except Exception:  # noqa: BLE001
                logger.warning(
                    "hermes.nous_engine.cerebro_browser.lazy_start_failed — headless fallback",
                    exc_info=True,
                )
                return None
            if cdp_url is None:
                logger.warning(
                    "hermes.nous_engine.cerebro_browser.no_cdp_url — headless fallback"
                )
            return cdp_url

        return _provider

    def _is_cerebro_agent(self, agent_id: str | None) -> bool:
        """True when the active agent is the default Cerebro.

        Fast path: compare against DEFAULT_AGENT_ID constant.
        Registry lookup: only when agent_id is not the constant (custom default).
        """
        from hermes.agents.domain.agent import DEFAULT_AGENT_ID  # noqa: PLC0415

        if agent_id is None:
            # No agent_id resolved → conservative: treat as non-Cerebro.
            return False
        if agent_id == DEFAULT_AGENT_ID:
            return True
        # Slow path: check is_default flag from registry (custom agents set as default).
        if self._agent_registry is not None:
            try:
                agent = self._agent_registry.get_agent(agent_id)
                return bool(getattr(agent, "is_default", False))
            except Exception:  # noqa: BLE001
                pass
        return False

    async def _resolve_external_specs(
        self, agent_id: str | None = None
    ) -> tuple[ToolSpec, ...]:
        """Resuelve ToolSpecs externos desde tools_source (F3) con filtrado por agente (B4)."""
        if self._tools_source is None:
            return ()
        try:
            all_specs: tuple[ToolSpec, ...] = await self._tools_source()
        except Exception as exc:  # noqa: BLE001
            logger.error("hermes.nous_engine.tools_source_error: %s — 0 external tools", exc)
            return ()
        external = tuple(s for s in all_specs if classify_nous_tool(s.name) is None)
        try:
            external = self._apply_agent_filter(external, agent_id)
        except Exception as exc:  # noqa: BLE001 — fail-safe: no dejar al agente sin tools
            logger.error(
                "hermes.nous_engine.agent_filter_error: %s — devolviendo todas las tools", exc
            )
        return external

    def _apply_agent_filter(
        self, external: tuple[ToolSpec, ...], agent_id: str | None
    ) -> tuple[ToolSpec, ...]:
        """Filtra y expande specs según las asignaciones del agente activo (B4).

        Retorna el mismo tuple si no hay repos ni agent_id (retrocompat total).
        """
        has_repos = (
            self._composio_connection_repo is not None
            or self._capability_binding_repo is not None
        )
        if agent_id is None or not has_repos:
            return external

        tenant_id = str(self._tenant_id) if self._tenant_id else ""

        # Cerebro (agente default) = OMNIPOTENTE: ve TODAS las tools externas
        # (MCP/skill/composio) sin filtro de bindings. El fail-closed por binding
        # aplica SOLO a agentes CUSTOM (restriccion por-agente, nunca al cerebro).
        # Sin esto, Hermes (Cerebro) NO descubria ninguna tool MCP/skill: el
        # fail-closed las descartaba todas por falta de binding (y el tenant del
        # binding ni siquiera casaba con self._tenant_id). Modelo del dueno: el
        # cerebro lo ve y puede todo; a los custom se les aprieta.
        from hermes.agents.domain.agent import DEFAULT_AGENT_ID  # noqa: PLC0415
        if agent_id != DEFAULT_AGENT_ID:
            # --- MCP / skill filtering (fail-CLOSED by kind) — solo agentes custom ---
            external = self._filter_mcp_skill(external, agent_id, tenant_id)

        # --- Composio: expand by assigned connections ---
        external = self._expand_composio(external, agent_id, tenant_id)

        return external

    def _filter_mcp_skill(
        self,
        specs: tuple[ToolSpec, ...],
        agent_id: str,
        tenant_id: str,
    ) -> tuple[ToolSpec, ...]:
        """Filtra specs MCP y skill según los bindings del agente.

        FAIL-CLOSED por kind: si el agente no tiene ningún binding de un kind
        (mcp o skill), NO se expone ninguna capability de ese kind. Solo las
        capabilities cuyo id aparece en los bindings del agente sobreviven al
        filtro. Las specs que no son ni MCP ni skill pasan sin tocar (su control
        de acceso vive en otra capa: Composio por conexión, os_surface por
        policy/broker).

        Rationale (confused-deputy): una capability MCP o una skill da al agente
        acceso lateral (red, FS, ejecución). El default debe ser denegar — un
        agente sin asignación explícita no hereda el catálogo completo del SO.
        """
        if self._capability_binding_repo is None:
            return specs

        bindings = self._capability_binding_repo.list_by_agent(agent_id, tenant_id)
        bound_mcp = {
            b.capability.capability_id
            for b in bindings
            if b.capability.kind == "mcp"
        }
        bound_skill = {
            b.capability.capability_id
            for b in bindings
            if b.capability.kind == "skill"
        }

        result = []
        for spec in specs:
            tags = set(spec.tags or ())
            if "mcp" in tags:
                # FAIL-CLOSED: sin bindings mcp → ninguna tool mcp.
                if self._mcp_spec_is_bound(spec, bound_mcp):
                    result.append(spec)
            elif spec.entity_type == "skill":
                # FAIL-CLOSED: sin bindings skill → ninguna skill.
                if self._skill_spec_is_bound(spec, bound_skill):
                    result.append(spec)
            else:
                result.append(spec)

        return tuple(result)

    @staticmethod
    def _mcp_spec_is_bound(spec: ToolSpec, bound_mcp: set[str]) -> bool:
        """True si el slug del server MCP de *spec* está en los bindings.

        Nombre MCP: ``mcp__<slug>__<tool>``. Sin bindings → siempre False
        (fail-closed). Un nombre malformado (sin slug) nunca matchea.
        """
        if not bound_mcp:
            return False
        parts = spec.name.split("__")
        slug = parts[1] if len(parts) >= 2 else ""
        return bool(slug) and slug in bound_mcp

    @staticmethod
    def _skill_spec_is_bound(spec: ToolSpec, bound_skill: set[str]) -> bool:
        """True si la skill de *spec* está en los bindings del agente.

        El ``capability_id`` de un binding skill es el ``package_id`` de la skill.
        Una skill ToolSpec se identifica por su ``name``; si sigue el patrón
        ``skill__<package_id>__<tool>`` (simétrico a MCP) se extrae el slug,
        si no se usa el ``name`` completo como identificador. Sin bindings →
        siempre False (fail-closed).
        """
        if not bound_skill:
            return False
        parts = spec.name.split("__")
        if len(parts) >= 2 and parts[0] == "skill":
            return parts[1] in bound_skill
        return spec.name in bound_skill

    def _expand_composio(
        self,
        specs: tuple[ToolSpec, ...],
        agent_id: str,
        tenant_id: str,
    ) -> tuple[ToolSpec, ...]:
        """Expande specs Composio por conexiones asignadas al agente.

        Si hay conexiones asignadas: cada spec Composio se instancia por conexión
        con el connected_account_id inyectado en el handler (B1) y el nombre
        desambiguado por alias para que el LLM elija la cuenta correcta.
        Si no hay conexiones: comportamiento actual (specs globales, sin pinning).
        """
        if self._composio_connection_repo is None:
            return specs

        conns = self._composio_connection_repo.list_by_agent(agent_id, tenant_id)
        if not conns:
            return specs  # fail-open: sin conexiones asignadas → todas las tools Composio

        aliases = self._composio_connection_repo.get_aliases()
        composio_specs = [s for s in specs if "composio" in (s.tags or ())]
        other_specs = [s for s in specs if "composio" not in (s.tags or ())]

        if not composio_specs:
            return specs

        expanded: list[ToolSpec] = []
        for conn in conns:
            suffix = aliases.get(conn.connected_account_id, "") or conn.connected_account_id[:8]
            for base_spec in composio_specs:
                # Match toolkit: base slug is toolkit_verb_noun; toolkit = parts[0]
                base_name = base_spec.name.split("__")[0]  # strip any existing suffix
                toolkit_from_spec = base_name.upper().split("_")[0]
                if toolkit_from_spec.lower() != conn.toolkit_slug.lower():
                    continue
                expanded.append(
                    self._rebind_spec(base_spec, conn.connected_account_id, suffix)
                )

        if not expanded:
            # No spec matched any assigned connection — fail-open, return all Composio specs
            return specs

        return tuple(other_specs + expanded)

    def _rebind_spec(
        self, base: ToolSpec, connected_account_id: str, suffix: str
    ) -> ToolSpec:
        """Clona un ToolSpec Composio con connected_account_id inyectado.

        FIX READ (B1): para specs READ reconstruye el handler desde cero vía
        make_broker_read_handler con el connected_account_id real. El handler
        original tenía connected_account_id=None baked-in (construido antes del
        binding por agente); el wrapper anterior delegaba al original sin usarlo,
        dejando la cuenta como None en la propuesta al broker.

        El slug se deriva de base.name (= slug.lower() en composio_tool_specs).
        broker y consent_context vienen de self — siempre server-side, nunca del LLM.
        Fail-safe: si broker o consent_context no están disponibles, mantiene el
        handler original (comportamiento actual: cuenta default).
        """
        from hermes.domain.tool_spec import ToolRisk  # noqa: PLC0415

        new_name = f"{base.name.split('__')[0]}__{suffix}"
        new_tags = tuple(
            t for t in (base.tags or ()) if not t.startswith("ca:")
        ) + (f"ca:{connected_account_id}",)

        new_handler = base.handler
        if base.risk == ToolRisk.READ_ONLY and self._broker is not None and self._consent_context is not None:
            from hermes.runtime.composio_broker_handler import make_broker_read_handler  # noqa: PLC0415
            # Derive slug from base.name: composio_tool_specs builds spec_name as
            # slug.lower() (possibly with a suffix after "__"). Strip any suffix to
            # get the bare slug, then uppercase for the Composio API.
            bare_slug = base.name.split("__")[0].upper()
            new_handler = make_broker_read_handler(
                slug=bare_slug,
                entity_id="",  # adapter falls back to self._entity_id when empty
                broker=self._broker,
                consent_context=self._consent_context,
                connected_account_id=connected_account_id,
            )

        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(
            base,
            name=new_name,
            tags=new_tags,
            handler=new_handler,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _tokenize_context(self, context: DecisionContext):  # type: ignore[return]
        package = {
            "domain_payload": context.domain_payload,
            "subjects": list(context.subjects),
            "constraints": context.constraints,
        }
        return self._tokenizer.tokenize(package)

    def _build_governed_agent(
        self,
        model_config: ModelConfig,
        system_prompt: str,
        loop: asyncio.AbstractEventLoop,
        tenant_id: UUID,
        external_catalog: _ExternalToolCatalog | None = None,
        *,
        consent_context: "ConsentContext | None" = None,
        active_agent_id: str = "",
        pii_mapping: dict[str, str] | None = None,
    ) -> GovernedAIAgent:
        """Construye GovernedAIAgent headless con broker, gate y catálogo externo.

        Memory bridge (Option B): el snapshot de TenantMemoryStore se inyecta
        en ephemeral_system_prompt. Nous lee el snapshot (read-only); todas las
        escrituras siguen pasando por el broker → MemorySurfaceAdapter → store
        (PII-gate + confinamiento por tenant intactos).

        F3: external_catalog inyecta las ToolSpecs externas (Composio + MCP)
        en GovernedAIAgent._external_catalog para que _invoke_tool las encuentre.

        consent_context: per-cycle override que propaga el operator_id real del
        WorkItem (spec 014 inc. 3 / CTRL-13). Si None, cae al consent de clase.
        """
        effective_consent = consent_context if consent_context is not None else self._consent_context
        # FIX D — use cached variants to avoid re-reading memory/disk every message.
        enriched_prompt = _cached_enrich_prompt(system_prompt, tenant_id)
        enabled_ts = _build_enabled_toolsets(
            self._enabled_toolsets, external_catalog
        )
        # Resolve the provider EXACTLY as hermes-agent's own CLI does
        # (oneshot.py): resolve_runtime_provider() reads the registry/.env/OAuth
        # store and returns the real (provider, api_mode, base_url, api_key,
        # credential_pool). Passing model/api_key/base_url straight to AIAgent —
        # as we did before — bypasses this and the agent raises
        # "No LLM provider configured" / can't pick the right base_url+api_mode.
        # FIX D — cache the runtime resolution with a 30s TTL.
        rt, bare_model = _cached_resolve_hermes_runtime(id(self), model_config)
        # FIX C — forward operational knobs from ModelConfig so callers can cap
        # token generation (critical for reasoning models) and tune temperature.
        # None / 0 are intentionally excluded so we never override Nous defaults
        # with a falsy value when the config didn't set them explicitly.
        _extra_knobs: dict[str, Any] = {}
        if model_config.max_tokens is not None:
            _extra_knobs["max_tokens"] = model_config.max_tokens
        if model_config.temperature != 0.0:
            _extra_knobs["temperature"] = model_config.temperature
        # AIAgent (Nous v0.15.1) does NOT accept a `timeout_seconds` constructor
        # kwarg — passing it raises TypeError and kills the turn. Per-request LLM
        # timeouts are ENV-driven (HERMES_API_TIMEOUT / HERMES_STREAM_STALE_TIMEOUT
        # / HERMES_STREAM_READ_TIMEOUT). Forward our configured value to the env
        # knob Nous actually reads, never as an unsupported kwarg.
        # ALWAYS set it to THIS cycle's value (default included): the old
        # `!= 90` skip left a custom value stale in os.environ so the next
        # default-timeout cycle silently inherited it. (Full isolation under a
        # >1 worker pool needs a per-request timeout in Nous — env is process
        # global; tracked as backlog.)
        os.environ["HERMES_API_TIMEOUT"] = str(model_config.timeout_seconds)
        if model_config.max_iterations != 8:
            _extra_knobs["max_iterations"] = model_config.max_iterations
        # Reasoning models served WITHOUT a vLLM reasoning parser (Qwen3.x,
        # DeepSeek-R1, GLM Thinking on a plain OpenAI-compat endpoint) emit CoT
        # as BARE prose in message.content with no <think> tags, which neither
        # Nous strip_think_blocks nor StreamingThinkScrubber can catch. Tell the
        # chat template not to think. chat_template_kwargs only shapes the
        # rendered prompt; the OpenAI tools/tool_calls schema is untouched, so
        # tool-calling is unaffected. Mirrors skill_synthesis.py.
        _extra_body: dict[str, Any] = {}
        _op_extra = model_config.extra.get("extra_body") if model_config.extra else None
        if isinstance(_op_extra, dict):
            _extra_body.update(_op_extra)
        _ctk = _extra_body.setdefault("chat_template_kwargs", {})
        if isinstance(_ctk, dict) and "enable_thinking" not in _ctk:
            _ctk["enable_thinking"] = False
        if _extra_body:
            _extra_knobs["request_overrides"] = {"extra_body": _extra_body}
        agent = GovernedAIAgent(
            model=bare_model,
            api_key=rt.get("api_key"),
            base_url=rt.get("base_url"),
            provider=rt.get("provider"),
            api_mode=rt.get("api_mode"),
            credential_pool=rt.get("credential_pool"),
            quiet_mode=True,
            save_trajectories=False,
            skip_memory=True,
            skip_context_files=True,
            enabled_toolsets=enabled_ts,
            ephemeral_system_prompt=enriched_prompt,
            broker=self._broker,
            consent_context=effective_consent,
            engine_loop=loop,
            tenant_id=tenant_id,
            external_catalog=external_catalog,
            tool_call_emitter=self._chunk_sink_emitter,
            active_agent_id=active_agent_id,
            pii_mapping=pii_mapping,
            **_extra_knobs,
        )
        return agent

    def _map_result_to_output(
        self,
        result: dict[str, Any],
        mapping: dict[str, str],
        model: str,
        agent: GovernedAIAgent,
        tool_steps: "tuple[dict[str, Any], ...] | None" = None,
    ) -> CycleOutput:
        """Mapea el dict de run_conversation → CycleOutput.

        F2: extrae proposals PENDING_APPROVAL desde agent._pending_proposals.
        Solo PENDING entran en tool_call_proposals — EXECUTED/REJECTED ya
        fueron resueltos in-loop y no se re-surfacean.

        tool_steps: descriptores de las tool-calls emitidas en el ciclo (orden de
        ejecución), para persistirlos y reconstruir las tarjetas al recargar.
        """
        raw_narrative = result.get("final_response") or ""
        narrative_safe = str(raw_narrative).strip()

        try:
            narrative = self._tokenizer.rehydrate(narrative_safe, mapping)
        except UnknownPlaceholderError as exc:
            logger.warning(
                "hermes.nous_engine.rehydrate_unknown_placeholder",
                extra={"error": str(exc)},
            )
            narrative = narrative_safe

        api_calls = result.get("api_calls") or 0
        logger.info(
            "hermes.nous_engine.cycle_complete",
            extra={
                "api_calls": api_calls,
                "model": model,
                "pending_proposals": len(agent._pending_proposals),
                "read_external_content": agent._read_external_content,
            },
        )

        pending = tuple(agent._pending_proposals)
        return CycleOutput(
            tool_call_proposals=pending,
            narrative=narrative,
            malformed_intents=(),
            rejected_by_policy=(),
            usage=TokenUsage(
                prompt_tokens=int(getattr(agent, "session_prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(agent, "session_completion_tokens", 0) or 0),
                total_tokens=int(getattr(agent, "session_total_tokens", 0) or 0),
                cost_usd=float(getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0),
                model=model,
                cost_status=str(getattr(agent, "session_cost_status", "unknown") or "unknown"),
                cost_source=str(getattr(agent, "session_cost_source", "none") or "none"),
                provider=str(getattr(agent, "provider", "") or ""),
            ),
            read_external_content=agent._read_external_content,
            tool_steps=tuple(tool_steps or ()),
        )


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _resolve_per_cycle_consent(
    base: "ConsentContext | None",
    context: DecisionContext,
) -> "ConsentContext | None":
    """Resuelve el ConsentContext per-ciclo con el operator_id real del WorkItem.

    spec 014 inc. 3 (CTRL-13 fix): el orchestrator inyecta "task_operator_id"
    (UUID) en context.metadata desde item.payload["enqueued_by"] — fijado
    server-side en ControlPlaneService desde channel.sender_uid (CTRL-P1-3 /
    CWE-862). Si está presente y el base.operator_id es None (HERMES_OPERATOR_ID
    ausente al arrancar), construye un ConsentContext con el operator_id real.

    INVARIANTE de seguridad: el operator_id que llega aquí NUNCA puede ser
    fabricado por el LLM — viene del WorkItem.payload["enqueued_by"], que el
    control-plane estableció server-side antes de que el engine viera la tarea.
    El engine no expone este campo como parámetro de ninguna tool.

    Si base ya tiene operator_id, lo conserva (el daemon arrancó con
    HERMES_OPERATOR_ID configurado — el valor de arranque es igualmente legítimo).
    Si task_operator_id está ausente, devuelve base sin cambios.
    """
    if base is None:
        return None

    task_operator_id: "UUID | None" = context.metadata.get("task_operator_id")
    if task_operator_id is None:
        return base

    if base.operator_id is not None:
        # daemon-level operator_id ya válido — no sobreescribir
        return base

    from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415

    logger.debug(
        "hermes.nous_engine.per_cycle_consent: operator_id resolved from enqueued_by=%s",
        str(task_operator_id)[:16],
    )
    return ConsentContext(
        tenant_id=base.tenant_id,
        operator_id=task_operator_id,
        derived_from_untrusted_content=base.derived_from_untrusted_content,
    )


def _run_conversation_with_cdp(
    agent: GovernedAIAgent,
    user_message: str,
    history: Any,
    cdp_provider: Any,
    stream_callback: "Callable[..., None] | None" = None,
    conversation_id: str = "",
) -> dict[str, Any]:
    """Run agent.run_conversation in the current executor thread.

    When cdp_url is set (Cerebro cycle), wraps the call in cerebro_cdp_scope so
    that browser_tool._get_cdp_override() reads the thread-local URL and routes
    browser_navigate/browser_click to the headed visible browser.

    When cdp_url is None (worker cycle), calls run_conversation directly so
    browser_tool falls back to its headless isolated session (unchanged path).

    Thread-safety: cerebro_cdp_scope uses threading.local() — setting the value
    here is confined to THIS thread.  Concurrent worker threads are unaffected.

    Security kernel (spec 015 — two-mode):
      1. set_session_key_for_thread() sets the stable session key ("cerebro")
         so that register_gateway_notify and resolve_gateway_approval operate
         on the same key as this thread's run_conversation call.
      2. apply_auto_mode_for_cycle() calls enable/disable_session_yolo BEFORE
         run_conversation based on the persisted AUTO flag:
           AUTO ON  → enable_session_yolo → full autonomy (hardline still active).
           AUTO OFF → disable_session_yolo → gateway HITL engaged for dangerous cmds.
      Both calls are fail-soft: any error is logged without crashing the cycle.

    FIX B.1: stream_callback is forwarded to run_conversation so the Nous AIAgent
    emits tokens incrementally.  None = no streaming (fallback unchanged).
    """
    from hermes.runtime.approval_gateway import (  # noqa: PLC0415
        set_session_key_for_thread,
        apply_auto_mode_for_cycle,
        clear_session_key_for_thread,
    )
    from hermes.runtime.cycle_cdp_context import (  # noqa: PLC0415
        cleanup_thread_browser_session,
    )
    from hermes.runtime.conversation_task_registry import (  # noqa: PLC0415
        set_conversation_for_task,
        clear_conversation_for_task,
        set_current_cycle_task,
        clear_current_cycle_task,
        reset_write_tool_failures,
    )

    set_session_key_for_thread()
    apply_auto_mode_for_cycle()

    # Explicit, stable task_id for THIS cycle so we can deterministically reap
    # the confined-browser session it may create. Nous mints its own uuid when
    # task_id is None and leaks the session on the happy path (see
    # cleanup_thread_browser_session); owning the id lets us tear it down.
    cycle_task_id = uuid4().hex

    # Anchor this cycle's task_id to the chat conversation so the security hook
    # can register HITL approvals against the thread the owner is looking at
    # (the hook only receives this random task_id, not the conversation_id).
    set_conversation_for_task(cycle_task_id, conversation_id)
    # Ambient stamp so broker-routed writes that get no task_id (the sequential
    # write wrapper) still resolve THIS conversation → block-and-resume, not the
    # non-resuming retry queue.
    set_current_cycle_task(cycle_task_id)
    reset_write_tool_failures()  # fresh circuit-breaker counters per cycle

    try:
        if cdp_provider is not None:
            with cerebro_cdp_scope(cdp_provider):
                return _run_conversation_streaming_or_fallback(
                    agent, user_message, history, stream_callback,
                    task_id=cycle_task_id,
                )
        return _run_conversation_streaming_or_fallback(
            agent, user_message, history, stream_callback,
            task_id=cycle_task_id,
        )
    finally:
        clear_session_key_for_thread()
        clear_conversation_for_task(cycle_task_id)
        clear_current_cycle_task()
        # Reap this cycle's confined-browser session (agent-browser controller
        # daemon + CDP supervisor) so the NEXT cycle attaches to a clean
        # Chromium. No-op when the cycle never touched the browser; only the
        # jailed/headed CDP cycles can create such a session, so gate on the
        # provider to skip pointless work on pure-text cycles.
        if cdp_provider is not None:
            cleanup_thread_browser_session(cycle_task_id)


def _run_conversation_streaming_or_fallback(
    agent: Any,
    user_message: str,
    history: Any,
    stream_callback: "Callable[..., None] | None",
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run run_conversation with streaming; fall back to non-streaming on failure.

    Some provider/model pairs route to the OpenAI Responses API, which adds
    `include:["reasoning.encrypted_content"]`. Non-reasoning models (e.g. OpenAI
    gpt-4o-mini) reject that with a non-retryable HTTP 400, failing the whole
    cycle. Streaming is opportunistic: if the streaming path raises, retry once on
    the proven NON-streaming path so chat never regresses. Nous providers stream via
    chat.completions and succeed on the first try (no fallback, no double cost).
    The gpt-4o-mini failure is a fast request-level 400 (no tokens emitted yet), so
    the retry starts clean.
    """
    if stream_callback is None:
        return agent.run_conversation(
            user_message, conversation_history=history, task_id=task_id
        )
    # Count emitted deltas: the non-streaming retry re-runs the WHOLE turn, so it
    # is only safe when the stream failed BEFORE any token (the gpt-4o-mini
    # request-level 400). If the stream already emitted deltas, the model may have
    # already executed tool calls (send_message, write_file, …) — re-running would
    # duplicate those side effects, so we re-raise instead of retrying.
    emitted = {"n": 0}

    def _counting_cb(*args: Any, **kwargs: Any) -> Any:
        emitted["n"] += 1
        return stream_callback(*args, **kwargs)

    try:
        return agent.run_conversation(
            user_message,
            conversation_history=history,
            task_id=task_id,
            stream_callback=_counting_cb,
        )
    except Exception as exc:  # noqa: BLE001 — a streaming-path failure must not break chat
        if emitted["n"] > 0:
            logger.warning(
                "hermes.nous_engine.streaming_failed_after_%d_deltas — re-raising "
                "(NOT retrying: a non-stream rerun would duplicate tool side effects)",
                emitted["n"],
            )
            raise
        logger.warning(
            "hermes.nous_engine.streaming_failed_fallback_nonstream error=%s — "
            "retrying without streaming (no deltas emitted; provider/model likely "
            "rejects the Responses-API reasoning include)",
            exc,
        )
        return agent.run_conversation(
            user_message, conversation_history=history, task_id=task_id
        )


def _deterministic_proposal_id(tool_name: str, parameters: dict[str, Any]) -> UUID:
    """proposal_id DETERMINISTA por (tool, params) — uuid5(sha256(tool+args)).

    Re-proponer la MISMA acción produce el MISMO proposal_id: colapsa en la fila
    pendiente existente (register_pending re-arma por id), casa con el token aprobado
    y con el Event del block-and-resume → mata la deriva del digest y el bucle de
    re-aprobación. Misma fórmula que el digest del hook de native dangers.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(
        (tool_name + "\x00" + json.dumps(parameters, sort_keys=True, default=str)).encode(
            "utf-8", "replace"
        )
    ).hexdigest()
    return uuid5(NAMESPACE_URL, digest)


def _build_proposal(
    *,
    function_name: str,
    function_args: dict[str, Any],
    tenant_id: UUID,
    effective_task_id: str,
) -> ToolCallProposal:
    """Construye un ToolCallProposal desde una llamada de tool de Nous.

    entity_id = effective_task_id (el scope de la tarea en curso).
    entity_type = "nous_tool" (tipo genérico para tools de Nous).
    justification = vacío; Nous no provee justificación explícita por llamada.
    """
    parameters = dict(function_args)
    return ToolCallProposal(
        proposal_id=_deterministic_proposal_id(function_name, parameters),
        tool_name=function_name,
        tenant_id=tenant_id,
        entity_id=effective_task_id or "nous_task",
        entity_type="nous_tool",
        parameters=parameters,
        justification=f"nous tool call: {function_name}",
    )


def _dispatch_via_bridge(
    *,
    proposal: ToolCallProposal,
    broker: CapabilityBrokerPort,
    consent_context: ConsentContext,
    engine_loop: asyncio.AbstractEventLoop,
    conversation_id: str = "",
    hitl_approval_token: str | None = None,
) -> Any:
    """Puente async → sync thread-safe para llamar broker.dispatch desde el executor.

    run_conversation corre en un hilo de executor (no en el event loop principal).
    asyncio.run_coroutine_threadsafe envía la coroutine al loop principal y espera
    el resultado con .result(timeout). El broker devuelve PENDING_APPROVAL sin
    esperar al humano → esto no congela el event loop.

    Si el broker tarda más de _BROKER_DISPATCH_TIMEOUT_S → devuelve REJECTED_BY_POLICY
    (fail-closed). No lanza al caller: el gate siempre devuelve una string.
    """
    from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus  # noqa: PLC0415
    from uuid import uuid4 as _uuid4  # noqa: PLC0415

    try:
        future = asyncio.run_coroutine_threadsafe(
            broker.dispatch(
                proposal,
                consent_context,
                conversation_id=conversation_id,
                hitl_approval_token=hitl_approval_token,
            ),
            engine_loop,
        )
        return future.result(timeout=_BROKER_DISPATCH_TIMEOUT_S)
    except TimeoutError:
        logger.error(
            "hermes.nous_engine.broker_dispatch_timeout: tool=%s proposal=%s",
            proposal.tool_name,
            proposal.proposal_id,
        )
        return ExecutionOutcome(
            proposal_id=proposal.proposal_id,
            status=ExecutionStatus.REJECTED_BY_POLICY,
            error="broker_dispatch_timeout — fail-closed",
        )
    except Exception as exc:
        logger.error(
            "hermes.nous_engine.broker_dispatch_error: tool=%s error=%s",
            proposal.tool_name,
            str(exc),
        )
        return ExecutionOutcome(
            proposal_id=proposal.proposal_id,
            status=ExecutionStatus.REJECTED_BY_POLICY,
            error=f"broker_dispatch_error: {type(exc).__name__}",
        )


_EXTERNAL_CONTENT_TOOLS: frozenset[str] = frozenset({
    "web_search",
    "web_extract",
    "browser_snapshot",
    "browser_back",
    "browser_get_images",
    "browser_console",
    "browser_vision",
    "session_search",
})


def _is_external_content_tool(tool_name: str) -> bool:
    """True si la tool READ ingiere contenido externo no confiable (CTRL-5).

    Reusa la semántica de CapturingToolHost._is_untrusted_read:
      - web/browser snapshots → siempre externo.
      - read_file → podría ser externo dependiendo del path; el taint lo
        maneja el orchestrator vía ConsentContext.derived_from_untrusted_content
        para los paths fuera del allowlist, que no aplica en el gate de Nous
        (Nous no tiene paths Hermes). Conservador: read_file de Nous = untrusted.
    """
    if tool_name in _EXTERNAL_CONTENT_TOOLS:
        return True
    # read_file de Nous = untrusted (Nous no tiene el allowlist de Hermes).
    return tool_name == "read_file"


# ---------------------------------------------------------------------------
# Memory bridge helpers
# ---------------------------------------------------------------------------


_REHYDRATE_TOKENIZER = DefaultPIITokenizer()


def _rehydrate_external_args(
    args: dict[str, Any], mapping: dict[str, str]
) -> dict[str, Any]:
    """Replace PII placeholders in an external tool's args with real values.

    The LLM sees tokenized context, so its emitted args (e.g. a recipient email)
    carry ``[[EMAIL_1]]`` placeholders. Before an external (composio/mcp) call
    leaves for the third-party API, restore real values from the per-cycle map.
    Walks dict/list/str recursively; unknown placeholders are left as-is (never
    raise into the dispatch — the adapter/API will reject a bad value honestly).
    Native tools are NOT rehydrated (their handlers run in-process under the cage).
    """
    if not mapping:
        return args

    def _r(v: Any) -> Any:
        if isinstance(v, str):
            try:
                return _REHYDRATE_TOKENIZER.rehydrate(v, mapping)
            except UnknownPlaceholderError:
                return v
        if isinstance(v, dict):
            return {k: _r(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_r(x) for x in v]
        return v

    return _r(args)


def _build_external_proposal(
    *,
    function_name: str,
    function_args: dict[str, Any],
    tenant_id: UUID,
    effective_task_id: str,
    spec: ToolSpec,
) -> ToolCallProposal:
    """Construye ToolCallProposal para una tool externa (Composio/MCP).

    entity_type es extraído del ToolSpec (e.g. "composio", "mcp").
    parameters envuelve args en el formato esperado por el adapter:
      - Composio: {"slug": name.upper(), "params": args, "entity_id": entity_id}
      - MCP (mcp__slug__tool): {"server_id": ..., "tool_name": ..., "args": args}
    El registry (Composio o MCP) usa tool_name + entity_type para resolver el adapter.
    """
    entity_type = spec.entity_type or "external"
    parameters = _shape_external_parameters(function_name, function_args, spec)
    return ToolCallProposal(
        proposal_id=_deterministic_proposal_id(function_name, parameters),
        tool_name=function_name,
        tenant_id=tenant_id,
        entity_id=effective_task_id or "nous_task",
        entity_type=entity_type,
        parameters=parameters,
        justification=f"nous external tool call: {function_name}",
    )


def _shape_external_parameters(
    function_name: str,
    function_args: dict[str, Any],
    spec: ToolSpec,
) -> dict[str, Any]:
    """Forma los parámetros de un proposal externo según el entity_type del ToolSpec.

    Composio: {"slug": slug, "params": args, "entity_id": ""}
      → ComposioCapabilityRegistry resuelve por tool_name (slug.lower()).
      El entity_id real viene del ConsentContext en el broker.
    MCP (mcp__server__tool): {"server_id": "", "tool_name": tool, "args": args}
      → McpCapabilityRegistry resuelve el server a partir del qualified_name.
    os_surface (capability_tool_specs): args as-is with optional "op" injection
      for DESKTOP_APP tools (LibreOfficeUnoSurfaceAdapter reads payload["op"]).
    """
    entity_type = spec.entity_type or ""
    if entity_type == "composio":
        # FIX WRITE (B1): extract connected_account_id from the ca: tag baked
        # into the spec by _rebind_spec. This is server-side only — the LLM
        # never sets this value; it only sees the aliased tool name.
        # Fail-safe: if no ca: tag present, connected_account_id=None keeps
        # the existing default-account behaviour.
        connected_account_id: str | None = None
        for tag in (spec.tags or ()):
            if tag.startswith("ca:"):
                connected_account_id = tag[3:] or None
                break
        # Strip any per-connection suffix (e.g. "__sales_acct") from the tool
        # name to recover the canonical Composio slug before uppercasing.
        bare_slug = function_name.split("__")[0].upper()
        return {
            "slug": bare_slug,
            "params": dict(function_args),
            "entity_id": "",
            "connected_account_id": connected_account_id,
        }
    if entity_type == "mcp" or function_name.startswith("mcp__"):
        parts = function_name.split("__")
        bare_tool = parts[2] if len(parts) >= 3 else function_name
        return {
            "server_id": "",
            "qualified_name": function_name,
            "tool_name": bare_tool,
            "args": dict(function_args),
        }
    if entity_type == "os_surface":
        # spec 014 inc. 3: inject "op" for DESKTOP_APP tools so that
        # LibreOfficeUnoSurfaceAdapter.replay() can dispatch correctly.
        # The op map lives in capability_tool_specs; we import lazily to
        # avoid a module-level circular dependency with nous_engine.
        from hermes.runtime.capability_tool_specs import (  # noqa: PLC0415
            _DESKTOP_APP_OP_MAP,
        )
        params = dict(function_args)
        op = _DESKTOP_APP_OP_MAP.get(function_name)
        if op is not None:
            params["op"] = op
        return params
    return dict(function_args)


def _wire_sequential_gate(agent: "GovernedAIAgent") -> None:
    """Re-register every WRITE-classified native Nous tool in tools.registry
    with a broker-dispatching wrapper.

    SECURITY FIX: the sequential dispatch path (dominant for single tool
    calls — len==1 → _should_parallelize_tool_batch returns False) calls
    _ra().handle_function_call → registry.dispatch directly, bypassing
    GovernedAIAgent._invoke_tool. This function closes that bypass by
    replacing the registry handler with a wrapper that routes WRITE tools
    through the broker, identical to what _invoke_tool does.

    The concurrent path (≥2 calls) calls agent._invoke_tool which handles
    WRITEs before reaching registry.dispatch — so registry wrappers are
    never reached from the concurrent path for WRITE tools. No double-gate.

    Only WRITE-classified tools are re-registered. READ tools are safe on
    both paths (the sequential path's native READ handler is acceptable).

    Idempotent: override=True replaces any previous wrapper on re-entrant
    calls (e.g. if two GovernedAIAgents are built in the same process).
    The last-built agent's wrappers are active — they all call broker.dispatch
    via the agent closure, so they route to the current agent's broker.

    MULTI-TENANT ISOLATION RISK:
      tools.registry is PROCESS-GLOBAL (module-level singleton in hermes-agent).
      With override=True, the last-built GovernedAIAgent's wrappers win.
      In a concurrent multi-tenant single-process scenario where two agents
      are built near-simultaneously, Agent B's wrappers would overwrite Agent A's,
      causing Agent A's tool calls to dispatch through Agent B's broker (wrong
      tenant). CURRENT ARCHITECTURE: run_cycle creates one GovernedAIAgent per
      cycle, run_in_executor serializes cycles — no concurrent agents per process.
      SAFE AS-IS. If concurrent per-process multi-agent is needed in the future,
      tools.registry must be instance-scoped or calls must be serialized.

    Fail-soft: if tools.registry is unavailable (hermes-agent not installed)
    the function logs and returns — the bypass is only exploitable when
    hermes-agent IS installed and the sequential path IS active.
    """
    try:
        from tools.registry import registry as nous_registry  # noqa: PLC0415
    except ImportError:
        logger.debug(
            "hermes.nous_engine._wire_sequential_gate: "
            "tools.registry unavailable — hermes-agent not installed, skip"
        )
        return

    from hermes.runtime.nous_tool_risk_map import (  # noqa: PLC0415
        NousRisk,
        classify_nous_tool,
        NOUS_TOOL_CATALOG,
    )

    # MODELO HERMES-NATIVE (W20): las tools WRITE nativas YA NO se envuelven con
    # un wrapper broker-dispatching. El broker las rechazaba ('no registrado',
    # sin surface-adapter) → terminal/browser_navigate/write_file/patch salían
    # BLOCKED. El gate del sequential path es ahora el hook `pre_tool_call`
    # (model_tools.handle_function_call lo dispara ANTES de registry.dispatch:
    # kill-switch + suelo hardline + anti-autojailbreak + guards approval.py), y
    # el hook `post_tool_call` audita. Dejar el handler NATIVO original → la tool
    # ejecuta nativo tras pasar el hook. (El broker sigue gateando/ejecutando las
    # custom os_surface por su propio registro.)
    write_tools: list[str] = []  # intencionalmente vacío: el hook es el gate

    for tool_name in write_tools:
        entry = nous_registry.get_entry(tool_name)
        if entry is None:
            continue
        _make_sequential_write_wrapper(agent, tool_name, nous_registry, entry)

    # SECURITY (red-team 2026-06-19 — el agujero más grave, probado e2e): "terminal"
    # (y demás caged-exec tools) NO deben ejecutar su handler NATIVO in-daemon — corre
    # como `hermes` (dueño de master.key 0600) en el host netns (egress abierto). El
    # hook pre_tool_call solo aplica denylist/kill-switch, NO confina. El agente real
    # leyó master.key + exfiltró por curl por ESTE path (sequential → registry.dispatch
    # → handler nativo). Re-registramos el handler para que la EJECUCIÓN se delegue al
    # exec-launcher (hermes-sandbox + netns + InaccessiblePaths). El hook sigue
    # disparándose ANTES (handle_function_call → pre_tool_call). El path concurrent lo
    # cubre _run_native_tool. FAIL-CLOSED dentro de _run_caged_tool.
    for tool_name in _CAGED_NATIVE_TOOLS:
        entry = nous_registry.get_entry(tool_name)
        if entry is None:
            # Tool not registered for this agent → nothing to wrap (it can't be
            # called). Only the concurrent-path _dispatch_nous_native check matters.
            logger.debug(
                "hermes.nous_engine.cage_wrap: tool %s not in Nous registry "
                "(not exposed to this agent) — skipping wrap", tool_name,
            )
            continue
        _make_sequential_caged_wrapper(agent, tool_name, nous_registry, entry)


def _make_sequential_caged_wrapper(
    agent: "GovernedAIAgent",
    tool_name: str,
    nous_registry: Any,
    entry: Any,
) -> None:
    """Re-register a native exec tool so its EXECUTION goes through the cage.

    Replaces the native in-daemon handler with one that delegates to the
    exec-launcher (hermes-sandbox + netns + InaccessiblePaths) via
    GovernedAIAgent._run_caged_tool. (red-team 2026-06-19.)
    """
    def _caged_wrapper(args: dict[str, Any], **kwargs: Any) -> str:
        parsed_args = dict(args) if isinstance(args, dict) else {}
        return agent._run_caged_tool(tool_name, parsed_args)

    try:
        nous_registry.register(
            name=tool_name,
            toolset=entry.toolset,
            schema=entry.schema,
            handler=_caged_wrapper,
            check_fn=entry.check_fn,
            requires_env=entry.requires_env,
            is_async=False,
            description=entry.description,
            emoji=getattr(entry, "emoji", ""),
            override=True,
        )
        logger.info(
            "hermes.nous_engine.cage_wrap: tool '%s' now routes through the "
            "exec-launcher cage (no longer runs in-daemon)", tool_name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.nous_engine.cage_wrap.FAILED tool=%s error=%s — "
            "IN-DAEMON BYPASS REMAINS OPEN", tool_name, exc,
        )


def _make_sequential_write_wrapper(
    agent: "GovernedAIAgent",
    tool_name: str,
    nous_registry: Any,
    entry: Any,
) -> None:
    """Register a broker-dispatching wrapper for one WRITE tool in the registry.

    The wrapper closes over the GovernedAIAgent instance so it can dispatch
    through the same broker bridge as _invoke_tool._dispatch_write_proposal.
    Uses a dedicated effective_task_id placeholder since the sequential path
    does not pass task_id through registry.dispatch (it is set at the
    handle_function_call level via the task_id parameter, not forwarded to
    the handler itself — the handler signature is (args) only).
    """
    def _broker_write_wrapper(args: dict[str, Any], **kwargs: Any) -> str:
        # Reuse the same WRITE path as _invoke_tool — identical semantics,
        # same broker, same consent_context. effective_task_id comes from
        # kwargs if provided (model_tools passes task_id as kwarg in some
        # callers), defaulting to a sentinel so the proposal is still valid.
        effective_task_id = str(kwargs.get("task_id") or "")
        parsed_args = dict(args) if isinstance(args, dict) else {}
        return agent._dispatch_write_proposal(
            function_name=tool_name,
            function_args=parsed_args,
            effective_task_id=effective_task_id,
            tool_call_id=str(kwargs.get("tool_call_id") or ""),
        )

    try:
        nous_registry.register(
            name=tool_name,
            toolset=entry.toolset,
            schema=entry.schema,
            handler=_broker_write_wrapper,
            check_fn=entry.check_fn,
            requires_env=entry.requires_env,
            is_async=False,
            description=entry.description,
            emoji=getattr(entry, "emoji", ""),
            override=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.nous_engine._wire_sequential_gate.register_failed: "
            "tool=%s error=%s — sequential bypass NOT closed for this tool",
            tool_name, exc,
        )


def _wire_inline_branch_gates(agent: "GovernedAIAgent") -> None:
    """Gate memory and clarify by monkeypatching the handler functions Nous calls inline.

    SECURITY ISSUE 2:
      tool_executor.execute_tool_calls_sequential contains hardcoded if/elif
      branches for memory (line ~904) and clarify (line ~931) that call the
      underlying handler functions DIRECTLY, before handle_function_call or
      registry.dispatch. This completely bypasses our registry wrappers.

      Both tools are classified as WRITE in nous_tool_risk_map.py (memory=WRITE,
      clarify=WRITE), so they SHOULD route through the broker.

      The inline branch imports the handler lazily:
        from tools.memory_tool import memory_tool as _memory_tool
        from tools.clarify_tool import clarify_tool as _clarify_tool

      Since the import happens at call time (not module level), patching the
      module attribute intercepts it: Python resolves the attribute on the cached
      module object each time the import statement executes.

    GATING STRATEGY:
      memory: classified WRITE → wrap with _dispatch_write_proposal (full broker
              gate: consent, audit, kill-switch, HITL). The broker routes to
              MemorySurfaceAdapter. Clarify: classified WRITE → same.

      Both wrappers run synchronously (the inline branch is synchronous).
      The broker bridge (_dispatch_via_bridge) bridges to the async event loop.

    CLARIFY SEMANTICS NOTE:
      clarify is interactive — it blocks on user input. Routing it through the
      broker PENDING_APPROVAL path means the user would need to approve the
      clarification question before the agent can ask it. This is CORRECT
      behaviour for an agentic OS: every outbound user-facing action must be
      gated. The HeadlessAgent use-case (quiet_mode=True, no clarify callback)
      already returns an error from clarify_tool when callback=None; the broker
      gate adds an additional outer layer.

    todo + delegate_task:
      Both are classified WRITE and intercepted by inline branches. However:
      - todo: creates/updates a local TODO list visible only in-session. The
        consequence is low (no external effects). The registry wrapper IS
        present for todo (it is in NOUS_TOOL_CATALOG) but the inline branch
        fires before registry.dispatch, so the wrapper is dead. Residual
        documented below.
      - delegate_task: spawns subagents via agent._dispatch_delegate_task, not
        an external I/O surface. The inline branch calls agent._dispatch_delegate_task
        directly. The registry wrapper is also dead for delegate_task. Since
        delegate_task in the Hermes context creates sub-tasks handled by the
        AgentLoopOrchestrator (not arbitrary process spawning), the risk is
        lower. Residual documented below.

    KNOWN RESIDUALS (not gated by this function):
      todo: inline branch calls tools.todo_tool.todo_tool directly. Consequence:
        todo writes are not broker-gated. Risk: LOW (local in-session state only,
        no persistent external effects). The registry wrapper exists but is
        bypassed. Accepted residual — gating todo through broker HITL would
        break interactive task management with no security benefit.
      delegate_task: inline branch calls agent._dispatch_delegate_task directly.
        Consequence: subagent spawning not broker-gated. Risk: MEDIUM in theory
        (spawns another agent), but in the Hermes OS context the subagent runs
        under the same broker constraints and its own tool calls are gated.
        Accepted residual — gating delegate_task through broker would create a
        deadlock (agent waits for approval to spawn, approved subagent also needs
        approval for its tools). The HITL gate on each subagent's individual tool
        calls is the correct gate boundary.
      session_search: READ tool with inline handling in some code paths. No gate
        needed — READ tools are not broker-gated (no external effect).

    Fail-soft: if tools.memory_tool or tools.clarify_tool cannot be imported
      (hermes-agent not installed), logs DEBUG and returns. The inline branch
      will also fail to import and no call will execute.
    """
    _patch_memory_tool(agent)
    _patch_clarify_tool(agent)
    _patch_skill_manage_tool(agent)


def _patch_skill_manage_tool(agent: "GovernedAIAgent") -> None:
    """Auto-wrap plain skill content into a valid SKILL.md before the native handler.

    ROOT of the skill_manage failures: the native tool requires ``content`` to be a
    full SKILL.md (``---`` frontmatter with name/description/version), but the LLM
    almost always sends just the body ("saluda al usuario") → "SKILL.md must start
    with YAML frontmatter" → it retries blindly (the retry-spam). We reuse the
    existing _ensure_frontmatter_fields helper to wrap plain content, so a skill is
    created on the FIRST try. Fail-soft: import errors leave the native tool as-is.
    """
    try:
        import tools.skill_manager_tool as _sm_mod  # noqa: PLC0415
        from hermes.shell_server.skills.skill_synthesis import (  # noqa: PLC0415
            _ensure_frontmatter_fields,
        )
    except ImportError:
        logger.debug(
            "hermes.nous_engine._patch_skill_manage_tool: unavailable — skip"
        )
        return

    _original_skill_manage = _sm_mod.skill_manage

    def _wrapped_skill_manage(action=None, name=None, content=None, **kwargs):
        # Only content-bearing actions (create/edit) carry a SKILL.md body. If the
        # model sent a bare body (no frontmatter), wrap it so the native parser accepts it.
        if (
            isinstance(content, str)
            and content.strip()
            and not content.lstrip().startswith("---")
        ):
            try:
                content = _ensure_frontmatter_fields(content, name or "", "")
                logger.info(
                    "hermes.nous_engine.skill_manage.frontmatter_wrapped name=%s", name
                )
            except Exception:  # noqa: BLE001 — never block the call on wrapping
                logger.debug("skill_manage frontmatter wrap failed", exc_info=True)
        return _original_skill_manage(action=action, name=name, content=content, **kwargs)

    _sm_mod.skill_manage = _wrapped_skill_manage
    logger.debug("hermes.nous_engine._patch_skill_manage_tool: content auto-frontmatter on")


def _patch_memory_tool(agent: "GovernedAIAgent") -> None:
    """Monkeypatch tools.memory_tool.memory_tool with a broker-routing wrapper.

    The wrapper intercepts the write actions (add/replace/remove) and routes
    them through agent._dispatch_write_proposal → broker. Read actions (read)
    are passed through to the original function (no external effect).
    """
    try:
        import tools.memory_tool as _mem_mod  # noqa: PLC0415
    except ImportError:
        logger.debug(
            "hermes.nous_engine._patch_memory_tool: "
            "tools.memory_tool unavailable — hermes-agent not installed, skip"
        )
        return

    _original_memory_tool = _mem_mod.memory_tool

    def _gated_memory_tool(
        action: str | None = None,
        target: str = "memory",
        content: str | None = None,
        old_text: str | None = None,
        store: Any = None,
        **kwargs: Any,
    ) -> str:
        # READ actions have no persistent external effect — pass through.
        if action == "read":
            return _original_memory_tool(
                action=action,
                target=target,
                content=content,
                old_text=old_text,
                store=store,
                **kwargs,
            )
        # WRITE actions (add, replace, remove) — route through broker.
        function_args: dict[str, Any] = {
            "action": action,
            "target": target,
            # Provenance: stamp the writing agent. Reserved key — never from LLM input.
            "_provenance_agent_id": agent._active_agent_id,
        }
        if content is not None:
            function_args["content"] = content
        if old_text is not None:
            function_args["old_text"] = old_text
        return agent._dispatch_write_proposal(
            function_name="memory",
            function_args=function_args,
            effective_task_id="",
            tool_call_id=None,
        )

    _mem_mod.memory_tool = _gated_memory_tool
    logger.debug("hermes.nous_engine._patch_memory_tool: memory write gated via broker")


def _patch_clarify_tool(agent: "GovernedAIAgent") -> None:
    """Monkeypatch tools.clarify_tool.clarify_tool with a broker-routing wrapper.

    clarify is an outbound user-facing action (presents a question, blocks on
    user input). Routing it through the broker ensures consent + audit coverage.
    When the broker returns PENDING_APPROVAL, the agent sees BLOCKED and must
    wait for the human to respond — which is the correct behaviour for HITL.

    In headless mode (quiet_mode=True, callback=None), clarify_tool already
    returns an error without user interaction. The broker gate adds an outer
    layer that also requires consent before the question is even presented.
    """
    try:
        import tools.clarify_tool as _clarify_mod  # noqa: PLC0415
    except ImportError:
        logger.debug(
            "hermes.nous_engine._patch_clarify_tool: "
            "tools.clarify_tool unavailable — hermes-agent not installed, skip"
        )
        return

    def _gated_clarify_tool(
        question: str = "",
        choices: list[Any] | None = None,
        callback: Any = None,
        **kwargs: Any,
    ) -> str:
        function_args: dict[str, Any] = {"question": question}
        if choices is not None:
            function_args["choices"] = choices
        return agent._dispatch_write_proposal(
            function_name="clarify",
            function_args=function_args,
            effective_task_id="",
            tool_call_id=None,
        )

    _clarify_mod.clarify_tool = _gated_clarify_tool
    logger.debug("hermes.nous_engine._patch_clarify_tool: clarify gated via broker")


def register_mcp_tools_in_nous_registry(server, broker, consent_context, engine_loop) -> None:
    """Camino A: registra las tools de UN servidor MCP en el tools.registry
    PROCESS-GLOBAL de Nous al CONECTAR. Cualquier agente Nous las descubre vía
    enabled_toolsets=None — independiente del path per-ciclo (run_cycle._resolve).

    Handler sync que puentea al engine_loop (run_coroutine_threadsafe), igual que
    _execute_external_read. El broker resuelve el riesgo de la tool (READ auto /
    WRITE HITL) — no hay bypass de gate.
    """
    try:
        from tools.registry import registry as nous_registry  # noqa: PLC0415
    except ImportError:
        logger.debug("hermes.nous_engine.mcp_global_register: tools.registry no disponible")
        return
    from hermes.runtime.mcp_broker_handler import make_mcp_broker_read_handler  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    slug = str(server.slug)
    toolset = "mcp-%s" % slug
    n = 0
    for tool in server.tools:
        qualified = tool.qualified_name
        bare = tool.name
        schema = {"name": qualified, "description": tool.description,
                  "parameters": {"type": "object", "properties": {}}}
        read_handler = make_mcp_broker_read_handler(
            qualified_name=qualified, bare_tool_name=bare,
            broker=broker, consent_context=consent_context,
        )
        def _make_sync(_rh, _qn):
            def _sync(args, **kwargs):  # noqa: ARG001
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        _rh(args if isinstance(args, dict) else {}), engine_loop
                    )
                    res = fut.result(timeout=120.0)
                    return res if isinstance(res, str) else _json.dumps(res, ensure_ascii=False)
                except Exception as _e:  # noqa: BLE001
                    return _json.dumps({"error": "mcp dispatch failed: %r" % _e})
            return _sync
        try:
            nous_registry.register(
                name=qualified, toolset=toolset, schema=schema,
                handler=_make_sync(read_handler, qualified), is_async=False,
                description=tool.description, override=True,
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.nous_engine.mcp_global_register_failed: tool=%s error=%s",
                qualified, exc,
            )
    logger.info(
        "hermes.nous_engine.mcp_tools_registered_global: slug=%s count=%d", slug, n
    )
    # Invalida el memo de model_tools para que el schema del LLM recompute e
    # incluya las tools MCP recién registradas en el próximo ciclo.
    try:
        import model_tools as _mt  # noqa: PLC0415
        if hasattr(_mt, "_tool_defs_cache"):
            _mt._tool_defs_cache.clear()
    except Exception as _ce:  # noqa: BLE001
        logger.warning("hermes.nous_engine.mcp_cache_clear_failed: %s", _ce)


def _register_external_specs_in_nous(
    specs: tuple[ToolSpec, ...],
    agent: "GovernedAIAgent",
) -> None:
    """Registra ToolSpecs externos en el Nous tools.registry con wrappers broker-routing.

    SEQUENTIAL PATH FIX (Issue 1):
      External tools (Composio/MCP) are NOT in NOUS_TOOL_CATALOG, so
      _wire_sequential_gate did not wrap them. On the sequential path
      (len==1 → dominant case), Nous calls:
        execute_tool_calls_sequential → _ra().handle_function_call
          → registry.dispatch → handler

      The handler registered HERE is now invoked, not _invoke_tool.
      Previous code registered a _blocked_handler stub → every external tool
      call on the sequential path returned BLOCKED (dead, not just fail-closed).

      Fix: register a broker-dispatching wrapper per spec that mirrors
      _dispatch_external exactly:
        READ  → calls spec.handler via asyncio.run_coroutine_threadsafe
                 (same as _execute_external_read — broker-dispatching closure)
        WRITE → calls agent._dispatch_external_write → broker.dispatch

    CONCURRENT PATH (no change):
      _invoke_tool handles external tools before registry.dispatch is reached.
      The concurrent path never calls the registry handler for externals.
      No double-gate: concurrent → _invoke_tool → _dispatch_external (broker).
      Sequential → registry.dispatch → this wrapper → _dispatch_external (broker).

    MULTI-TENANT GUARD:
      tools.registry is process-global; override=True means the last-built
      GovernedAIAgent's wrappers are active. In a multi-tenant single-process
      deployment each tenant's agent would overwrite the previous one's wrappers.
      This is the same last-writer-wins behaviour as _wire_sequential_gate for
      native tools. In the current architecture (one agent per run_cycle, never
      concurrent per process) this is safe. If concurrent per-process multi-agent
      is needed, tools.registry must be instance-scoped or calls must be
      serialized. See also _wire_sequential_gate comment.
    """
    if not specs:
        return

    try:
        from tools.registry import registry as nous_registry  # noqa: PLC0415
    except ImportError:
        logger.debug(
            "hermes.nous_engine.nous_registry_unavailable: "
            "hermes-agent no instalado, skip register_external_specs"
        )
        return

    for spec in specs:
        _make_external_sequential_wrapper(agent, spec, nous_registry)


def _sync_agent_tools_with_external(
    agent: "GovernedAIAgent",
    specs: tuple[ToolSpec, ...],
) -> None:
    """Make THIS cycle's agent expose the just-registered external ToolSpecs.

    ``agent_init`` builds ``agent.tools`` (the schema array sent to the LLM) and
    ``agent.valid_tool_names`` (the call-time allow-list) ONCE, from the Nous
    registry, DURING agent construction — which happens before
    ``_register_external_specs_in_nous`` runs. The registry is process-global, so a
    warm daemon sees externals registered by an earlier cycle, but the FIRST cycle
    after boot builds against an empty external set: the model never receives the
    Composio/MCP tools and the agent would reject a call to them as an unknown name.

    Append any spec not already present (by name) as a plain function schema and
    extend the allow-list. Idempotent: warm cycles where ``agent.tools`` already
    carries these names add nothing. Appending directly (rather than re-running the
    full tool assembly) preserves memory/LCM tools injected after init, and keeps the
    intent-retrieved top-K VISIBLE (not re-deferred behind tool_search) — which is the
    whole point of the semantic retrieval that produced this short list.
    """
    if not specs:
        return
    # GovernedAIAgent wraps the real AIAgent by composition — tools/valid_tool_names
    # live on the inner agent (agent_init sets them there during construction).
    inner = getattr(agent, "_inner", agent)
    try:
        from tools.registry import registry as _nous_registry  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        _nous_registry = None

    def _is_accumulated_external(name: str) -> bool:
        """True if `name` is a Composio/MCP tool from the process-global registry.

        The registry is shared across cycles and NEVER pruned, so it accumulates the
        union of every intent's retrieved tools. Such names must be re-presented ONLY
        when they belong to THIS cycle's retrieved set — otherwise the intent-based
        narrowing erodes over a session (the model drifts back to seeing hundreds).
        Native/core/capability tools are NOT accumulated externals and are kept as-is.
        """
        if not name or _nous_registry is None:
            return False
        try:
            entry = _nous_registry.get_entry(name)
        except Exception:  # noqa: BLE001
            return False
        toolset = getattr(entry, "toolset", "") if entry else ""
        return toolset == "composio" or toolset.startswith("mcp-")

    try:
        current = getattr(inner, "tools", None)
        source = current if isinstance(current, list) else []
        current_names = {s.name for s in specs}
        # 1) Drop stale accumulated externals not selected this cycle (keeps narrowing).
        kept: list = []
        pruned = 0
        for t in source:
            nm = (t.get("function") or {}).get("name") if isinstance(t, dict) else None
            if nm and nm not in current_names and _is_accumulated_external(nm):
                pruned += 1
                continue
            kept.append(t)
        # 2) Append this cycle's retrieved externals that agent_init didn't capture
        #    (cold start: registry was empty when the agent was built).
        present = {
            (t.get("function") or {}).get("name")
            for t in kept
            if isinstance(t, dict)
        }
        added = 0
        for spec in specs:
            if spec.name in present:
                continue
            kept.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters_schema
                        or {"type": "object", "properties": {}},
                    },
                }
            )
            present.add(spec.name)
            added += 1
        inner.tools = kept
        # Keep the call-time allow-list exactly in sync with what the model can see.
        inner.valid_tool_names = {
            (t.get("function") or {}).get("name")
            for t in kept
            if isinstance(t, dict) and (t.get("function") or {}).get("name")
        }
        if added or pruned:
            logger.info(
                "hermes.nous_engine.synced_external_tools added=%d pruned=%d total=%d "
                "(cold-start visibility + per-turn narrowing)",
                added, pruned, len(kept),
            )
    except Exception as exc:  # noqa: BLE001 — never break the cycle over tool sync
        logger.warning("hermes.nous_engine.sync_external_tools_failed: %s", exc)


def _make_external_sequential_wrapper(
    agent: "GovernedAIAgent",
    spec: ToolSpec,
    nous_registry: Any,
) -> None:
    """Register a broker-dispatching sequential wrapper for one external ToolSpec.

    READ:  calls spec.handler (already a broker-dispatching closure built by
           make_broker_read_handler / make_mcp_broker_read_handler) via the
           engine_loop bridge. Identical to _execute_external_read.
    WRITE: calls agent._dispatch_external_write → broker.dispatch EXACTLY ONCE.
           Provides effective_task_id from kwargs if available.

    The wrapper is synchronous (is_async=False) so registry.dispatch can call
    it directly without an event loop.  Async I/O is bridged via
    asyncio.run_coroutine_threadsafe into the engine_loop (same pattern as
    _execute_external_read).
    """
    from hermes.domain.tool_spec import ToolRisk  # noqa: PLC0415

    toolset = _toolset_for_spec(spec)
    schema = {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters_schema,
    }
    captured_spec = spec  # close over the loop variable explicitly

    if captured_spec.risk == ToolRisk.READ_ONLY:
        def _sequential_wrapper(args: dict[str, Any], **kwargs: Any) -> str:
            parsed_args = dict(args) if isinstance(args, dict) else {}
            return agent._execute_external_read(captured_spec.name, parsed_args, captured_spec)
    else:
        def _sequential_wrapper(args: dict[str, Any], **kwargs: Any) -> str:  # type: ignore[misc]
            effective_task_id = str(kwargs.get("task_id") or "")
            tool_call_id = str(kwargs.get("tool_call_id") or "")
            parsed_args = dict(args) if isinstance(args, dict) else {}
            return agent._dispatch_external_write(
                captured_spec.name,
                parsed_args,
                effective_task_id,
                tool_call_id or None,
                captured_spec,
            )

    try:
        nous_registry.register(
            name=spec.name,
            toolset=toolset,
            schema=schema,
            handler=_sequential_wrapper,
            is_async=False,
            description=spec.description,
            override=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.nous_engine.register_external_spec_failed: tool=%s error=%s",
            spec.name, exc,
        )


def _toolset_for_spec(spec: ToolSpec) -> str:
    """Deriva el nombre de toolset Nous para un ToolSpec externo.

    spec 014 inc. 3: os_surface tools get "os_surface" toolset so they
    appear in the LLM schema under a distinct, identifiable group.
    """
    if spec.entity_type == "composio":
        return "composio"
    if spec.name.startswith("mcp__"):
        parts = spec.name.split("__")
        slug = parts[1] if len(parts) >= 2 else "mcp"
        return f"mcp-{slug}"
    if spec.entity_type == "os_surface":
        return "os_surface"
    return spec.entity_type or "external"


def _build_enabled_toolsets(
    base_toolsets: list[str] | None,
    external_catalog: _ExternalToolCatalog | None,
) -> list[str] | None:
    """Construye la lista de enabled_toolsets para GovernedAIAgent.

    Si hay tools externas, añade sus toolsets para que aparezcan en el
    schema de function-calling del LLM. Si base_toolsets es None (= todos),
    retorna None para preservar el comportamiento por defecto de Nous.
    """
    if external_catalog is None or len(external_catalog) == 0:
        return base_toolsets

    extra_toolsets = {_toolset_for_spec(s) for s in external_catalog.all_specs()}

    if base_toolsets is None:
        return None  # None significa "todos" — los externos ya están registrados.

    combined = list(base_toolsets)
    for ts in sorted(extra_toolsets):
        if ts not in combined:
            combined.append(ts)
    return combined


def _enrich_prompt_with_memory_snapshot(base_prompt: str, tenant_id: UUID) -> str:
    """Inject tenant memory snapshot into the system prompt (Option B bridge).

    Import is lazy so the bridge does not break the import graph when
    hermes-agent is absent. If the bridge itself fails (filesystem error,
    missing env) we log a warning and return base_prompt unchanged — fail-soft
    because memory recall is advisory, not a hard runtime requirement.
    """
    try:
        from hermes.memory.infrastructure.nous_memory_bridge import (  # noqa: PLC0415
            build_nous_memory_bridge,
        )
        bridge = build_nous_memory_bridge(tenant_id=tenant_id)
        return bridge.enrich_system_prompt(base_prompt)
    except Exception as exc:
        logger.warning(
            "hermes.nous_engine.memory_bridge_skipped tenant=%s: %s",
            str(tenant_id)[:8],
            exc,
        )
        return base_prompt


# ---------------------------------------------------------------------------
# TODO DEVOPS — bake de hermes-agent en el Containerfile
# ---------------------------------------------------------------------------
# El paquete hermes-agent==0.15.1 (NousResearch) NO está en PyPI estándar.
# Debe instalarse en la imagen del SO antes de que HERMES_ENGINE=nous funcione.
#
# En Containerfile.personal-desktop (o equivalente del bake):
#
#     RUN pip install --target=/usr/lib/python3.13/site-packages \
#         hermes-agent==0.15.1
#
# Ver feedback_bootc_pip_usr_lib: /usr/local se borra en el primer boot;
# usar --target=/usr/lib/python3.13/site-packages siempre en builds bootc/ostree.
