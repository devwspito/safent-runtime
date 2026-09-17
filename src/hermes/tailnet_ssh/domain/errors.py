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


class AllowlistStoreUnavailableError(TailnetSshError):
    """`/var/lib/hermes/tailscale/ssh-allowlist.json` exists but is
    unreadable or corrupt (as opposed to simply absent, which is a
    legitimate empty state). CWE-636 fix: a read-modify-write method (allow/
    allow_governed/revoke) NEVER treats this as "empty" and rewrites the
    file — that would silently discard every entry that survived the
    corruption. grant_for NEVER treats this as "no ceiling" either — a
    capability-ceiling decision that cannot read its own data source fails
    CLOSED (deny), never open."""


class InvalidRemoteIdentityError(TailnetSshError):
    """A CLOUD-managed host's declared remote identity is malformed —
    fail-closed: the call is refused rather than passed to `ssh` verbatim."""


class SshCapabilityDeniedError(TailnetSshError):
    """A CLOUD-managed host's governed-SSH grant (spec 002 US3) does not
    include the capability the use case is about to perform (exec /
    file_read / file_write). The grant's `capabilities` set is a CEILING —
    the use case never widens it, and raises this BEFORE the ssh subprocess
    is ever spawned. Never raised for a local/unmanaged host — those keep
    today's unrestricted behaviour (no ceiling exists to deny against)."""
