"""T031 🔒 — Contrato exacto de introspección D-Bus org.hermes.Runtime1.

Verifica que el adapter exporta la interfaz con:
  - nombre exacto org.hermes.Runtime1
  - métodos y firmas exactas según dbus_runtime_iface_v1.md
  - señales declaradas
  - Enqueue NO tiene 'enqueued_by' como parámetro de entrada (G1/G2: el campo
    se deriva server-side, nunca del payload del cliente).

No requiere bus real: usa ServiceInterface.introspect() directamente.
"""

from __future__ import annotations

import pytest

# El adapter importa dbus_fast a nivel de módulo; sin la dep, skip limpio en vez
# de abortar la colección de toda la suite (hallazgo del checkpoint US2).
pytest.importorskip("dbus_fast")

from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (  # noqa: E402
    Runtime1ServiceInterface,
)

pytestmark = pytest.mark.unit

_IFACE_NAME = "org.hermes.Runtime1"


class _StubWiring:
    """Wiring stub mínimo para construir la interfaz sin bus ni puertos reales."""

    async def enqueue(self, **_) -> None: ...  # noqa: ANN001
    async def request_pause(self, **_) -> None: ...  # noqa: ANN001
    async def request_resume(self, **_) -> None: ...  # noqa: ANN001
    async def approve_action(self, **_) -> object: ...  # noqa: ANN001
    async def reject_action(self, **_) -> None: ...  # noqa: ANN001
    async def get_queue_status(self) -> dict: return {}
    async def list_pending(self, **_) -> list: return []
    async def get_task_status(self, **_) -> dict: return {}


@pytest.fixture()
def iface() -> Runtime1ServiceInterface:
    """Interface con wiring stub — sólo para inspección del contrato."""
    return Runtime1ServiceInterface(wiring=_StubWiring())  # type: ignore[arg-type]


class TestInterfaceName:
    def test_interface_name_exact(self, iface: Runtime1ServiceInterface) -> None:
        """El nombre de la interfaz es EXACTAMENTE org.hermes.Runtime1 (v1)."""
        assert iface.name == _IFACE_NAME


class TestMethodSignatures:
    """Verifica métodos declarados y firmas D-Bus (in/out).

    Fuente de verdad: dbus_runtime_iface_v1.md §Métodos.
    """

    def _method_map(self, iface: Runtime1ServiceInterface) -> dict[str, object]:
        from dbus_fast.service import ServiceInterface

        return {
            m.name: m for m in ServiceInterface._get_methods(iface)
        }

    def test_enqueue_exists(self, iface: Runtime1ServiceInterface) -> None:
        methods = self._method_map(iface)
        assert "Enqueue" in methods, "Método Enqueue debe estar declarado"

    def test_enqueue_in_signature(self, iface: Runtime1ServiceInterface) -> None:
        """Enqueue: trigger_kind(s) text(s) priority(i) dedup_key(s) conversation_id(s) operator_token(s) agent_id(s) → 7 args in.

        The 5th argument (conversation_id, type 's') was added when the daemon
        threading of conversation_id was completed.  An empty string means no
        conversation_id; the adapter treats "" as None.  chat_message triggers
        MUST supply a non-empty conversation_id or the work item is silently
        dropped by INSERT OR IGNORE (invariant I5 of the agent_tasks schema).

        The 6th argument (operator_token, type 's') was added by the
        confused-deputy remediation (CWE-862): "" for DIRECT calls by an
        authorized operator (sender_uid ∈ authorized_uids); a signed token,
        MANDATORY, for PROXY calls from the shell-server — the real operator_id
        is extracted from it, never the proxy's uid.

        The 7th argument (agent_id, type 's') was added by the per-conversation
        agent contract (cycle B / phase 1, commit 2d103ac) which killed the
        global active-agent singleton: it names the agent hired for this
        conversation ("" = unspecified). It carries NO authority — like every
        other arg it is subject-to server-side authZ (sender_uid resolved by
        _resolve_current_sender_uid); enqueued_by is still injected server-side
        and is NOT a parameter (see test_enqueue_has_no_enqueued_by_parameter).
        """
        methods = self._method_map(iface)
        m = methods["Enqueue"]
        assert m.in_signature == "ssissss", (
            f"Enqueue in_signature debe ser 'ssissss', got '{m.in_signature}'"
        )

    def test_enqueue_out_signature(self, iface: Runtime1ServiceInterface) -> None:
        """Enqueue devuelve task_id(s) y stream_path(s)."""
        methods = self._method_map(iface)
        m = methods["Enqueue"]
        assert m.out_signature == "ss", (
            f"Enqueue out_signature debe ser 'ss', got '{m.out_signature}'"
        )

    def test_enqueue_has_no_enqueued_by_parameter(
        self, iface: Runtime1ServiceInterface
    ) -> None:
        """`enqueued_by` NO es parámetro de Enqueue — se inyecta server-side.

        CTRL-P1-3 / G2: cualquier enqueued_by del payload se descarta. La interfaz
        D-Bus no debe exponerlo como argumento (sería spoofeable).
        """
        methods = self._method_map(iface)
        m = methods["Enqueue"]
        in_args = [arg.name for arg in m.introspection.in_args]
        assert "enqueued_by" not in in_args, (
            f"enqueued_by NO debe ser parámetro de Enqueue. Args: {in_args}"
        )

    def test_get_queue_status_exists_no_in(self, iface: Runtime1ServiceInterface) -> None:
        """GetQueueStatus: sin argumentos de entrada."""
        methods = self._method_map(iface)
        assert "GetQueueStatus" in methods
        assert methods["GetQueueStatus"].in_signature == ""

    def test_get_queue_status_out_dict(self, iface: Runtime1ServiceInterface) -> None:
        """GetQueueStatus devuelve a{sv}."""
        methods = self._method_map(iface)
        assert methods["GetQueueStatus"].out_signature == "a{sv}"

    def test_list_pending_in_signature(self, iface: Runtime1ServiceInterface) -> None:
        """ListPending: limit(u) → a(ssis)."""
        methods = self._method_map(iface)
        assert "ListPending" in methods
        m = methods["ListPending"]
        assert m.in_signature == "u"
        assert m.out_signature == "a(ssis)"

    def test_get_task_status_in_signature(self, iface: Runtime1ServiceInterface) -> None:
        """GetTaskStatus: task_id(s) → a{sv}."""
        methods = self._method_map(iface)
        assert "GetTaskStatus" in methods
        m = methods["GetTaskStatus"]
        assert m.in_signature == "s"
        assert m.out_signature == "a{sv}"

    def test_pause_in_signature(self, iface: Runtime1ServiceInterface) -> None:
        """Pause: reason(s) → ok(b)."""
        methods = self._method_map(iface)
        assert "Pause" in methods
        m = methods["Pause"]
        assert m.in_signature == "s"
        assert m.out_signature == "b"

    def test_resume_no_in(self, iface: Runtime1ServiceInterface) -> None:
        """Resume: sin args → ok(b)."""
        methods = self._method_map(iface)
        assert "Resume" in methods
        m = methods["Resume"]
        assert m.in_signature == ""
        assert m.out_signature == "b"

    def test_approve_in_signature(self, iface: Runtime1ServiceInterface) -> None:
        """Approve: proposal_id(s) totp(s) → approval_token(s).

        The 2nd arg (totp, type 's') was added so the owner's TOTP reaches the gate —
        the single MFA enforcement point for all surfaces (TOTP-only model 2026-06-24)."""
        methods = self._method_map(iface)
        assert "Approve" in methods
        m = methods["Approve"]
        assert m.in_signature == "ss"
        assert m.out_signature == "s"

    def test_reject_in_signature(self, iface: Runtime1ServiceInterface) -> None:
        """Reject: proposal_id(s) reason(s) → ok(b)."""
        methods = self._method_map(iface)
        assert "Reject" in methods
        m = methods["Reject"]
        assert m.in_signature == "ss"
        assert m.out_signature == "b"


class TestSignals:
    """Verifica señales declaradas según dbus_runtime_iface_v1.md §Signals."""

    def _signal_map(self, iface: Runtime1ServiceInterface) -> dict[str, object]:
        from dbus_fast.service import ServiceInterface

        return {
            s.name: s for s in ServiceInterface._get_signals(iface)
        }

    def test_task_enqueued_signal(self, iface: Runtime1ServiceInterface) -> None:
        """TaskEnqueued: task_id(s) trigger_kind(s) priority(i) → sig='ssi'."""
        signals = self._signal_map(iface)
        assert "TaskEnqueued" in signals
        assert signals["TaskEnqueued"].signature == "ssi"

    def test_task_status_changed_signal(self, iface: Runtime1ServiceInterface) -> None:
        """TaskStatusChanged: task_id(s) old_status(s) new_status(s) → 'sss'."""
        signals = self._signal_map(iface)
        assert "TaskStatusChanged" in signals
        assert signals["TaskStatusChanged"].signature == "sss"

    def test_task_pending_approval_signal(self, iface: Runtime1ServiceInterface) -> None:
        """TaskPendingApproval: task_id(s) proposal_id(s) risk(s) → 'sss'."""
        signals = self._signal_map(iface)
        assert "TaskPendingApproval" in signals
        assert signals["TaskPendingApproval"].signature == "sss"

    def test_agent_liveness_changed_signal(self, iface: Runtime1ServiceInterface) -> None:
        """AgentLivenessChanged: alive(b) has_model(b) → 'bb'."""
        signals = self._signal_map(iface)
        assert "AgentLivenessChanged" in signals
        assert signals["AgentLivenessChanged"].signature == "bb"
