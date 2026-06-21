"""T015 — VOs del bounded context `execution` (InputSurfaceKey, ExecutionContextId, ExecutionContext).

Verifica:
- InputSurfaceKey es frozen (inmutable) e igualdad estructural por (kind, surface_id).
- Misma (kind, surface_id) => colisionan (== True, mismo hash).
- surface distinto => NO colisionan.
- isolation_key / surface_id distinto => NO colisionan.
- ExecutionContextId es frozen e igualdad estructural.
- ExecutionContext es frozen y contiene todos sus campos.
- Enums InputSurfaceKind / InputOwnerKind tienen los valores del contrato.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.execution.domain.ports import (
    ExecutionContext,
    ExecutionContextId,
    InputOwnerKind,
    InputSurfaceKey,
    InputSurfaceKind,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# InputSurfaceKind enum
# ---------------------------------------------------------------------------


def test_input_surface_kind_values() -> None:
    assert InputSurfaceKind.KEYBOARD == "keyboard"
    assert InputSurfaceKind.MOUSE == "mouse"
    assert InputSurfaceKind.SCREEN == "screen"
    assert InputSurfaceKind.BROWSER == "browser"


# ---------------------------------------------------------------------------
# InputOwnerKind enum
# ---------------------------------------------------------------------------


def test_input_owner_kind_values() -> None:
    assert InputOwnerKind.OPERATOR == "operator"
    assert InputOwnerKind.AGENT_TASK == "agent_task"


# ---------------------------------------------------------------------------
# InputSurfaceKey — igualdad y hashability
# ---------------------------------------------------------------------------


def test_same_kind_same_surface_id_are_equal() -> None:
    a = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="session-abc")
    b = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="session-abc")
    assert a == b


def test_same_kind_same_surface_id_have_same_hash() -> None:
    a = InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="primary")
    b = InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="primary")
    assert hash(a) == hash(b)


def test_same_kind_different_surface_id_not_equal() -> None:
    a = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="session-1")
    b = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="session-2")
    assert a != b


def test_different_kind_same_surface_id_not_equal() -> None:
    a = InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="primary")
    b = InputSurfaceKey(kind=InputSurfaceKind.MOUSE, surface_id="primary")
    assert a != b


def test_input_surface_key_is_frozen() -> None:
    key = InputSurfaceKey(kind=InputSurfaceKind.SCREEN, surface_id="hdmi-0")
    with pytest.raises((AttributeError, TypeError)):
        key.surface_id = "mutated"  # type: ignore[misc]


def test_input_surface_key_usable_as_dict_key() -> None:
    k1 = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="s1")
    k2 = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="s1")
    registry: dict[InputSurfaceKey, str] = {k1: "owner-A"}
    assert registry[k2] == "owner-A"


def test_two_different_keys_do_not_collide_in_dict() -> None:
    k1 = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="s1")
    k2 = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="s2")
    registry: dict[InputSurfaceKey, str] = {k1: "owner-A", k2: "owner-B"}
    assert registry[k1] != registry[k2]


# ---------------------------------------------------------------------------
# ExecutionContextId — frozen, igualdad estructural
# ---------------------------------------------------------------------------


def test_execution_context_id_equality() -> None:
    uid = uuid4()
    a = ExecutionContextId(value=uid, owner_kind=InputOwnerKind.AGENT_TASK)
    b = ExecutionContextId(value=uid, owner_kind=InputOwnerKind.AGENT_TASK)
    assert a == b


def test_execution_context_id_different_kind_not_equal() -> None:
    uid = uuid4()
    a = ExecutionContextId(value=uid, owner_kind=InputOwnerKind.AGENT_TASK)
    b = ExecutionContextId(value=uid, owner_kind=InputOwnerKind.OPERATOR)
    assert a != b


def test_execution_context_id_is_frozen() -> None:
    ctx_id = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.OPERATOR)
    with pytest.raises((AttributeError, TypeError)):
        ctx_id.owner_kind = InputOwnerKind.AGENT_TASK  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ExecutionContext — frozen, campos coherentes
# ---------------------------------------------------------------------------


def test_execution_context_fields() -> None:
    uid = uuid4()
    ctx_id = ExecutionContextId(value=uid, owner_kind=InputOwnerKind.AGENT_TASK)
    surface = InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="tenant:site")
    ctx = ExecutionContext(
        context_id=ctx_id,
        surface=surface,
        isolation_key="browser-session-xyz",
    )
    assert ctx.context_id == ctx_id
    assert ctx.surface == surface
    assert ctx.isolation_key == "browser-session-xyz"


def test_execution_context_is_frozen() -> None:
    ctx = ExecutionContext(
        context_id=ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.OPERATOR),
        surface=InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="p"),
        isolation_key="primary",
    )
    with pytest.raises((AttributeError, TypeError)):
        ctx.isolation_key = "mutated"  # type: ignore[misc]
