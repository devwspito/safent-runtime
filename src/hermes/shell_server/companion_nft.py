"""companion_nft — pure nftables ruleset generation for companion egress (024, item 3).

Domain layer: pure Python + stdlib only (no filesystem, no subprocess) — the
root oneshot script (`ops/agents-os-edition/scripts/hermes-companion-nft`) is
the only I/O boundary, calling these functions then writing their output to
`/run/hermes/nft/` (tmpfs, regenerated every boot from the validated
`companions.json`, never hand-edited).

Anti-pivot property (INV-3): a companion opens EXACTLY one `ip daddr` + one
`tcp dport` — never the subnet, never the companion's own database/broker,
never the bridge gateway. Forward/NAT also require the MCP host-side veth:
enabling forwarding in the container must not admit a spoofed MCP source
arriving on its external interface or the browser's veth.
Three fragments, one per existing chain the
generator ADDS TO (never a new table):

  companion-ads-fwd.nft  -> `include`d into mcp-host.nft's `forward` chain,
                             BEFORE the RFC1918 drop (10.201.0.0/24 sits
                             inside 10.0.0.0/8 — a drop is TERMINAL in
                             nftables, so ordering is load-bearing, not
                             cosmetic; see mcp-host.nft's own comment).
  companion-ads-out.nft  -> `include`d into mcp-ns.nft's `output` chain
                             (INSIDE the hermes-mcp netns) — belt-and-
                             suspenders, mirrors the existing ->proxy rule.
  companion-ads-nat.nft  -> `include`d into mcp-host.nft's `postrouting`
                             chain — masquerades ONLY traffic to that ONE
                             companion destination (the podman/docker bridge
                             the companion sits on has no route back into
                             the nested hermes-mcp netns without it; the
                             existing ->WAN masquerade is unaffected, it
                             matches on a different source).

Every fragment is generated from the SAME validated `CompanionEndpoint` list
(hermes.shell_server.companions) — never from unvalidated input — so a
malformed/tampered companions.json (rejected by that loader) simply yields
no rule for that slug, not a wrong one.
"""

from __future__ import annotations

from hermes.shell_server.companions import CompanionEndpoint

# veth-hmcp-ns — the hermes-mcp netns's OWN source IP (mcp-host.nft topology
# comment: "hermes-mcp netns: veth-hmcp-ns 10.200.1.2/30"). A product
# constant, like the companion subnet/port (spec.md §7) — never derived.
MCP_NETNS_SOURCE_IP = "10.200.1.2"
MCP_HOST_VETH = "veth-hmcp-host"

FORWARD_FRAGMENT_FILENAME = "companion-ads-fwd.nft"
OUTPUT_FRAGMENT_FILENAME = "companion-ads-out.nft"
NAT_FRAGMENT_FILENAME = "companion-ads-nat.nft"


def render_companion_forward_rules(endpoints: list[CompanionEndpoint]) -> str:
    """mcp-host.nft `forward` chain fragment: one `accept` per companion.

    Empty input -> empty string (the caller skips writing the file; an
    absent file under a glob `include` is not an error — see mcp-host.nft).
    """
    return _render_rules(endpoints, _forward_rule)


def render_companion_output_rules(endpoints: list[CompanionEndpoint]) -> str:
    """mcp-ns.nft `output` chain fragment (inside the hermes-mcp netns)."""
    return _render_rules(endpoints, _output_rule)


def render_companion_nat_rules(endpoints: list[CompanionEndpoint]) -> str:
    """mcp-host.nft `postrouting` chain fragment — masquerade to the companion only."""
    return _render_rules(endpoints, _nat_rule)


def _render_rules(endpoints: list[CompanionEndpoint], rule_of) -> str:
    if not endpoints:
        return ""
    ordered = sorted(endpoints, key=lambda ep: ep.slug)
    return "".join(f"{rule_of(ep)}\n" for ep in ordered)


def _forward_rule(ep: CompanionEndpoint) -> str:
    return (
        f'iifname "{MCP_HOST_VETH}" ip saddr {MCP_NETNS_SOURCE_IP} '
        f"ip daddr {ep.ip} tcp dport {ep.port} accept "
        f'comment "companion {ep.slug}"'
    )


def _output_rule(ep: CompanionEndpoint) -> str:
    return f'ip daddr {ep.ip} tcp dport {ep.port} accept comment "companion {ep.slug} egress"'


def _nat_rule(ep: CompanionEndpoint) -> str:
    return (
        f'iifname "{MCP_HOST_VETH}" ip saddr {MCP_NETNS_SOURCE_IP} '
        f"ip daddr {ep.ip} masquerade "
        f'comment "companion {ep.slug} nat"'
    )
