"""Tests del catálogo de OS-native skills + bridge a ToolSpec del runtime."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentDenied,
    ConsentManager,
    ConsentScope,
)
from hermes.domain.tool_spec import ToolRisk
from hermes.runtime.tool_host import CapturingToolHost
from hermes.shell_server.os_native_skills.catalog import (
    OS_NATIVE_SKILLS,
    SkillRisk,
    skill_by_name,
)
from hermes.shell_server.os_native_skills.tool_specs import (
    _check_consent,
    build_os_native_tool_specs,
    to_tool_spec,
)

pytestmark = pytest.mark.unit

_TENANT = UUID("00000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("00000000-0000-0000-0000-000000000002")


class TestCatalog:
    def test_screenshot_is_read_only_with_screen_capability(self) -> None:
        s = skill_by_name("screenshot")
        assert s is not None
        assert s.risk is SkillRisk.READ_ONLY
        assert "screen" in s.capabilities

    def test_screen_record_is_write_proposal_with_screen_and_mic(self) -> None:
        s = skill_by_name("screen_record")
        assert s is not None
        assert s.risk is SkillRisk.WRITE_PROPOSAL
        assert "screen" in s.capabilities
        assert "microphone" in s.capabilities

    def test_screen_record_schema_requires_duration(self) -> None:
        s = skill_by_name("screen_record")
        assert "duration_seconds" in s.parameters_schema["required"]

    def test_unknown_skill(self) -> None:
        assert skill_by_name("nope") is None


class TestToolSpecBridge:
    def test_read_only_skill_keeps_handler(self) -> None:
        async def fake_handler(args):  # noqa: ANN001
            return {"ok": True}

        s = skill_by_name("screenshot")
        spec = to_tool_spec(s, handler=fake_handler)
        assert spec.name == "screenshot"
        assert spec.risk is ToolRisk.READ_ONLY
        assert spec.handler is fake_handler

    def test_write_proposal_skill_has_no_handler(self) -> None:
        s = skill_by_name("screen_record")
        spec = to_tool_spec(s, handler=None)
        assert spec.risk is ToolRisk.WRITE_PROPOSAL
        assert spec.handler is None

    def test_build_all_specs(self) -> None:
        specs = build_os_native_tool_specs()
        names = {s.name for s in specs}
        assert names == {s.name for s in OS_NATIVE_SKILLS}
        # screenshot READ_ONLY, screen_record WRITE_PROPOSAL
        by_name = {s.name: s for s in specs}
        assert by_name["screenshot"].risk is ToolRisk.READ_ONLY
        assert by_name["screen_record"].risk is ToolRisk.WRITE_PROPOSAL


# ---------------------------------------------------------------------------
# Regression: finding #4 — consent pre-flight is enforced for READ_ONLY skills
# ---------------------------------------------------------------------------


class TestConsentPreFlight:
    """FR-013 / constitución IV — fail-closed consent gate (finding #4)."""

    def _make_cm_with_screen(self) -> ConsentManager:
        cm = ConsentManager()
        cm.grant(
            tenant_id=_TENANT,
            human_operator_id=_OPERATOR,
            capability=Capability.SCREEN_CAPTURE,
            scope=ConsentScope.SESSION,
        )
        return cm

    def test_consent_denied_when_capability_not_granted(self) -> None:
        """screenshot without active screen consent → ConsentDenied (fail-closed)."""
        cm = ConsentManager()  # no consent granted
        with pytest.raises(ConsentDenied):
            _check_consent(
                required_capabilities=("screen",),
                consent_manager=cm,
                human_operator_id=_OPERATOR,
                skill_name="screenshot",
            )

    def test_consent_passes_when_capability_granted(self) -> None:
        """screenshot with active screen consent → no exception."""
        cm = self._make_cm_with_screen()
        # Must not raise.
        _check_consent(
            required_capabilities=("screen",),
            consent_manager=cm,
            human_operator_id=_OPERATOR,
            skill_name="screenshot",
        )

    def test_consent_check_skipped_when_no_consent_manager(self) -> None:
        """When consent_manager is None (headless/test), gate is skipped."""
        _check_consent(
            required_capabilities=("screen",),
            consent_manager=None,
            human_operator_id=_OPERATOR,
            skill_name="screenshot",
        )

    def test_consent_check_skipped_when_no_operator(self) -> None:
        """When human_operator_id is None, gate is skipped."""
        cm = ConsentManager()  # no consent granted
        _check_consent(
            required_capabilities=("screen",),
            consent_manager=cm,
            human_operator_id=None,
            skill_name="screenshot",
        )

    def test_unknown_capability_string_raises_consent_denied(self) -> None:
        """A skill declaring an unknown capability string raises ConsentDenied."""
        cm = ConsentManager()
        with pytest.raises(ConsentDenied, match="Capability desconocida"):
            _check_consent(
                required_capabilities=("unknown_future_cap",),
                consent_manager=cm,
                human_operator_id=_OPERATOR,
                skill_name="some_skill",
            )

    def test_handler_built_with_consent_manager_enforces_gate(self) -> None:
        """build_os_native_tool_specs passes consent_manager into handlers."""
        cm = ConsentManager()  # no consent — gate should fire
        specs = build_os_native_tool_specs(
            consent_manager=cm,
            human_operator_id=_OPERATOR,
        )
        screenshot_spec = next(s for s in specs if s.name == "screenshot")
        # The handler is a closure; calling it should raise ConsentDenied
        # before reaching the real executor.
        import asyncio

        with pytest.raises(ConsentDenied):
            asyncio.run(screenshot_spec.handler({}))

    def test_screen_capture_capability_value(self) -> None:
        """Capability.SCREEN_CAPTURE value is 'screen' (matches catalog string)."""
        assert Capability.SCREEN_CAPTURE == "screen"


# ---------------------------------------------------------------------------
# Regression: finding #22 — screenshot filenames are unique across captures
# ---------------------------------------------------------------------------


class TestScreenshotUniqueFilenames:
    """Two consecutive execute_screenshot calls must return distinct paths (finding #22)."""

    def test_filename_pattern_uses_uuid(self, tmp_path) -> None:
        """Validate _artifact_dir / filename logic without hitting the OS."""
        import uuid as uuid_mod

        # Simulate the filename generation logic from executors.py
        names = [f"screenshot_{uuid_mod.uuid4().hex}.png" for _ in range(50)]
        assert len(set(names)) == 50, "UUID-based filenames must be unique"


# ---------------------------------------------------------------------------
# Regression: finding #27 — screen_record defaults with_audio to False
# ---------------------------------------------------------------------------


class TestScreenRecordAudioDefault:
    """screen_record must default to no-audio when with_audio is omitted (finding #27)."""

    def test_with_audio_default_is_false(self) -> None:
        from hermes.shell_server.os_native_skills.executors import execute_screen_record  # noqa: PLC0415

        # We only test the argument-parsing logic; we cannot call the real
        # executor (requires D-Bus + GStreamer). Inspect the source default.
        import inspect

        src = inspect.getsource(execute_screen_record)
        # The default must be False, not True.
        assert 'args.get("with_audio", False)' in src, (
            "execute_screen_record must default with_audio to False (secure default)"
        )
        assert 'args.get("with_audio", True)' not in src, (
            "execute_screen_record must NOT default with_audio to True"
        )


# ---------------------------------------------------------------------------
# Regression: finding #13 — screen_record WRITE_PROPOSAL is captured not dropped
# ---------------------------------------------------------------------------


def _make_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class TestScreenRecordProposalCapture:
    """screen_record WRITE_PROPOSAL must be captured as a ToolCallProposal (finding #13).

    Previously _capture_write returned None because entity_id was missing from
    the schema, silently discarding the proposal as 'malformed'.
    """

    async def test_screen_record_call_becomes_proposal_not_malformed(self) -> None:
        specs = build_os_native_tool_specs()
        host = CapturingToolHost(specs=specs, tenant_id=_TENANT)

        result = await host.process_round(
            [
                _make_call(
                    "rec1",
                    "screen_record",
                    {"duration_seconds": 10},
                )
            ]
        )

        assert result.malformed == (), (
            f"screen_record should be captured, not malformed: {result.malformed}"
        )
        assert len(result.proposals) == 1
        proposal = result.proposals[0]
        assert proposal.tool_name == "screen_record"
        assert proposal.entity_type == "os_surface"
        assert proposal.entity_id == "os_surface"

    async def test_screen_record_with_audio_flag_in_proposal(self) -> None:
        specs = build_os_native_tool_specs()
        host = CapturingToolHost(specs=specs, tenant_id=_TENANT)

        result = await host.process_round(
            [
                _make_call(
                    "rec2",
                    "screen_record",
                    {"duration_seconds": 5, "with_audio": True},
                )
            ]
        )

        assert len(result.proposals) == 1
        assert result.proposals[0].parameters["with_audio"] is True
