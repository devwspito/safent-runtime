"""LayoutPrefs — persistencia de las preferencias de disposición de la shell.

Almacena en JSON (mismo directorio que hermes-theme.json) las preferencias
de layout editables desde Ajustes → Disposición:

  - show_sidebar   (bool, default True)
  - show_workspace (bool, default True)
  - density        ("comfortable" | "compact", default "comfortable")

Los valores se aplican a la HermesShellWindow pero NUNCA se guardan en el
dominio ni en el servidor — son puramente presentacionales.

El acceso es síncrono (archivo pequeño); se lee una vez al abrir Ajustes
y se escribe en cada cambio. Cualquier error de E/S produce un log de aviso
y continúa con los defaults para no bloquear la UI.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Density = Literal["comfortable", "compact"]

_PERSIST_FILENAME = "hermes-layout.json"
_DEFAULT_SHOW_SIDEBAR = True
_DEFAULT_SHOW_WORKSPACE = True
_DEFAULT_DENSITY: Density = "comfortable"
_DEFAULT_BANNER_DISMISSED = False


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / "hermes-shell"
    d.mkdir(parents=True, exist_ok=True)
    return d


class LayoutPrefs:
    """Lectura/escritura de preferencias de disposición.

    Diseñado para instanciarse una vez (en SettingsWindow) y pasarse
    como referencia al panel de Disposición. No se necesita singleton
    porque solo se usa desde el hilo GTK.
    """

    def __init__(self) -> None:
        self.show_sidebar: bool = _DEFAULT_SHOW_SIDEBAR
        self.show_workspace: bool = _DEFAULT_SHOW_WORKSPACE
        self.density: Density = _DEFAULT_DENSITY
        # True cuando el usuario descartó explícitamente el banner de "sin modelo"
        # con "Más tarde". Se persiste para no acosar en cada arranque.
        self.banner_dismissed: bool = _DEFAULT_BANNER_DISMISSED
        self._load()

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persiste el estado actual en disco. Silencia errores de E/S."""
        path = _config_dir() / _PERSIST_FILENAME
        try:
            path.write_text(
                json.dumps({
                    "show_sidebar": self.show_sidebar,
                    "show_workspace": self.show_workspace,
                    "density": self.density,
                    "banner_dismissed": self.banner_dismissed,
                }),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("no se pudo persistir layout prefs: %s", exc)

    def reset_to_defaults(self) -> None:
        """Restaura los defaults y persiste."""
        self.show_sidebar = _DEFAULT_SHOW_SIDEBAR
        self.show_workspace = _DEFAULT_SHOW_WORKSPACE
        self.density = _DEFAULT_DENSITY
        # No resetear banner_dismissed — sería molesto forzar el banner
        # al restaurar densidad/paneles.
        self.save()

    # ------------------------------------------------------------------
    # Implementación interna
    # ------------------------------------------------------------------

    def _load(self) -> None:
        path = _config_dir() / _PERSIST_FILENAME
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.show_sidebar = bool(data.get("show_sidebar", _DEFAULT_SHOW_SIDEBAR))
            self.show_workspace = bool(data.get("show_workspace", _DEFAULT_SHOW_WORKSPACE))
            raw_density = data.get("density", _DEFAULT_DENSITY)
            if raw_density in ("comfortable", "compact"):
                self.density = raw_density  # type: ignore[assignment]
            self.banner_dismissed = bool(
                data.get("banner_dismissed", _DEFAULT_BANNER_DISMISSED)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("layout prefs corruptos, usando defaults: %s", exc)
