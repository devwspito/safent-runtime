"""Bounded context LOCAL ``control_plane`` — feature 006 / PIEZA 5 (FR-018).

Canal LOCAL daemon ↔ shell/CLI sobre D-Bus (org.hermes.Runtime1) + socket
Unix AF_UNIX /run/hermes/tasks.sock para el stream de chunks.

DISTINTO del canal REMOTO de spec 002
(``src/hermes/workspace/ws_control_plane_channel.py``):
  - spec 002 = mTLS JSON-RPC VM → CP externo (red).
  - este BC   = IPC local dentro del mismo host (D-Bus + socket Unix).

No mezclar ni reutilizar: contratos incompatibles, trust-models diferentes.

Capas:
  domain/       → ports (Protocols) + value objects. Cero framework.
  application/  → servicio de orquestación chat→enqueue.
  infrastructure/ → adapter D-Bus dbus-fast + socket Unix stream.
"""
