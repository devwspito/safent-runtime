"""Characterization tests for ActiveProviderService.

Pins the caching behaviour, force_refresh, and delegation to
resolve_model_config without testing provider_config_source internals
(those live in test_provider_config_source.py).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes.runtime.active_provider import ActiveProviderService
from hermes.runtime.model_config import ModelConfig

pytestmark = pytest.mark.unit


def _cfg(model: str = "openai/gpt-4o", api_key: str | None = "sk-x") -> ModelConfig:
    return ModelConfig(model=model, api_key=api_key)


# ---------------------------------------------------------------------------
# resolve() — delegation and caching
# ---------------------------------------------------------------------------

def test_resolve_delegates_to_resolve_model_config(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    expected = _cfg()
    with patch(
        "hermes.runtime.active_provider.resolve_model_config", return_value=expected
    ) as mock_resolve:
        result = svc.resolve()
    assert result is expected
    mock_resolve.assert_called_once_with(tmp_path / "state.db")


def test_resolve_caches_second_call(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config", return_value=_cfg()
    ) as mock_resolve:
        svc.resolve()
        svc.resolve()  # second call — should NOT hit the real function again
    assert mock_resolve.call_count == 1


def test_resolve_returns_none_when_no_provider(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config", return_value=None
    ):
        assert svc.resolve() is None


def test_resolve_fail_soft_on_exception(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config",
        side_effect=RuntimeError("boom"),
    ):
        result = svc.resolve()
    assert result is None


# ---------------------------------------------------------------------------
# force_refresh() — cache invalidation
# ---------------------------------------------------------------------------

def test_force_refresh_triggers_re_read(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    cfg1 = _cfg("openai/gpt-4o")
    cfg2 = _cfg("anthropic/claude-3-5-sonnet-20241022")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config",
        side_effect=[cfg1, cfg2],
    ) as mock_resolve:
        assert svc.resolve() is cfg1
        svc.force_refresh()
        assert svc.resolve() is cfg2
    assert mock_resolve.call_count == 2


# ---------------------------------------------------------------------------
# get_provider_id() / get_model()
# ---------------------------------------------------------------------------

def test_get_provider_id_parses_litellm_format(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config",
        return_value=_cfg("anthropic/claude-3-5-haiku-20241022"),
    ):
        assert svc.get_provider_id() == "anthropic"


def test_get_model_parses_litellm_format(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config",
        return_value=_cfg("openai/gpt-4o-mini"),
    ):
        assert svc.get_model() == "gpt-4o-mini"


def test_get_provider_id_returns_none_when_no_config(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config", return_value=None
    ):
        assert svc.get_provider_id() is None
        assert svc.get_model() is None


# ---------------------------------------------------------------------------
# get_active_metadata()
# ---------------------------------------------------------------------------

def test_get_active_metadata_shape(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config",
        return_value=_cfg("openai/gpt-4o", api_key="sk-123"),
    ), patch("hermes.runtime.active_provider._is_native_active", return_value=False):
        meta = svc.get_active_metadata()
    assert meta["provider_id"] == "openai"
    assert meta["model"] == "gpt-4o"
    assert meta["has_key"] is True
    assert meta["native"] is False


def test_get_active_metadata_empty_when_no_config(tmp_path: Path) -> None:
    svc = ActiveProviderService(db_path=tmp_path / "state.db")
    with patch(
        "hermes.runtime.active_provider.resolve_model_config", return_value=None
    ):
        assert svc.get_active_metadata() == {}
