"""CLI-N4 (specs/025-safent-repaso matriz-final-39eeb8e, CWE-778) — the
emergency-brake pause/resume audit trail must never be silently dropped.

Before this fix, `hermes.runtime.__main__` built `SqliteAgentState(db_path=...)`
with NO `signer`/`audit_repo` (both were built a few lines further down, for
OTHER components), and `SqliteAgentState` absorbed the missing signer with a
bare `return` inside `_emit_audit_paused`/`_emit_audit_resumed` — no log, no
exception. `safent brake release` printed 'audited as the owner,
reason="host_cli"' while the signed chain grew with
task_claimed/task_completed/... but ZERO `AGENT_PAUSED`/`AGENT_RESUMED` (live
evidence: matriz-final-39eeb8e CLI-N4, reproduced twice on the DGX).

Covers:
  - `hermes.runtime.__main__._build_agent_state` — the composition-root seam
    that wires the SAME `firmer`/`audit_repo` every other audited action
    uses (`_build_real_broker`'s approval_gate, `register_security_hooks`)
    into `SqliteAgentState` — raises loudly instead of degrading when they
    are unavailable.
  - `SqliteAgentState` itself — construction fails without signer/audit_repo,
    and, wired correctly, `pause()`/`resume()` actually reach the signed
    chain with `reason` intact (the exact fact CLI-N4's live check found
    missing).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind
from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState

pytestmark = pytest.mark.unit

_OPERATOR = uuid4()


def _real_audit_deps(tmp_path: Path) -> tuple[AuditHashChainSigner, SqliteAuditRepository]:
    signer = AuditHashChainSigner(signing_key=os.urandom(32))
    audit_repo = SqliteAuditRepository(db_path=tmp_path / "audit.db")
    return signer, audit_repo


class TestBuildAgentStateCompositionRoot:
    """`hermes.runtime.__main__._build_agent_state` — the composition-root seam."""

    def test_raises_when_signer_and_audit_repo_are_both_missing(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _build_agent_state

        with pytest.raises(RuntimeError, match="agent_state_audit_unavailable"):
            _build_agent_state(db_path=tmp_path / "shell-state.db", firmer=None, audit_repo=None)

    def test_raises_when_only_audit_repo_is_missing(self, tmp_path: Path) -> None:
        """Partial wiring is as dangerous as none — must not silently degrade."""
        from hermes.runtime.__main__ import _build_agent_state

        signer, _unused_repo = _real_audit_deps(tmp_path)
        with pytest.raises(RuntimeError, match="agent_state_audit_unavailable"):
            _build_agent_state(db_path=tmp_path / "shell-state.db", firmer=signer, audit_repo=None)

    def test_wires_the_real_signer_and_audit_repo_into_a_working_state(
        self, tmp_path: Path
    ) -> None:
        from hermes.runtime.__main__ import _build_agent_state

        signer, audit_repo = _real_audit_deps(tmp_path)
        state = _build_agent_state(
            db_path=tmp_path / "shell-state.db", firmer=signer, audit_repo=audit_repo
        )
        assert isinstance(state, SqliteAgentState)


class TestSqliteAgentStateFailsLoudWithoutAudit:
    def test_construction_raises_without_signer_or_audit_repo(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="audit_wiring_missing"):
            SqliteAgentState(db_path=tmp_path / "shell-state.db", signer=None, audit_repo=None)


class TestPauseResumeReachTheSignedChain:
    """The exact fact CLI-N4's live check verified was missing: after
    pause + release, the signed chain gains `AGENT_PAUSED`/`AGENT_RESUMED`
    rows, and a host-CLI release is distinguishable from a TOTP-gated one via
    `reason` (`8907b94` / security review 2026-09-10, CWE-778)."""

    async def test_pause_then_host_cli_release_both_reach_the_chain_with_reason(
        self, tmp_path: Path
    ) -> None:
        signer, audit_repo = _real_audit_deps(tmp_path)
        state = SqliteAgentState(
            db_path=tmp_path / "shell-state.db", signer=signer, audit_repo=audit_repo
        )

        await state.pause(by=_OPERATOR, reason="emergency brake engaged")
        await state.resume(by=_OPERATOR, reason="host_cli")

        chain = await audit_repo.load_chain()
        kinds = [e.audit_kind for e in chain]
        assert kinds.count(AuditKind.AGENT_PAUSED) == 1
        assert kinds.count(AuditKind.AGENT_RESUMED) == 1

        resumed = next(e for e in chain if e.audit_kind == AuditKind.AGENT_RESUMED)
        assert "host_cli" in resumed.description
