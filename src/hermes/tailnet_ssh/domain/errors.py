"""Named exceptions for tailnet_ssh. Never a bare Exception/ValueError at a
trust boundary — every failure mode here is specific and fail-closed."""

from __future__ import annotations


class TailnetSshError(Exception):
    """Base of every tailnet_ssh domain/application error."""


class InvalidTailnetHostError(TailnetSshError):
    """`host` is malformed, empty, or an IP literal — never accepted."""


class UnknownTailnetHostError(TailnetSshError):
    """`host` does not match the MagicDNS suffix nor a listed peer name."""


class TailnetDirectoryUnavailableError(TailnetSshError):
    """`/run/hermes/tailscale/status.json` is missing, unreadable, or malformed."""


class RemoteCommandRejectedError(TailnetSshError):
    """`command` is empty, oversized, or otherwise not a valid single argv token."""


class RemotePathRejectedError(TailnetSshError):
    """A `tailnet_file_get`/`tailnet_file_put` remote path fails validation."""


class RemoteCommandTimeoutError(TailnetSshError):
    """The ssh subprocess exceeded `timeout_s`."""


class SshExecutionError(TailnetSshError):
    """The `ssh` binary is missing, unspawnable, or exited abnormally before running."""
