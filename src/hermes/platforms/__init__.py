"""Platforms bounded context — PlatformModel lifecycle and governance.

Hexagonal layout:
  domain/       — pure domain: aggregates, entities, value objects, events, ports
  application/  — use cases (stubs for US1-2, fully wired for T001-T026 scope)
  infrastructure/ — SQLite persistence + signer adapter
"""
