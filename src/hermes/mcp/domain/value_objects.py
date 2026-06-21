"""mcp/domain/value_objects — typed value objects for the MCP bounded context.

Domain layer: pure Python + stdlib only.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping
from uuid import UUID


# ---------------------------------------------------------------------------
# TrustLevel
# ---------------------------------------------------------------------------


class TrustLevel(StrEnum):
    """Trust posture of an MCP server.

    BUILTIN: first-party, baked into the image; identical to the native registry posture.
    USER_TRUSTED: community / publisher-signed; DEFAULT_DENY + approved domains.
    USER_ADDED: any unverified spec; DEFAULT_DENY with EMPTY egress allowlist,
                HITL forced on every tool-call.
    """

    BUILTIN = "builtin"
    USER_TRUSTED = "user_trusted"
    USER_ADDED = "user_added"


# ---------------------------------------------------------------------------
# ServerSlug
# ---------------------------------------------------------------------------

_SLUG_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


@dataclass(frozen=True)
class ServerSlug:
    """URL-safe identifier for an MCP server. Pattern: [a-z0-9][a-z0-9-]*[a-z0-9].

    Invariant: must match _SLUG_PATTERN (single character slugs are allowed).
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or not _SLUG_PATTERN.match(self.value):
            raise ValueError(
                f"ServerSlug must match [a-z0-9]([a-z0-9-]*[a-z0-9])?, got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# McpServerId
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpServerId:
    """UUID-based identity for a running MCP server connection."""

    value: UUID

    @classmethod
    def generate(cls) -> McpServerId:
        return cls(value=uuid.uuid4())

    @classmethod
    def from_str(cls, raw: str) -> McpServerId:
        return cls(value=UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transport:
    """Describes how to launch / connect to an MCP server.

    P1: STDIO only. argv[0] is the executable; argv[1:] are arguments.
    The command/url NEVER comes from LLM input — only from the catalog.

    env: BYOK environment variables forwarded to the MCP subprocess.
    Keys must be pre-validated by the caller (_validate_mcp_env in the
    infrastructure layer) — Transport itself is a pure value object with
    no validation beyond basic structural integrity.
    The mapping is stored as an immutable MappingProxyType so callers
    cannot mutate it after construction (frozen dataclass only prevents
    re-assignment of the field, not mutation of a mutable container).
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("Transport.argv must have at least one element (the command)")
        # Coerce any plain dict supplied by callers to an immutable proxy so the
        # frozen invariant holds in practice, not just on the reference.
        if not isinstance(self.env, MappingProxyType):
            object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    @classmethod
    def stdio(cls, argv: list[str], *, env: dict[str, str] | None = None) -> Transport:
        return cls(argv=tuple(argv), env=MappingProxyType(env or {}))


# ---------------------------------------------------------------------------
# ServerHealth
# ---------------------------------------------------------------------------


class ServerHealth(StrEnum):
    """Runtime health of a connected MCP server."""

    CONNECTING = "connecting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"    # responding but slow / partial errors
    FAILED = "failed"        # terminal — restart_count exhausted
    DISCONNECTED = "disconnected"
