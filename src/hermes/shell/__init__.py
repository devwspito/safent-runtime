"""hermes.shell — bounded context de la Hermes Shell (UI nativa del SO).

Capas DDD:
  - domain/        — VOs + entidades + design tokens (zero GTK)
  - application/   — use cases + puertos (zero GTK)
  - infrastructure/ — adapters (DBus, SQLite, IPC)
  - presentation/  — GTK4 + libadwaita widgets

La app entry point es `hermes.shell.presentation.gtk4.app:main`.
"""
