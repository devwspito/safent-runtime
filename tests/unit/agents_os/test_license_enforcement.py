"""Unit tests — associate license enforcement in DbusRuntimeServiceWiring (Fase 3a).

Coverage:
  create_agent:
    - CE (no association_store): creates without restriction
    - associate, not expired, under limit: creates normally
    - associate, max_agents reached: raises LicenseExceeded; no agent created
    - associate, license expired: raises LicenseExpired; no agent created
    - CE path (is_associated=False) with store present: creates normally

  enqueue:
    - CE path: enqueue proceeds (no restriction)
    - associate, expired license: raises LicenseExpired before delegating

  data invariant:
    - LicenseExceeded must not delete or modify existing agents
    - LicenseExpired must not delete or modify existing agents
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes.agents.application.serialization import draft_from_dict
from hermes.agents.domain.agent import DEFAULT_AGENT_ID
from hermes.agents.domain.ports import LicenseExceeded, LicenseExpired
from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusRuntimeServiceWiring,
)

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "shell-state.db"


@pytest.fixture
def registry(db_path: Path) -> SqliteAgentRegistry:
    return SqliteAgentRegistry(db_path=db_path)


def _make_store(
    *,
    is_associated: bool = True,
    max_agents: int | None = None,
    expires_at: str | None = None,
) -> MagicMock:
    store = MagicMock()
    store.is_associated.return_value = is_associated
    if is_associated:
        assoc = MagicMock()
        lic: dict = {}
        if max_agents is not None:
            lic["max_agents"] = max_agents
        if expires_at is not None:
            lic["expires_at"] = expires_at
        assoc.license = lic
        store.get.return_value = assoc
    else:
        store.get.return_value = None
    return store


def _wiring(
    registry: SqliteAgentRegistry,
    association_store=None,
) -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=None,
        approval_gate=None,
        authorized_uids=frozenset({_OPERATOR_UID}),
        agent_registry=registry,
        association_store=association_store,
    )


def _draft(name: str = "Test") -> object:
    return draft_from_dict({"name": name})


def _cloud_draft(name: str = "Test") -> object:
    """A cloud-managed agent — the only kind that consumes a per-agent license
    seat. The CE roster + locally-created agents are bundled and never count."""
    return draft_from_dict({"name": name, "managed_by": "cloud"})


# ---------------------------------------------------------------------------
# create_agent — CE (no association_store)
# ---------------------------------------------------------------------------


class TestCreateAgentCE:
    def test_creates_without_restriction_in_ce(
        self, registry: SqliteAgentRegistry
    ) -> None:
        w = _wiring(registry, association_store=None)
        initial = len(registry.list_agents())
        asyncio.run(w.create_agent(draft=_draft("Alpha"), sender_uid=_OPERATOR_UID))
        assert len(registry.list_agents()) == initial + 1

    def test_creates_when_not_associated(
        self, registry: SqliteAgentRegistry
    ) -> None:
        store = _make_store(is_associated=False)
        w = _wiring(registry, association_store=store)
        initial = len(registry.list_agents())
        asyncio.run(w.create_agent(draft=_draft("Beta"), sender_uid=_OPERATOR_UID))
        assert len(registry.list_agents()) == initial + 1


# ---------------------------------------------------------------------------
# create_agent — associate, within license
# ---------------------------------------------------------------------------


class TestCreateAgentAssociateWithinLicense:
    def test_creates_when_under_max_agents(
        self, registry: SqliteAgentRegistry
    ) -> None:
        # default + roster seeded; set max_agents well above current count
        current = len(registry.list_agents())
        store = _make_store(max_agents=current + 10)
        w = _wiring(registry, association_store=store)
        created = asyncio.run(
            w.create_agent(draft=_draft("Gamma"), sender_uid=_OPERATOR_UID)
        )
        assert created["name"] == "Gamma"

    def test_creates_when_no_max_agents_key(
        self, registry: SqliteAgentRegistry
    ) -> None:
        store = _make_store(max_agents=None)
        w = _wiring(registry, association_store=store)
        initial = len(registry.list_agents())
        asyncio.run(w.create_agent(draft=_draft("Delta"), sender_uid=_OPERATOR_UID))
        assert len(registry.list_agents()) == initial + 1

    def test_creates_when_future_expiry(
        self, registry: SqliteAgentRegistry
    ) -> None:
        future = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()
        store = _make_store(expires_at=future)
        w = _wiring(registry, association_store=store)
        initial = len(registry.list_agents())
        asyncio.run(w.create_agent(draft=_draft("Epsilon"), sender_uid=_OPERATOR_UID))
        assert len(registry.list_agents()) == initial + 1

    def test_creates_when_naive_date_only_future_expiry(
        self, registry: SqliteAgentRegistry
    ) -> None:
        """A date-only / tz-naive expires_at (e.g. "2027-12-31", the common
        console value) must NOT raise `TypeError: can't compare offset-naive and
        offset-aware datetimes` — it broke create_agent for EVERY cloud agent
        (2026-07-05, caught by the 20-employee Enterprise live test)."""
        store = _make_store(expires_at="2027-12-31")  # naive, no tz, date-only
        w = _wiring(registry, association_store=store)
        initial = len(registry.list_agents())
        asyncio.run(w.create_agent(draft=_draft("EpsilonNaive"), sender_uid=_OPERATOR_UID))
        assert len(registry.list_agents()) == initial + 1


# ---------------------------------------------------------------------------
# create_agent — associate, limit exceeded
# ---------------------------------------------------------------------------


class TestCreateAgentLicenseExceeded:
    def test_raises_license_exceeded_when_at_max(
        self, registry: SqliteAgentRegistry
    ) -> None:
        # The license caps cloud-managed agents only. Fill the single seat with a
        # cloud-managed agent, then a second cloud-managed agent must be rejected.
        store = _make_store(max_agents=1)
        w = _wiring(registry, association_store=store)
        asyncio.run(w.create_agent(draft=_cloud_draft("Cloud-1"), sender_uid=_OPERATOR_UID))
        with pytest.raises(LicenseExceeded):
            asyncio.run(w.create_agent(draft=_cloud_draft("Cloud-2"), sender_uid=_OPERATOR_UID))

    def test_no_agent_created_when_exceeded(
        self, registry: SqliteAgentRegistry
    ) -> None:
        """Invariant: no data is created when limit is reached."""
        store = _make_store(max_agents=1)
        w = _wiring(registry, association_store=store)
        asyncio.run(w.create_agent(draft=_cloud_draft("Cloud-1"), sender_uid=_OPERATOR_UID))
        after_first = len(registry.list_agents())
        try:
            asyncio.run(w.create_agent(draft=_cloud_draft("Cloud-2"), sender_uid=_OPERATOR_UID))
        except LicenseExceeded:
            pass
        assert len(registry.list_agents()) == after_first  # no agent was created

    def test_local_agents_do_not_consume_license_seats(
        self, registry: SqliteAgentRegistry
    ) -> None:
        """FIX 3: only cloud-managed agents count against the per-agent license.
        The CE roster + locally-created agents never consume a seat — otherwise a
        fresh associate (28 default agents) would exceed any small license."""
        store = _make_store(max_agents=1)
        w = _wiring(registry, association_store=store)
        # Local agents create freely even with a 1-seat license...
        asyncio.run(w.create_agent(draft=_draft("Local-1"), sender_uid=_OPERATOR_UID))
        asyncio.run(w.create_agent(draft=_draft("Local-2"), sender_uid=_OPERATOR_UID))
        # ...and the single cloud seat is still available.
        created = asyncio.run(
            w.create_agent(draft=_cloud_draft("Cloud-1"), sender_uid=_OPERATOR_UID)
        )
        assert created["name"] == "Cloud-1"
        # The 2nd cloud-managed agent exceeds the 1-seat license.
        with pytest.raises(LicenseExceeded):
            asyncio.run(w.create_agent(draft=_cloud_draft("Cloud-2"), sender_uid=_OPERATOR_UID))

    def test_default_agent_untouched_when_exceeded(
        self, registry: SqliteAgentRegistry
    ) -> None:
        """Invariant: existing default agent must not be deleted."""
        current = len(registry.list_agents())
        store = _make_store(max_agents=current)
        w = _wiring(registry, association_store=store)
        try:
            asyncio.run(w.create_agent(draft=_draft("Z"), sender_uid=_OPERATOR_UID))
        except LicenseExceeded:
            pass
        ids = {a["agent_id"] for a in w.list_agents()}
        assert DEFAULT_AGENT_ID in ids


# ---------------------------------------------------------------------------
# create_agent — associate, license expired
# ---------------------------------------------------------------------------


class TestCreateAgentLicenseExpired:
    def test_raises_license_expired_when_past(
        self, registry: SqliteAgentRegistry
    ) -> None:
        past = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat()
        store = _make_store(expires_at=past)
        w = _wiring(registry, association_store=store)
        with pytest.raises(LicenseExpired):
            asyncio.run(w.create_agent(draft=_draft("Expired"), sender_uid=_OPERATOR_UID))

    def test_no_agent_created_when_expired(
        self, registry: SqliteAgentRegistry
    ) -> None:
        past = (datetime.now(tz=UTC) - timedelta(days=7)).isoformat()
        store = _make_store(expires_at=past)
        w = _wiring(registry, association_store=store)
        initial = len(registry.list_agents())
        try:
            asyncio.run(w.create_agent(draft=_draft("Exp2"), sender_uid=_OPERATOR_UID))
        except LicenseExpired:
            pass
        assert len(registry.list_agents()) == initial  # no agent was created

    def test_existing_agents_untouched_when_expired(
        self, registry: SqliteAgentRegistry
    ) -> None:
        """Invariant: existing agents must not be deleted on expiry enforcement."""
        past = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
        store = _make_store(expires_at=past)
        w = _wiring(registry, association_store=store)
        agents_before = {a["agent_id"] for a in w.list_agents()}
        try:
            asyncio.run(w.create_agent(draft=_draft("Exp3"), sender_uid=_OPERATOR_UID))
        except LicenseExpired:
            pass
        agents_after = {a["agent_id"] for a in w.list_agents()}
        assert agents_before == agents_after  # nothing was deleted


# ---------------------------------------------------------------------------
# enqueue — license expiry
# ---------------------------------------------------------------------------


class _FakeControlPlane:
    """Minimal stub to satisfy the cp_service delegation in enqueue."""

    def __init__(self) -> None:
        self.called = False

    async def enqueue(self, **_kwargs):  # noqa: ANN202
        self.called = True
        result = MagicMock()
        result.task_id = "fake-task-id"
        result.stream_path = "/ws/tasks/fake-task-id"
        return result

    def audit_entries_emitted(self) -> list:
        return []


def _wiring_with_cp(
    registry: SqliteAgentRegistry,
    association_store=None,
) -> tuple[DbusRuntimeServiceWiring, _FakeControlPlane]:
    cp = _FakeControlPlane()
    w = DbusRuntimeServiceWiring(
        agent_state=None,
        approval_gate=None,
        authorized_uids=frozenset({_OPERATOR_UID}),
        agent_registry=registry,
        control_plane_service=cp,  # type: ignore[arg-type]
        association_store=association_store,
        proxy_uid=None,
    )
    return w, cp


class TestEnqueueLicenseEnforcement:
    def test_enqueue_raises_license_expired_when_expired(
        self, registry: SqliteAgentRegistry
    ) -> None:
        past = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
        store = _make_store(expires_at=past)
        w, cp = _wiring_with_cp(registry, association_store=store)
        with pytest.raises(LicenseExpired):
            asyncio.run(
                w.enqueue(
                    trigger_kind="chat_message",
                    text="hello",
                    priority=0,
                    dedup_key=None,
                    sender_uid=_OPERATOR_UID,
                )
            )
        assert not cp.called  # nothing was enqueued

    def test_enqueue_proceeds_in_ce(
        self, registry: SqliteAgentRegistry
    ) -> None:
        w, cp = _wiring_with_cp(registry, association_store=None)
        # cp.service is not None but the underlying call will succeed on our stub
        asyncio.run(
            w.enqueue(
                trigger_kind="chat_message",
                text="hello",
                priority=0,
                dedup_key=None,
                sender_uid=_OPERATOR_UID,
            )
        )
        assert cp.called

    def test_enqueue_proceeds_when_not_expired(
        self, registry: SqliteAgentRegistry
    ) -> None:
        future = (datetime.now(tz=UTC) + timedelta(days=30)).isoformat()
        store = _make_store(expires_at=future)
        w, cp = _wiring_with_cp(registry, association_store=store)
        asyncio.run(
            w.enqueue(
                trigger_kind="chat_message",
                text="hello",
                priority=0,
                dedup_key=None,
                sender_uid=_OPERATOR_UID,
            )
        )
        assert cp.called
