"""PostCycleMemoryExtractor — extract durable facts from a completed chat cycle.

Called by AgentLoopOrchestrator after a chat cycle completes successfully
(both the narrative path and the tool-dispatch path).  Never called on failure
or on trivial/empty turns.

Design:
  - One short LLM call (no tools, no broker, no consent cycle) to extract
    1-3 key facts from the conversation turn.
  - Writes through TenantMemoryStore (PII gate, path confinement).
  - Target: "memory" (same target the MemorySurfaceAdapter uses for agent notes).
  - Completely fail-soft: any error is logged and swallowed — the chat cycle
    has already completed; this is a best-effort enrichment.
  - Skips trivial turns (< _MIN_CONTENT_LEN chars combined) to avoid noise.

Security:
  - Model config resolved from the daemon's DB/env (same as NousReasoningEngine).
  - No untrusted data enters the prompt in raw form — conversation content is
    length-capped and only the assistant narrative / user message are used.
  - No secrets, tokens, or PII are written (TenantMemoryStore._assert_no_pii).

Capa: application (orchestrates infrastructure).  No framework imports.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger("hermes.memory.post_cycle_extractor")

_MIN_CONTENT_LEN = 60   # skip turns too short to yield meaningful facts
_MAX_PROMPT_CHARS = 2000  # cap content sent to the model (safety + cost)
_EXTRACTION_TIMEOUT = 15.0  # seconds — short call, not a reasoning cycle
_MIN_FACT_LEN = 5
_MAX_FACT_LEN = 160

_EXTRACTION_SYSTEM_PROMPT = (
    "You are a memory distillation assistant. "
    "Given a single conversation exchange between a user and an AI assistant, "
    "extract 0 to 3 short, concrete facts worth remembering long-term. "
    "Rules:\n"
    "- Only extract genuinely useful, specific facts (preferences, decisions, "
    "  names, dates, context the assistant should recall later).\n"
    "- If no fact is worth keeping, output exactly: NONE\n"
    "- Output one fact per line, no numbering, no bullets.\n"
    "- Each fact must be under 160 characters.\n"
    "- Never include PII (passwords, credit cards, SSNs, tokens).\n"
    "- Language: match the conversation language.\n"
    "- No markdown, no preamble."
)

_DEFAULT_MEMORY_ROOT = Path(
    os.environ.get("HERMES_MEMORY_ROOT", "/var/lib/hermes/memory")
)


async def maybe_extract_and_store(
    *,
    user_message: str,
    assistant_reply: str,
    tenant_id: UUID,
    engine: Any,  # the orchestrator's injected engine (used to resolve model config)
) -> None:
    """Best-effort extraction of durable facts from one chat turn.

    Writes to the "memory" target of TenantMemoryStore.  Silently skips when:
      - Turn is too short to be interesting.
      - No model is configured.
      - The selected native provider is unavailable.
      - The LLM returns NONE or an empty/malformed response.
      - TenantMemoryStore rejects the content (PII gate).
    """
    combined = (user_message or "").strip() + " " + (assistant_reply or "").strip()
    if len(combined) < _MIN_CONTENT_LEN:
        return

    try:
        model_cfg = _resolve_model_config(engine)
        if model_cfg is None:
            return
        facts = await _call_extractor(
            user_message=user_message,
            assistant_reply=assistant_reply,
            model_cfg=model_cfg,
        )
        if not facts:
            return
        _write_facts(facts, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — never interrupt the caller or expose payloads
        logger.warning("hermes.memory.post_cycle_extractor.failed")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_model_config(engine: Any) -> Any:
    """Extract the active ModelConfig from the engine. Fail-soft → None."""
    try:
        # NousReasoningEngine exposes _resolve_model_config()
        if hasattr(engine, "_resolve_model_config"):
            return engine._resolve_model_config()  # noqa: SLF001
        return None
    except Exception:  # noqa: BLE001
        return None


async def _call_extractor(
    *,
    user_message: str,
    assistant_reply: str,
    model_cfg: Any,
) -> list[str]:
    """Make the short LLM extraction call. Returns a list of fact strings.

    Uses the same explicit native transport as skill synthesis. Responses stays
    Responses; Qwen-only extensions never leak to another selected provider.
    """
    try:
        from hermes.providers.infrastructure.native_text_completion import (  # noqa: PLC0415
            complete_native_text,
        )
    except ImportError:
        return []

    user_content = _cap(user_message, _MAX_PROMPT_CHARS // 2)
    assistant_content = _cap(assistant_reply, _MAX_PROMPT_CHARS // 2)
    user_prompt = f"User: {user_content}\n\nAssistant: {assistant_content}"

    try:
        raw = await complete_native_text(
            model_cfg,
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=256,
            temperature=0.0,
            timeout=_EXTRACTION_TIMEOUT,
        )
        return _parse_facts(raw)
    except Exception:  # noqa: BLE001 — optional enrichment, safe diagnostic only
        logger.warning("hermes.memory.extractor.llm_failed")
        return []


def _parse_facts(raw: str) -> list[str]:
    """Parse newline-delimited facts from the model response."""
    if not raw or raw.strip().upper() == "NONE":
        return []
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    # Drop any line that looks like "NONE" or is suspiciously short / long
    facts = [
        ln for ln in lines
        if ln.upper() != "NONE" and _MIN_FACT_LEN < len(ln) <= _MAX_FACT_LEN
    ]
    return facts[:3]  # cap at 3 regardless of model output


def _write_facts(facts: list[str], *, tenant_id: UUID) -> None:
    """Write extracted facts to the tenant memory store. Fail-soft per-fact."""
    try:
        from hermes.memory.infrastructure.tenant_memory_store import (  # noqa: PLC0415
            TenantMemoryStore,
        )
    except ImportError:
        return

    store = TenantMemoryStore(root=_DEFAULT_MEMORY_ROOT, tenant_id=tenant_id)
    written = 0
    for fact in facts:
        try:
            result = store.add("memory", fact)
            if result.get("success"):
                written += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "hermes.memory.extractor.write_failed fact=%r: %s",
                fact[:40],
                exc,
            )
    if written:
        logger.info(
            "hermes.memory.post_cycle_extractor.wrote tenant=%s facts=%d",
            str(tenant_id)[:8],
            written,
        )


def _cap(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, appending an ellipsis if truncated."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"
