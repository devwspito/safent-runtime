"""Owner-approved tools get ONE attempt per turn.

2026-09-14: the model called skill_manage five times in one turn, each call
malformed; every retry minted a fresh approval card the owner accepted. A
retry after an owner-approved failure must be refused with guidance that also
forbids detours (creating skills, installing connectors).
"""
from __future__ import annotations

import json

from hermes.runtime import nous_engine
from hermes.runtime.conversation_task_registry import (
    bump_write_tool_failure,
    reset_write_tool_failures,
    write_tool_failure_count,
)


def test_one_failure_opens_the_circuit() -> None:
    reset_write_tool_failures()
    assert nous_engine._MAX_WRITE_TOOL_FAILURES == 1
    assert bump_write_tool_failure("skill_manage") == 1
    assert write_tool_failure_count("skill_manage") >= nous_engine._MAX_WRITE_TOOL_FAILURES


def test_broken_circuit_message_forbids_retries_and_detours() -> None:
    payload = json.loads(nous_engine._write_circuit_broken_msg("skill_manage", 1))
    text = payload["error"]
    assert "skill_manage" in text
    assert "NO lo reintentes" in text
    assert "crear habilidades" in text and "instalar conectores" in text
    assert "herramientas ya conectadas" in text


def test_waiting_for_the_owner_is_not_a_failure() -> None:
    pending = '{"error": "BLOCKED: pendiente de aprobación HITL; el dueño decidirá en la tarjeta"}'
    assert nous_engine._write_result_is_failure(pending) is False
    assert nous_engine._write_result_is_failure('{"error": "operations[0] needs a name", "success": false}')
