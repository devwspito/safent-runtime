"""ActiveProviderService — único punto de resolución del provider activo.

Todos los consumidores que necesitan saber qué modelo usar para inferencia
pasan por aquí. Internamente delega en `provider_config_source.resolve_model_config`
(cascade nativo → SQL → env). El resultado se cachea 30 segundos para evitar
lecturas repetidas de disco/DB en hot-paths (run_cycle, OsNativeDispatcher).

Uso típico (DI via constructor):

    svc = ActiveProviderService(db_path=_DB_PATH)
    cfg = svc.resolve()          # ModelConfig | None
    meta = svc.get_active_metadata()   # dict con alias, kind, has_key, native

force_refresh() invalida la caché inmediatamente — útil tras configure_native_provider
o set_active_provider para que el siguiente run_cycle vea el cambio sin esperar 30s.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes.runtime.model_config import ModelConfig

# Import at module level so tests can patch 'hermes.runtime.active_provider.resolve_model_config'.
# The lazy local import inside resolve() is replaced by this module-level name.
try:
    from hermes.runtime.provider_config_source import (  # noqa: PLC0415
        resolve_model_config,
    )
except Exception:  # noqa: BLE001  (optional dep in minimal environments)
    resolve_model_config = None  # type: ignore[assignment]

logger = logging.getLogger("hermes.runtime.active_provider")

_CACHE_TTL_SECONDS = 30


class ActiveProviderService:
    """Resuelve y cachea el ModelConfig del provider activo."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._cached: ModelConfig | None = None
        self._cached_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self) -> ModelConfig | None:
        """ModelConfig del provider activo, con caché LRU de 30 segundos.

        None si no hay provider configurado ni HERMES_MODEL en env.
        Fail-soft: cualquier error interno devuelve None (loguea warning).
        """
        now = time.monotonic()
        if self._cached is not None and (now - self._cached_at) < _CACHE_TTL_SECONDS:
            return self._cached
        try:
            if resolve_model_config is None:
                return None
            result = resolve_model_config(self._db_path)
        except Exception:  # noqa: BLE001
            logger.warning("hermes.active_provider.resolve_failed", exc_info=True)
            return None
        self._cached = result
        self._cached_at = now
        logger.debug(
            "hermes.active_provider.resolved model=%s",
            result.model if result is not None else None,
        )
        return result

    def force_refresh(self) -> None:
        """Invalida la caché — el siguiente resolve() lee de disco."""
        self._cached = None
        self._cached_at = 0.0

    def get_provider_id(self) -> str | None:
        """Slug del provider activo ('openai-api', 'nous', …), o None."""
        cfg = self.resolve()
        if cfg is None:
            return None
        # litellm model string format: "<provider>/<model>" or just "<model>".
        parts = cfg.model.split("/", 1)
        return parts[0] if len(parts) == 2 else None

    def get_model(self) -> str | None:
        """Nombre del modelo activo (sin prefijo de provider), o None."""
        cfg = self.resolve()
        if cfg is None:
            return None
        parts = cfg.model.split("/", 1)
        return parts[1] if len(parts) == 2 else cfg.model

    def get_active_metadata(self) -> dict:
        """Metadata del provider activo para exponer por D-Bus / UI.

        Retorna:
            {provider_id, model, has_key, base_url, native}
        o {} si no hay provider configurado.
        """
        cfg = self.resolve()
        if cfg is None:
            return {}
        pid = self.get_provider_id()
        model = self.get_model()
        # Detect if this came from the native path (hermes_cli config.yaml):
        # the native loader always builds "pid/model" strings; SQL path also uses
        # litellm_model_string which does the same. We distinguish by checking
        # config.yaml directly (lightweight, no extra DB hit).
        native = _is_native_active()
        return {
            "provider_id": pid or "",
            "model": model or cfg.model,
            "has_key": cfg.api_key is not None,
            "base_url": cfg.base_url or "",
            "native": native,
        }


def _is_native_active() -> bool:
    """True si config.yaml tiene un model.provider no-auto."""
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        m = (load_config() or {}).get("model") or {}
        pid = (m.get("provider") or "").strip()
        return bool(pid) and pid != "auto"
    except Exception:  # noqa: BLE001
        return False
