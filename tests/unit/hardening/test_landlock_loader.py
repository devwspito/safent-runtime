"""Unit tests for hermes.security.landlock_loader.

Estrategia:
  - No se puede aplicar Landlock real en CI (requiere kernel con LSM activo
    y permiso root). Los tests cubren:
    1. Parseo/construcción: load_and_apply con Landlock simulado como ausente.
    2. Manejo de capability inválida → exit 2.
    3. Manejo de "Landlock ausente" → exit 0 + log (soft-degrade).
    4. _detect_abi en entorno sin Landlock → None.
    5. _access_mask_for_rules: cálculo correcto de bitmask + degrade por ABI.
    6. Integración constructor: build_browser_ruleset + load_and_apply (mocked).

Tests que requieren VM (kernel real con Landlock) están marcados con
`requires_vm` y excluidos en CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hermes.security.landlock_loader import (
    _access_mask_for_rules,
    _detect_abi,
    load_and_apply,
    main,
)

pytestmark = pytest.mark.unit


class TestDetectAbi:
    def test_returns_none_when_syscall_raises_enosys(self) -> None:
        """En CI (sin Landlock) la syscall devuelve ENOSYS → _detect_abi() = None."""
        with patch("hermes.security.landlock_loader._raw_syscall", return_value=-1), \
             patch("hermes.security.landlock_loader._errno", return_value=38):  # ENOSYS
            result = _detect_abi()
        assert result is None

    def test_returns_none_when_unsupported_arch(self) -> None:
        from hermes.security.landlock_loader import UnsupportedArchError  # noqa: PLC0415
        with patch("hermes.security.landlock_loader._syscall_nr",
                   side_effect=UnsupportedArchError("unknown arch")):
            result = _detect_abi()
        assert result is None

    def test_returns_version_when_syscall_succeeds(self) -> None:
        with patch("hermes.security.landlock_loader._raw_syscall", return_value=3):
            result = _detect_abi()
        assert result == 3


class TestAccessMaskForRules:
    def test_read_file_bit(self) -> None:
        mask = _access_mask_for_rules([frozenset({"read_file"})], abi_version=1)
        assert mask & (1 << 2)  # bit 2 = read_file

    def test_write_file_bit(self) -> None:
        mask = _access_mask_for_rules([frozenset({"write_file"})], abi_version=1)
        assert mask & (1 << 1)

    def test_execute_bit(self) -> None:
        mask = _access_mask_for_rules([frozenset({"execute"})], abi_version=1)
        assert mask & (1 << 0)

    def test_truncate_excluded_on_abi1(self) -> None:
        # truncate is ABI 3+; on ABI 1 it must be excluded (degrade).
        mask = _access_mask_for_rules([frozenset({"truncate"})], abi_version=1)
        assert mask == 0

    def test_truncate_included_on_abi3(self) -> None:
        mask = _access_mask_for_rules([frozenset({"truncate"})], abi_version=3)
        assert mask & (1 << 14)

    def test_refer_excluded_on_abi1(self) -> None:
        mask = _access_mask_for_rules([frozenset({"refer"})], abi_version=1)
        assert mask == 0

    def test_refer_included_on_abi2(self) -> None:
        mask = _access_mask_for_rules([frozenset({"refer"})], abi_version=2)
        assert mask & (1 << 13)

    def test_unknown_right_ignored(self) -> None:
        mask = _access_mask_for_rules([frozenset({"nonexistent_right"})], abi_version=3)
        assert mask == 0

    def test_multiple_rules_combined(self) -> None:
        mask = _access_mask_for_rules(
            [frozenset({"read_file"}), frozenset({"write_file"})], abi_version=1
        )
        assert mask & (1 << 2)
        assert mask & (1 << 1)


class TestLoadAndApplyInvalidCapability:
    def test_unknown_capability_returns_exit_2(self) -> None:
        result = load_and_apply("NOT_A_REAL_CAPABILITY")
        assert result == 2

    def test_empty_capability_returns_exit_2(self) -> None:
        result = load_and_apply("")
        assert result == 2


class TestLoadAndApplySoftDegrade:
    """Landlock absent → exit 0 (soft-degrade). Never aborts."""

    def test_absent_landlock_returns_0(self) -> None:
        with patch("hermes.security.landlock_loader._detect_abi", return_value=None):
            result = load_and_apply("browser")
        assert result == 0

    def test_absent_landlock_terminal_returns_0(self) -> None:
        with patch("hermes.security.landlock_loader._detect_abi", return_value=None):
            result = load_and_apply("terminal")
        assert result == 0


class TestLoadAndApplySuccess:
    def test_returns_0_when_apply_succeeds(self) -> None:
        with patch("hermes.security.landlock_loader._detect_abi", return_value=3), \
             patch("hermes.security.landlock_loader.apply_ruleset") as mock_apply:
            mock_apply.return_value = None
            result = load_and_apply("browser")
        assert result == 0
        mock_apply.assert_called_once()

    def test_returns_3_when_syscall_error(self) -> None:
        from hermes.security.landlock_loader import LandlockSyscallError  # noqa: PLC0415
        with patch("hermes.security.landlock_loader._detect_abi", return_value=3), \
             patch("hermes.security.landlock_loader.apply_ruleset",
                   side_effect=LandlockSyscallError("boom")):
            result = load_and_apply("browser")
        assert result == 3

    def test_unsupported_arch_degrades_not_hard_fail(self) -> None:
        from hermes.security.landlock_loader import UnsupportedArchError  # noqa: PLC0415
        with patch("hermes.security.landlock_loader._detect_abi", return_value=3), \
             patch("hermes.security.landlock_loader.apply_ruleset",
                   side_effect=UnsupportedArchError("mips")):
            result = load_and_apply("browser")
        assert result == 0


class TestMain:
    def test_no_args_returns_2(self) -> None:
        result = main([])
        assert result == 2

    def test_too_many_args_returns_2(self) -> None:
        result = main(["browser", "extra"])
        assert result == 2

    def test_valid_capability_soft_degrade(self) -> None:
        with patch("hermes.security.landlock_loader._detect_abi", return_value=None):
            result = main(["browser"])
        assert result == 0

    def test_case_insensitive(self) -> None:
        with patch("hermes.security.landlock_loader._detect_abi", return_value=None):
            result = main(["BROWSER"])
        assert result == 0


class TestBrowserSessionResolution:
    """Verifica que load_and_apply(browser) resuelve la session desde el entorno."""

    _BUILD_PATH = (
        "hermes.agents_os.infrastructure.landlock_ruleset_builder.build_browser_ruleset"
    )

    def test_env_var_read_for_browser_session(self) -> None:
        captured: dict = {}

        def fake_build(session_name: str) -> object:
            captured["session"] = session_name
            from hermes.agents_os.infrastructure.landlock_ruleset_builder import (  # noqa: PLC0415
                LandlockRulesetBuilder,
            )
            from hermes.agents_os.application.consent_manager import (  # noqa: PLC0415
                Capability as Cap,
            )
            return LandlockRulesetBuilder(session_name=session_name).build(Cap.BROWSER)

        with patch("hermes.security.landlock_loader._detect_abi", return_value=None), \
             patch(self._BUILD_PATH, fake_build), \
             patch.dict("os.environ", {"HERMES_BROWSER_SESSION": "exec-abc123"}):
            load_and_apply("browser")

        assert captured.get("session") == "exec-abc123"

    def test_default_session_when_env_absent(self) -> None:
        captured: dict = {}

        def fake_build(session_name: str) -> object:
            captured["session"] = session_name
            from hermes.agents_os.infrastructure.landlock_ruleset_builder import (  # noqa: PLC0415
                LandlockRulesetBuilder,
            )
            from hermes.agents_os.application.consent_manager import (  # noqa: PLC0415
                Capability as Cap,
            )
            return LandlockRulesetBuilder(session_name=session_name).build(Cap.BROWSER)

        import os  # noqa: PLC0415
        env = {k: v for k, v in os.environ.items() if k != "HERMES_BROWSER_SESSION"}
        with patch("hermes.security.landlock_loader._detect_abi", return_value=None), \
             patch(self._BUILD_PATH, fake_build), \
             patch.dict("os.environ", env, clear=True):
            load_and_apply("browser")

        assert captured.get("session") == "default"
