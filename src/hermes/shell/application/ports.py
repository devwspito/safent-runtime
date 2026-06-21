"""Puertos que la presentación consume.

F2 cableará los adapters reales (DBus, REST). Hoy solo la interfaz.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AgentResponseChunk:
    """Fragmento de respuesta del agente (streaming)."""

    delta: str
    is_final: bool
    tool_call: dict | None = None


@runtime_checkable
class AgentRuntimePort(Protocol):
    """Puerto contra hermes-runtime.service via DBus org.hermes.Runtime1."""

    async def send_message(
        self, *, text: str
    ) -> AsyncIterator[AgentResponseChunk]: ...

    async def get_status(self) -> dict: ...

    async def request_pause(self, *, reason: str) -> None: ...

    async def request_resume(self) -> None: ...


@runtime_checkable
class ConsentPromptPort(Protocol):
    """Puerto contra ConsentManager para mostrar prompts en UI."""

    async def request_consent(
        self, *, capability: str, scope: str, requestor: str
    ) -> bool: ...


@runtime_checkable
class AuditFeedPort(Protocol):
    """Puerto contra AuditTailWriter para feed de audit entries."""

    async def stream_entries(self) -> AsyncIterator[dict]: ...
