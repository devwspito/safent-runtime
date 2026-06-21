"""InstallTarget — value object describing what is about to be installed."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class InstallTarget:
    """Immutable description of an artifact to be scanned before install.

    kind:          "mcp_server" | "skill" | "composio_app" | "package"
    identifier:    Human-readable name / slug (e.g. github.com/user/repo)
    source_url:    Origin URL used for provenance check (may be empty string)
    version:       Semver string or commit SHA (empty = unknown)
    sha256:        Hex SHA-256 of the artifact (empty = unknown → shorter cache TTL)
    manifest_json: Raw JSON string of the MCP/skill manifest (empty if not applicable)
    """

    kind: str
    identifier: str
    source_url: str = ""
    version: str = ""
    sha256: str = ""
    manifest_json: str = ""
    argv: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("InstallTarget.kind must not be empty")
        if not self.identifier:
            raise ValueError("InstallTarget.identifier must not be empty")

    @property
    def cache_key(self) -> str:
        """Cache lookup key — includes sha256 when available."""
        if self.sha256:
            return f"{self.kind}:{self.sha256}"
        return f"{self.kind}:{self.identifier}"

    @property
    def has_sha256(self) -> bool:
        return bool(self.sha256)
