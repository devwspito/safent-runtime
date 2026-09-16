"""tailnet_ssh — governed SSH-over-tailnet (spec 022 v2).

Lets the agent run a non-interactive command on an owner-approved tailnet
host, through the SOCKS5 proxy the ops lane's `tailscaled` exposes on
127.0.0.1:1056 (host netns only — the agent netns cannot reach it, see
`specs/022-tailnet-connectivity/ssh-v2.md`).

Layers (DDD, one-way dependency domain -> application -> infrastructure):
  domain/         pure value objects + errors, zero I/O.
  application/     TailnetSshUseCase / TailnetFileGetUseCase / TailnetFilePutUseCase
                   orchestrate ports; no framework, no I/O of their own.
  infrastructure/  JSON status/allowlist readers, the subprocess `ssh` executor,
                   the SOCKS5 ProxyCommand connector.

Governance (per-host HITL block-and-resume + audit) lives in
`hermes.runtime.security_hook` (`_resolve_tailnet_ssh_consent`), which is the
ONLY caller allowed to grant a host — this module's use cases trust that the
pre-tool-call hook already gated the call (same trust model as
`DelegationSurfaceAdapter`: "this adapter performs ZERO additional
authorization of its own").
"""

from __future__ import annotations
