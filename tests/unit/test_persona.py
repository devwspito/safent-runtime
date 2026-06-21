from __future__ import annotations

import pytest

from hermes import PersonaSpec


def _make(**overrides: object) -> PersonaSpec:
    base: dict[str, object] = {
        "name": "Hermes",
        "role": "Oficial multidisciplinar",
        "language": "es-ES",
        "register": "castellano de despacho",
        "primary_mission": "back-office gestoria",
    }
    base.update(overrides)
    return PersonaSpec(**base)  # type: ignore[arg-type]


def test_requires_name() -> None:
    with pytest.raises(ValueError, match="name is required"):
        _make(name="")


def test_requires_role() -> None:
    with pytest.raises(ValueError, match="role is required"):
        _make(role="")


def test_requires_language() -> None:
    with pytest.raises(ValueError, match="language is required"):
        _make(language="")


def test_requires_mission() -> None:
    with pytest.raises(ValueError, match="primary_mission is required"):
        _make(primary_mission="")


def test_valid_persona() -> None:
    persona = _make(
        golden_rules=("Sujetos SII no presentan 347.",),
        forbidden_phrases=("como asistente",),
    )
    assert persona.name == "Hermes"
    assert "Sujetos SII no presentan 347." in persona.golden_rules
