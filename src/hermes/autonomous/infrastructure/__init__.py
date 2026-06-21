"""Lazy-import guard del bounded context.

Constitución v1.0.0 — Restricciones Técnicas:
- Importar este paquete sin las deps de ``[workspace]`` instaladas NO debe
  fallar. Solo los puntos de uso reales (adapters) fallan con error claro
  si la dep falta. Por eso los adapters dentro de ``infrastructure/`` usan
  ``importlib.import_module(...)`` o ``try/except ImportError`` lazy.
"""

from __future__ import annotations
