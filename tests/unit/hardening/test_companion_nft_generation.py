"""companion_nft — nftables ruleset generation for companion egress (024, item 3).

Anti-pivot property (INV-3, SC-2): exactly one `ip daddr` + `tcp dport`
accept per companion — never the subnet, the companion's own database, or
the bridge gateway. Golden-text assertions on the exact generated rule
lines (the smoke test greps this same shape on the live ruleset).
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from hermes.shell_server.companion_nft import (
    MCP_HOST_VETH,
    MCP_NETNS_SOURCE_IP,
    render_companion_forward_rules,
    render_companion_nat_rules,
    render_companion_output_rules,
)
from hermes.shell_server.companions import CompanionEndpoint

pytestmark = pytest.mark.unit


def _endpoint(
    slug: str = "safent-ads", ip: str = "10.201.0.10", port: int = 8443
) -> CompanionEndpoint:
    return CompanionEndpoint(
        slug=slug,
        url=f"https://{slug}.safent.internal:{port}/mcp",
        host=f"{slug}.safent.internal",
        ip=ip,
        port=port,
        ca_path="/etc/hermes/companions/ads-ca.crt",
        ca_fingerprint="sha256:" + "a" * 64,
        bearer_ref="file:/etc/hermes/companions/ads.bearer",
    )


class TestRenderCompanionForwardRules:
    def test_empty_list_yields_empty_string(self) -> None:
        assert render_companion_forward_rules([]) == ""

    def test_exactly_one_accept_rule_for_one_companion(self) -> None:
        text = render_companion_forward_rules([_endpoint()])
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert lines[0] == (
            f'iifname "{MCP_HOST_VETH}" ip saddr {MCP_NETNS_SOURCE_IP} '
            "ip daddr 10.201.0.10 tcp dport 8443 accept "
            'comment "companion safent-ads"'
        )

    def test_never_opens_the_subnet_only_the_single_ip(self) -> None:
        text = render_companion_forward_rules([_endpoint()])
        assert "10.201.0.0/24" not in text
        assert "/2" not in text  # no CIDR of any width sneaks in

    def test_never_opens_the_companion_database_port(self) -> None:
        text = render_companion_forward_rules([_endpoint()])
        assert "5432" not in text
        assert "10.201.0.11" not in text

    def test_source_is_pinned_to_the_mcp_netns_ip(self) -> None:
        text = render_companion_forward_rules([_endpoint()])
        assert f"ip saddr {MCP_NETNS_SOURCE_IP}" in text
        assert MCP_NETNS_SOURCE_IP == "10.200.1.2"

    def test_multiple_companions_sorted_by_slug_deterministic_output(self) -> None:
        text = render_companion_forward_rules(
            [_endpoint(slug="safent-zzz", ip="10.201.0.20"), _endpoint(slug="safent-ads")]
        )
        first, second = [ln for ln in text.splitlines() if ln.strip()]
        assert "safent-ads" in first
        assert "safent-zzz" in second


class TestRenderCompanionOutputRules:
    def test_empty_list_yields_empty_string(self) -> None:
        assert render_companion_output_rules([]) == ""

    def test_output_rule_has_no_source_match_it_is_scoped_by_the_netns_itself(self) -> None:
        text = render_companion_output_rules([_endpoint()])
        assert text.strip() == (
            'ip daddr 10.201.0.10 tcp dport 8443 accept comment "companion safent-ads egress"'
        )


class TestRenderCompanionNatRules:
    def test_empty_list_yields_empty_string(self) -> None:
        assert render_companion_nat_rules([]) == ""

    def test_masquerades_only_the_one_companion_destination(self) -> None:
        text = render_companion_nat_rules([_endpoint()])
        assert text.strip() == (
            f'iifname "{MCP_HOST_VETH}" ip saddr {MCP_NETNS_SOURCE_IP} '
            "ip daddr 10.201.0.10 masquerade "
            'comment "companion safent-ads nat"'
        )


@pytest.mark.parametrize("render", [render_companion_forward_rules, render_companion_nat_rules])
class TestIngressPin:
    def test_legitimate_mcp_veth_still_matches(self, render) -> None:
        assert _matches_generated_predicates(render([_endpoint()]), iifname="veth-hmcp-host")

    @pytest.mark.parametrize("wrong_ingress", ["eth0", "veth-hbr-host", "lo", "veth-foreign"])
    def test_spoofed_mcp_source_on_another_interface_is_rejected(
        self, render, wrong_ingress: str
    ) -> None:
        # Source/destination/port are otherwise the exact authorized tuple.
        assert not _matches_generated_predicates(render([_endpoint()]), iifname=wrong_ingress)

    @pytest.mark.parametrize("wrong_source", ["10.200.0.2", "10.200.1.1", "10.201.0.11"])
    def test_correct_interface_does_not_replace_source_validation(
        self, render, wrong_source: str
    ) -> None:
        assert not _matches_generated_predicates(
            render([_endpoint()]), iifname="veth-hmcp-host", source=wrong_source
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "ops/agents-os-edition/systemd/hermes-browser-netns.service",
        "ops/container/dropins/hermes-browser-netns.service.d/container.conf",
    ],
)
def test_ingress_pin_matches_the_actual_host_side_of_both_topologies(relative_path: str) -> None:
    topology = (Path(__file__).resolve().parents[3] / relative_path).read_text()
    assert MCP_HOST_VETH == "veth-hmcp-host"
    assert f"ip link add {MCP_HOST_VETH} type veth peer name veth-hmcp-ns" in topology
    assert f"ip addr add 10.200.1.1/30 dev {MCP_HOST_VETH}" in topology


def _matches_generated_predicates(
    rule: str, *, iifname: str, source: str = MCP_NETNS_SOURCE_IP
) -> bool:
    """Evaluate only the generator's fixed predicate grammar, not an nft emulator."""
    packet = {
        "iifname": iifname,
        "ip saddr": source,
        "ip daddr": "10.201.0.10",
        "tcp dport": "8443",
    }
    tokens = shlex.split(rule)
    index = 0
    conditions = []
    while tokens[index] not in {"accept", "masquerade"}:
        if tokens[index] == "iifname":
            conditions.append(packet["iifname"] == tokens[index + 1])
            index += 2
        else:
            field = " ".join(tokens[index : index + 2])
            assert field in {"ip saddr", "ip daddr", "tcp dport"}
            conditions.append(packet[field] == tokens[index + 2])
            index += 3
    return all(conditions)


class TestGeneratedRulesAreValidNftSyntaxShape:
    """Cheap syntactic sanity — no `table`/`chain` wrapper (these are
    `include`d INSIDE an existing chain body, see module docstring)."""

    @pytest.mark.parametrize(
        "render",
        [render_companion_forward_rules, render_companion_output_rules, render_companion_nat_rules],
    )
    def test_no_table_or_chain_keywords(self, render) -> None:
        text = render([_endpoint()])
        assert "table " not in text
        assert "chain " not in text
