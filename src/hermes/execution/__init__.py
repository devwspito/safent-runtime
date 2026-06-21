"""Bounded context ``execution`` — registro de contextos de ejecución aislados.

Feature 006 / PIEZA 4.

Generaliza el InputOwnershipLedger de spec 004 (teaching) a un registro
fail-closed con UN dueño por superficie de input.

Capas:
  domain/         → ports + value objects (InputSurfaceKey, ExecutionContextId, …).
  application/    → ExecutionContextRegistry (in-memory + RLock).
  infrastructure/ → SqliteExecutionContextStore (write-through + reconcile).
"""
