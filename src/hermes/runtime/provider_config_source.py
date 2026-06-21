"""ModelConfig desde el PROVIDER ACTIVO configurado por el usuario.

El usuario elige provider + modelo + API key en el onboarding (setup wizard) o en
Settings (Modelos y proveedores). Eso se persiste en la tabla `providers` de
`/var/lib/hermes/shell-state.db` con la API key cifrada (AES-GCM-256, SecretsVault
sobre `/var/lib/hermes/master.key`). El daemon del agente y el shell-server corren
ambos como `User=hermes` y comparten esa DB y esa master.key, así que el runtime
lee el provider activo y descifra la key DIRECTAMENTE — sin hablar HTTP con la UI
("queremos un SO, no una API"). Sin provider activo → None → el caller hace
fallback a env (CI/headless).

REUSE: SQLiteProviderRepository + SecretsVault + litellm_model_string de
`hermes.shell_server.providers`/`security` (módulos puros, sin FastAPI/GTK). El
import es local para no crear ciclo import-time runtime<->shell_server (convención
existente del repo, p.ej. runtime/__main__.py con os_native_skills).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from hermes.runtime.model_config import ModelConfig

logger = logging.getLogger("hermes.runtime.provider_config")


def load_active_model_config(db_path: Path) -> ModelConfig | None:
    """ModelConfig del provider activo, o None si no hay (o no se puede leer).

    Fail-soft: cualquier error (DB ausente, master.key ausente en CI, schema
    viejo) devuelve None y se loguea — el caller cae a env. NO revienta el daemon
    por un provider mal configurado; el agente queda degradado hasta que haya uno
    válido, que es el estado honesto.
    """
    try:
        from hermes.shell_server.providers.domain import litellm_model_string  # noqa: PLC0415
        from hermes.shell_server.providers.repo import (  # noqa: PLC0415
            SQLiteProviderRepository,
        )
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
    except Exception:  # noqa: BLE001  (paquete shell_server no instalado / opcional)
        logger.debug("hermes.provider_config.shell_server_unavailable")
        return None

    try:
        repo = SQLiteProviderRepository(db_path=db_path, vault=SecretsVault())
        provider = repo.get_active()
    except Exception:  # noqa: BLE001  (master.key ausente, DB sin tabla, etc.)
        logger.warning("hermes.provider_config.load_failed", exc_info=True)
        return None

    if provider is None or not provider.default_model:
        return None

    model = litellm_model_string(provider, provider.default_model)
    api_key: str | None = None
    if provider.has_api_key:
        try:
            api_key = repo.reveal_api_key(provider_id=provider.provider_id)
        except Exception:  # noqa: BLE001  (descifrado falla -> seguimos sin key)
            logger.warning("hermes.provider_config.reveal_failed", exc_info=True)

    logger.info(
        "hermes.provider_config.active",
        extra={"alias": provider.alias, "model": model, "has_key": api_key is not None},
    )
    return ModelConfig.from_provider(model=model, api_key=api_key, base_url=provider.base_url)


def _load_native_model_config() -> ModelConfig | None:
    """Provider NATIVO (hermes_cli): ~/.hermes/.env + config.yaml. Es lo que
    escribe `configure_native_provider` (D-Bus) cuando el usuario configura un
    provider desde el Catálogo nativo de la UI. Read-only desde aquí; el motor
    pasa por aquí cada ciclo. None si no hay model.provider en config.yaml.
    """
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        cfg = load_config() or {}
    except Exception:  # noqa: BLE001
        return None
    m = cfg.get("model") or {}
    pid = (m.get("provider") or "").strip()
    if not pid or pid == "auto":
        return None
    model = (m.get("default") or m.get("model") or "").strip()
    if not model:
        return None
    # litellm-style model string para que el motor sepa de qué provider hablar.
    model_string = f"{pid}/{model}"
    base_url = (m.get("base_url") or "").strip() or None
    api_key: str | None = None
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY  # noqa: PLC0415
        pc = PROVIDER_REGISTRY.get(pid)
        if pc is not None:
            for var in (getattr(pc, "api_key_env_vars", ()) or ()):
                v = os.environ.get(var)
                if v:
                    api_key = v
                    break
    except Exception:  # noqa: BLE001
        pass
    # Fallback: leer ~/.hermes/.env si la env-var no está cargada en el proceso.
    if api_key is None:
        try:
            from pathlib import Path as _Path  # noqa: PLC0415
            env_path = _Path(
                os.environ.get("HERMES_HOME") or (_Path.home() / ".hermes")
            ) / ".env"
            if env_path.exists():
                for ln in env_path.read_text(encoding="utf-8").splitlines():
                    if "=" not in ln or ln.lstrip().startswith("#"):
                        continue
                    k, _, v = ln.partition("=")
                    if k.strip() in (
                        getattr(PROVIDER_REGISTRY.get(pid), "api_key_env_vars", ()) or ()
                    ):
                        api_key = v.strip()
                        break
        except Exception:  # noqa: BLE001
            pass
    logger.info(
        "hermes.provider_config.native_active",
        extra={"provider": pid, "model": model_string, "has_key": api_key is not None},
    )
    return ModelConfig.from_provider(
        model=model_string, api_key=api_key, base_url=base_url
    )


def resolve_model_config(db_path: Path) -> ModelConfig | None:
    """Cascade: Path NATIVO (config.yaml) → Path SQL (DB) → env. None si ninguno.

    Esta es la fuente que el engine consulta POR CICLO: cambiar el provider en el
    onboarding o en Settings surte efecto en la siguiente tarea, sin reiniciar el
    daemon. El path NATIVO va PRIMERO porque es el que escribe la UI nueva
    (Catálogo nativo / `configure_native_provider` por D-Bus). El path SQL queda
    como fallback histórico (provider creado vía "+ Añadir" en versiones viejas).
    """
    config = _load_native_model_config()
    if config is not None:
        return config
    config = load_active_model_config(db_path)
    if config is not None:
        return config
    from hermes.runtime.model_config import HermesModelNotConfiguredError  # noqa: PLC0415

    try:
        return ModelConfig.from_env()
    except HermesModelNotConfiguredError:
        return None
