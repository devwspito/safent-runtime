"""hermes.shell — bounded context de la Hermes Shell.

Capas DDD:
  - domain/        — VOs + entidades + design tokens
  - application/   — use cases + puertos
  - infrastructure/ — adapters (DBus, SQLite, IPC)

NOTA: la `presentation/` GTK4 se retiró (UI nativa no shipeada — PyGObject no está
en la imagen y ningún entrypoint la lanzaba). El domain/application/infrastructure
de este paquete lo REUSA el TUI vivo (hermes.tui) vía dbus_fast_runtime_client.
La UI oficial es la React web app (hermes.shell_server) + el compositor QML (hermes.lumen).
"""
