"""Change a connected Codex model through Hermes, without replacing OAuth.

The native picker adds offline/synthetic choices. This owner-facing selector
requires the live account catalog instead; unavailable discovery is not access.
"""

from __future__ import annotations

import re

from hermes.runtime.managed_llm import local_configuration_write

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_MODEL_LENGTH = 128
_MAX_MODELS = 1_000
_CATALOG_URL = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"


class SelectionUnavailable(Exception):
    """Safe code only; upstream credentials and errors never cross this boundary."""


def _live_models() -> tuple[list[str], str]:
    import httpx  # noqa: PLC0415
    from hermes_cli.auth import resolve_codex_runtime_credentials  # noqa: PLC0415
    from hermes_cli.codex_models import _extract_chatgpt_account_id, _ranked_slugs  # noqa: PLC0415

    credentials = resolve_codex_runtime_credentials(refresh_if_expiring=True)
    token = credentials.get("api_key")
    account = _extract_chatgpt_account_id(token) if isinstance(token, str) else None
    if not account:
        raise SelectionUnavailable("models_unavailable")
    # Fixed native origin, no redirects, no configurable endpoint or browser token.
    response = httpx.get(
        _CATALOG_URL,
        headers={"Authorization": f"Bearer {token}", "ChatGPT-Account-Id": account},
        timeout=10,
        follow_redirects=False,
    )
    response.raise_for_status()
    data = response.json()
    entries = data.get("models") if isinstance(data, dict) else None
    if not isinstance(entries, list) or len(entries) > _MAX_MODELS:
        raise SelectionUnavailable("models_unavailable")
    models = [model for model in _ranked_slugs(entries) if _MODEL_ID.fullmatch(model)]
    if not models:
        raise SelectionUnavailable("models_unavailable")
    return models, account


def _same_oauth_account(account: str) -> bool:
    from hermes_cli.auth import _read_codex_tokens  # noqa: PLC0415
    from hermes_cli.codex_models import _extract_chatgpt_account_id  # noqa: PLC0415

    token = (_read_codex_tokens().get("tokens") or {}).get("access_token")
    return isinstance(token, str) and _extract_chatgpt_account_id(token) == account


def _read_selection(wiring, provider_id: str) -> dict:
    from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
        _read_native_active,
    )

    if provider_id != "openai-codex":
        raise SelectionUnavailable("unsupported_provider")
    # Check governance before accessing local OAuth, without holding the policy
    # lock during network discovery. The write path rechecks at commit time.
    with local_configuration_write(wiring._local_llm_db_path()):
        active = _read_native_active()
        if active.get("provider_id") != provider_id:
            raise SelectionUnavailable("selection_changed")
        return active


def list_models(wiring, *, provider_id: str, sender_uid: int) -> dict:
    wiring._authorize_and_resolve(sender_uid, operation="list_native_provider_models")
    try:
        active = _read_selection(wiring, provider_id)
        models, _account = _live_models()
        if _read_selection(wiring, provider_id) != active:
            raise SelectionUnavailable("selection_changed")
        return {
            "provider_id": provider_id,
            "active_model": active.get("default_model", ""),
            "models": models,
        }
    except PermissionError:
        return {"code": "managed_by_enterprise"}
    except SelectionUnavailable as exc:
        return {"code": str(exc)}
    except Exception:  # noqa: BLE001 — no upstream message or credentials in responses/logs
        return {"code": "models_unavailable"}


def select_model(
    wiring, *, provider_id: str, model: str, expected_model: str, sender_uid: int
) -> dict:
    wiring._authorize_and_resolve(sender_uid, operation="set_native_provider_model")
    if (
        not isinstance(model, str)
        or not _MODEL_ID.fullmatch(model)
        or not isinstance(expected_model, str)
        or len(expected_model) > _MAX_MODEL_LENGTH
    ):
        return {"code": "invalid_model"}
    try:
        active = _read_selection(wiring, provider_id)
        if active.get("default_model", "") != expected_model:
            raise SelectionUnavailable("selection_changed")
        models, account = _live_models()
        if model not in models:
            raise SelectionUnavailable("model_not_available")
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            _clear_engine_runtime_cache,
            _read_native_active,
            _remember_native_provider_model,
            _write_hermes_model_config,
        )

        with local_configuration_write(wiring._local_llm_db_path()):
            if _read_native_active() != active or not _same_oauth_account(account):
                raise SelectionUnavailable("selection_changed")
            # Keep provider, endpoint, OAuth store and unrelated model settings.
            base_url = active.get("base_url", "")
            _write_hermes_model_config(provider_id, model, base_url)
            written = _read_native_active()
            if written.get("provider_id") != provider_id or written.get("default_model") != model:
                raise SelectionUnavailable("models_unavailable")
            _remember_native_provider_model(provider_id, model, base_url)
            if wiring._active_provider_svc is not None:
                wiring._active_provider_svc.force_refresh()
            _clear_engine_runtime_cache()
        return {"provider_id": provider_id, "active_model": model}
    except PermissionError:
        return {"code": "managed_by_enterprise"}
    except SelectionUnavailable as exc:
        return {"code": str(exc)}
    except Exception:  # noqa: BLE001 — no upstream message or credentials in responses/logs
        return {"code": "models_unavailable"}
