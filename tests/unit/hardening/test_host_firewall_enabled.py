"""The host INPUT default-deny must actually LOAD, and not lock out the UI (024).

`inet hermes_host` was absent on every boot: hermes-host-firewall.service ships
in the image with a correct `[Install] WantedBy=multi-user.target`, but NOTHING
ever ran `systemctl enable` on it, so no .wants symlink existed and the unit
never started. Consequences: the container ran with NO inbound default-deny, and
hermes-browser-netns.service's own `mcp netns -> egress proxy` accept was skipped
every boot ("tabla inet hermes_host ausente").

Enabling it surfaced two more things this pins:

1. The control plane is published by the CONTAINER ENGINE (-p 127.0.0.1:N:7517).
   The engine DNATs on the HOST side, so inside this netns the packet arrives
   from the bridge gateway with no `ct status dnat` to match — the default-deny
   killed it (verified: :7517 went 307 -> hard timeout). The gateway is derived
   at boot and only inside a container.

2. `nft add rule` APPENDS, and `input` ends with an explicit terminal drop — so
   every rule any unit added to `input` was dead code. They now target a regular
   `input_dynamic` chain that `input` jumps to BEFORE that drop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_NFT = (_ROOT / "ops/agents-os-edition/netns/host-input.nft").read_text(encoding="utf-8")
_CONTAINERFILE = (_ROOT / "ops/container/Containerfile").read_text(encoding="utf-8")
_FIREWALL_UNIT = (
    _ROOT / "ops/agents-os-edition/systemd/hermes-host-firewall.service"
).read_text(encoding="utf-8")
_NETNS_UNIT = (
    _ROOT / "ops/agents-os-edition/systemd/hermes-browser-netns.service"
).read_text(encoding="utf-8")
_ENGINE_PORT = (
    _ROOT / "ops/agents-os-edition/scripts/hermes-host-firewall-engine-port"
).read_text(encoding="utf-8")
# The CONTAINER deployment does NOT run the base unit's ExecStart list: this
# drop-in resets it (`ExecStart=`) and re-declares every step in nsenter form.
# Anything fixed in the base unit has to be fixed here too or it is a no-op
# in the only deployment this feature ships in.
_NETNS_DROPIN = (
    _ROOT / "ops/container/dropins/hermes-browser-netns.service.d/container.conf"
).read_text(encoding="utf-8")
_PROXY_ACCEPT = (
    _ROOT / "ops/agents-os-edition/scripts/hermes-mcp-proxy-accept"
).read_text(encoding="utf-8")


class TestUnitIsActuallyEnabled:
    def test_containerfile_enables_the_host_firewall(self) -> None:
        enable_block = _CONTAINERFILE.split("RUN systemctl enable \\", 1)[1].split("\n\n", 1)[0]
        assert "hermes-host-firewall.service" in enable_block, (
            "the unit ships but is never enabled -> inet hermes_host never loads"
        )

    def test_unit_still_declares_its_install_target(self) -> None:
        assert "WantedBy=multi-user.target" in _FIREWALL_UNIT.split("[Install]", 1)[1]


class TestDynamicAcceptsLandBeforeTheTerminalDrop:
    def test_ruleset_declares_the_runtime_filled_chain(self) -> None:
        assert "chain input_dynamic {" in _NFT

    def test_input_jumps_to_it_before_the_terminal_drop(self) -> None:
        assert "jump input_dynamic" in _NFT
        drop = _NFT.index('log prefix "hermes_host INPUT DROP: "')
        assert _NFT.index("jump input_dynamic") < drop

    def test_proxy_accept_script_targets_the_dynamic_chain(self) -> None:
        assert "CHAIN=input_dynamic" in _PROXY_ACCEPT

    def test_both_the_base_unit_and_the_container_dropin_call_the_same_script(self) -> None:
        """The drop-in RESETS ExecStart (`ExecStart=`), so it is the only copy
        that runs in the container deployment: an inline command fixed in the
        base unit alone is a no-op there. One script, called by both."""
        for text, where in ((_NETNS_UNIT, "base unit"), (_NETNS_DROPIN, "container drop-in")):
            assert "/usr/libexec/hermes/hermes-mcp-proxy-accept" in text, where
            assert "nft add rule inet hermes_host" not in text, (
                f"{where} still inlines the rule — two copies is how this broke"
            )

    def test_script_puts_the_quotes_inside_the_comment_text(self) -> None:
        """nft joins its argv with spaces and RE-LEXES, so a shell-quoted
        multi-word comment still arrives as bare tokens -> syntax error -> the
        oneshot exits 1 -> hermes-runtime's confinement gate aborts the daemon."""
        assert 'comment "\\"$COMMENT\\""' in _PROXY_ACCEPT

    def test_script_is_idempotent_and_removable(self) -> None:
        assert 'grep -q "$COMMENT"' in _PROXY_ACCEPT
        assert '"--remove"' in _PROXY_ACCEPT
        assert "ExecStop=-/usr/libexec/hermes/hermes-mcp-proxy-accept --remove" in _NETNS_UNIT

    def test_ruleset_accepts_the_gvproxy_subnet_used_by_podman_machine(self) -> None:
        """macOS reaches the published port through gvproxy, whose guest-side
        network is 192.168.127.0/24. Without this accept the packet arrives and
        the terminal drop eats it: :7517 answers inside the VM and not from the
        host (specs/028-safent-app-nativa/diagnostico-red-macos.md)."""
        assert "192.168.127.0/24 tcp dport 7517 accept" in _NFT
        # the three hypervisor planes are siblings: none may be dropped silently
        for subnet in ("10.0.2.0/24", "192.168.64.0/24", "192.168.127.0/24"):
            assert f"ip saddr {subnet} tcp dport 7517 accept" in _NFT

    def test_engine_port_script_targets_the_dynamic_chain(self) -> None:
        assert "input_dynamic" in _ENGINE_PORT
        assert "nft add rule inet hermes_host input " not in _ENGINE_PORT


class TestPublishedPortAcceptStaysNarrow:
    def test_only_inside_a_container(self) -> None:
        assert "/run/.containerenv" in _ENGINE_PORT
        assert "/.dockerenv" in _ENGINE_PORT

    def test_one_source_address_and_one_port(self) -> None:
        assert 'ip saddr "$gw" tcp dport "$PORT" accept' in _ENGINE_PORT
        assert "PORT=7517" in _ENGINE_PORT
        assert "0.0.0.0/0" not in _ENGINE_PORT

    def test_idempotent_by_comment(self) -> None:
        assert 'grep -q "$COMMENT"' in _ENGINE_PORT

    def test_unit_runs_it_as_a_non_fatal_execstartpost(self) -> None:
        assert (
            "ExecStartPost=-/usr/libexec/hermes/hermes-host-firewall-engine-port"
            in _FIREWALL_UNIT
        )

    def test_scripts_are_baked_into_the_image(self) -> None:
        assert (
            "scripts/hermes-host-firewall-engine-port "
            "/usr/libexec/hermes/hermes-host-firewall-engine-port" in _CONTAINERFILE
        )
        assert (
            "scripts/hermes-mcp-proxy-accept /usr/libexec/hermes/hermes-mcp-proxy-accept"
            in _CONTAINERFILE
        )

    def test_static_hypervisor_accepts_are_untouched(self) -> None:
        assert "ip saddr 10.0.2.0/24 tcp dport 7517 accept" in _NFT
        assert "ip saddr 192.168.64.0/24 tcp dport 7517 accept" in _NFT
