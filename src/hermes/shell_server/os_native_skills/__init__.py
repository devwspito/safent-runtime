"""OS-native skills: capacidades NATIVAS del SO expuestas como tools de Hermes.

Tesis Agents OS: operamos a nivel SO, así que las capacidades (capturar
pantalla, grabar pantalla+audio, screenshot) son habilidades NATIVAS que el
runtime Hermes invoca por tool-calling, con acceso directo al hardware vía las
D-Bus/PipeWire del SO. No se reimplementan en Python — se apoyan en las
herramientas nativas (mutter ScreenCast + GStreamer).

Mismo catálogo reutilizado por:
  - el shell GTK4 (live view, training),
  - el tool_host del runtime Hermes (el agente las llama como tools).

Piezas:
    catalog.py    — OsNativeSkill + el catálogo (screenshot, screen_record).
    executors.py  — ejecutan cada skill sobre la capa nativa (en sesión).
    tool_specs.py — bridge a ToolSpec del runtime (Hermes registra estas tools).
"""

from .catalog import (
    OS_NATIVE_SKILLS,
    OsNativeSkill,
    SkillRisk,
)

__all__ = ["OS_NATIVE_SKILLS", "OsNativeSkill", "SkillRisk"]
