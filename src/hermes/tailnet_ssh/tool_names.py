"""Single source of truth for tailnet_ssh's tool names.

Zero imports (no I/O, no other hermes module) — deliberately, so this can be
imported from ANYWHERE (security_hook, tool_delicacy, tool_policy,
tool_sensitivity) without ever creating a cycle back into
`hermes.tailnet_ssh`.

Hand-curated (never a keyword/substring scan — see
feedback_no_deterministic_routing): the exact set of tool names this
capability owns. Extend here; every governance layer derives from this set.
"""

from __future__ import annotations

TAILNET_SSH_TOOL_NAMES: frozenset[str] = frozenset({
    "tailnet_ssh",
    "tailnet_file_get",
    "tailnet_file_put",
})
