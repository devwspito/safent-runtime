"""Domain errors for provider resolution.

Named exception types for every failure mode — never raise generic Exception.
"""

from __future__ import annotations


class UnmappedProviderError(LookupError):
    """A ProviderKind has no canonical entry in the catalog.

    Should never happen in production — the catalog covers all ProviderKind
    members. If it does, it is a programming error in the catalog definition.
    """


class ProviderResolutionError(RuntimeError):
    """The resolver could not produce a valid ResolvedModel.

    Wraps vault decryption failures, missing active provider, or any
    infrastructure error that prevents resolution.
    """
