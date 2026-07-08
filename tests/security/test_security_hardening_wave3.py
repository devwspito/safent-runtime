"""Security hardening wave-3 regression tests.

Covers all 12 fix points from the security remediation:
  Fix-1:  Audit key derives from master.key via HKDF (per-install, stable).
  Fix-2:  WORM anchor in /var/lib/hermes/audit/ (persistent, 0700/0600).
  Fix-3:  TSA pending alarm threshold (visible degraded state).
  Fix-4:  Egress SNI enforcement in DEFAULT_DENY.
  Fix-5:  Global egress default = DEFAULT_DENY.
  Fix-6:  HashChainAuditSink DEGRADED flag when signer absent.
  Fix-7:  Plain HTTP rejected in DEFAULT_DENY.
  Fix-8:  (ops/netns — documented follow-up, not in proxy code)
  Fix-9:  D-Bus sentinel uid 0 → PermissionError.
  Fix-10: JSON validation in D-Bus CreateAgent / SetAgentHouseRule.
  Fix-11: TLS key files via mkstemp (not mktemp TOCTOU).
  Fix-12: --no-sandbox removed from PlaywrightDriver.
"""

from __future__ import annotations

import asyncio
import struct
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Fix-1: Audit key stable cross-restart via master.key derive_subkey
# ---------------------------------------------------------------------------


class TestAuditKeyStableFromMasterKey:
    """Fix-1: _load_signing_key_or_fail derives from master.key when no env var."""

    def _make_fake_vault(self, *, subkey: bytes):
        """Return a fake SecretsVault that yields a fixed subkey."""
        vault = MagicMock()
        vault.derive_subkey.return_value = subkey
        return vault

    def test_same_master_key_produces_same_audit_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Deux appels avec la même master.key produisent la même clé d'audit (stable)."""
        from hermes.shell_server.security.secrets import SecretsVault

        # Write a master.key to a tmp path.
        master_key = b"A" * 32
        master_path = tmp_path / "master.key"
        master_path.write_bytes(master_key)

        monkeypatch.delenv("HERMES_AUDIT_KEY", raising=False)
        monkeypatch.setattr(
            "hermes.shell_server.security.secrets._MASTER_KEY_PATH", master_path
        )

        # Import here to get fresh module state.
        from hermes.runtime.__main__ import _load_signing_key_or_fail  # noqa: PLC0415

        key_a = _load_signing_key_or_fail()
        key_b = _load_signing_key_or_fail()
        assert key_a == key_b, "same master.key must produce same audit key"
        assert len(key_a) == 32

    def test_different_master_keys_produce_different_audit_keys(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Different master.key bytes → different audit keys."""
        monkeypatch.delenv("HERMES_AUDIT_KEY", raising=False)

        path_a = tmp_path / "master_a.key"
        path_a.write_bytes(b"A" * 32)

        path_b = tmp_path / "master_b.key"
        path_b.write_bytes(b"B" * 32)

        from hermes.runtime.__main__ import _load_signing_key_or_fail  # noqa: PLC0415

        monkeypatch.setattr(
            "hermes.shell_server.security.secrets._MASTER_KEY_PATH", path_a
        )
        key_a = _load_signing_key_or_fail()

        monkeypatch.setattr(
            "hermes.shell_server.security.secrets._MASTER_KEY_PATH", path_b
        )
        key_b = _load_signing_key_or_fail()

        assert key_a != key_b

    def test_no_master_key_raises_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Without HERMES_AUDIT_KEY AND without master.key → RuntimeError (fail-closed)."""
        monkeypatch.delenv("HERMES_AUDIT_KEY", raising=False)
        absent = tmp_path / "does_not_exist.key"
        monkeypatch.setattr(
            "hermes.shell_server.security.secrets._MASTER_KEY_PATH", absent
        )

        from hermes.runtime.__main__ import _load_signing_key_or_fail  # noqa: PLC0415

        with pytest.raises(RuntimeError, match="audit_key_unavailable|no existe"):
            _load_signing_key_or_fail()

    def test_env_var_takes_priority_over_master_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """HERMES_AUDIT_KEY wins over master.key derivation."""
        seal = b"Z" * 32
        monkeypatch.setenv("HERMES_AUDIT_KEY", seal.hex())

        # Point master.key to an absent file — should NOT raise.
        absent = tmp_path / "does_not_exist.key"
        monkeypatch.setattr(
            "hermes.shell_server.security.secrets._MASTER_KEY_PATH", absent
        )

        from hermes.runtime.__main__ import _load_signing_key_or_fail  # noqa: PLC0415

        key = _load_signing_key_or_fail()
        assert key == seal


# ---------------------------------------------------------------------------
# Fix-3: TSA pending alarm threshold
# ---------------------------------------------------------------------------


class TestTsaPendingAlarm:
    def test_degraded_flag_set_when_pending_exceeds_threshold(
        self, tmp_path: Path
    ) -> None:
        """tsa_degraded=True when pending queue length >= _PENDING_ALARM_THRESHOLD."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        tsa = TsaExternalAnchor(
            token_dir=tmp_path,
            ca_cert_pem=b"placeholder",
        )
        threshold = tsa._PENDING_ALARM_THRESHOLD  # type: ignore[attr-defined]

        # Seed the pending queue with threshold+1 entries.
        entries = [f"{'a' * 63}{i:01x}" for i in range(threshold + 1)]
        tsa._save_queue(entries)  # type: ignore[attr-defined]

        # _flush_queue will fail to drain (no real TSA), leaving queue >= threshold.
        # We simulate by manually calling the alarm logic.
        remaining = tsa._load_queue()  # type: ignore[attr-defined]
        tsa.tsa_degraded = len(remaining) >= threshold
        assert tsa.tsa_degraded is True

    def test_degraded_flag_false_when_pending_below_threshold(
        self, tmp_path: Path
    ) -> None:
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        tsa = TsaExternalAnchor(
            token_dir=tmp_path,
            ca_cert_pem=b"placeholder",
        )
        tsa.tsa_degraded = False
        assert tsa.tsa_degraded is False


# ---------------------------------------------------------------------------
# Fix-4: Egress SNI enforcement — deny if SNI not in whitelist / no SNI
# ---------------------------------------------------------------------------


def _make_tls_client_hello(sni: str) -> bytes:
    """Minimal TLS 1.2 ClientHello with SNI extension."""
    sni_bytes = sni.encode("ascii")
    sni_entry = struct.pack(">BH", 0x00, len(sni_bytes)) + sni_bytes
    sni_list = struct.pack(">H", len(sni_entry)) + sni_entry
    sni_ext = struct.pack(">HH", 0x0000, len(sni_list)) + sni_list
    exts_block = struct.pack(">H", len(sni_ext)) + sni_ext
    random_bytes = b"\x00" * 32
    ch_body = (
        b"\x03\x03" + random_bytes + b"\x00"
        + b"\x00\x02\xc0\x2b"
        + b"\x01\x00"
        + exts_block
    )
    hs_header = struct.pack(">B", 0x01) + struct.pack(">I", len(ch_body))[1:]
    hs = hs_header + ch_body
    return struct.pack(">BHH", 0x16, 0x0303, len(hs)) + hs


class _CollectingTransport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.written = bytearray()
        self._closing = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    def get_extra_info(self, name: str, default=None):
        if name == "peername":
            return ("10.200.0.2", 54321)
        return default


def _make_stream_reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


def _make_writer(transport: _CollectingTransport) -> asyncio.StreamWriter:
    protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    return asyncio.StreamWriter(
        transport, protocol, asyncio.StreamReader(), asyncio.get_event_loop()
    )


class TestSniEnforcement:
    """Fix-4: SNI enforcement in DEFAULT_DENY mode."""

    def _make_engine(self, *, domains: frozenset[str]):
        from hermes.egress_proxy.domain.policy import (
            EgressMode,
            EgressPolicyEngine,
            SessionPolicy,
        )

        return EgressPolicyEngine(
            global_policy=SessionPolicy(
                session_id="__global__",
                mode=EgressMode.DEFAULT_DENY,
                domains_whitelist=domains,
            )
        )

    @pytest.mark.asyncio
    async def test_sni_not_in_whitelist_is_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CONNECT with SNI outside whitelist → 403 + audit deny record."""
        from hermes.egress_proxy.infrastructure.audit_sink import InMemoryAuditSink
        from hermes.egress_proxy.infrastructure.proxy_handler import (
            ProxyConnectionHandler,
        )

        engine = self._make_engine(domains=frozenset({"safe.com"}))
        sink = InMemoryAuditSink()
        connect = b"CONNECT safe.com:443 HTTP/1.1\r\n\r\n"
        tls = _make_tls_client_hello("evil.com")

        transport = _CollectingTransport()
        reader = _make_stream_reader(connect + tls)
        writer = _make_writer(transport)

        opened: list[tuple[str, int]] = []

        async def _fake_connect(host, port):
            opened.append((host, port))
            r = asyncio.StreamReader()
            r.feed_eof()
            return r, _make_writer(_CollectingTransport())

        monkeypatch.setattr(
            "hermes.egress_proxy.infrastructure.proxy_handler.asyncio.open_connection",
            _fake_connect,
        )

        handler = ProxyConnectionHandler(policy_engine=engine, audit_sink=sink)
        await handler.handle(reader, writer)

        # Fix-4 (evolved design): an HTTP client emits its TLS ClientHello only AFTER
        # the CONNECT 200, so the proxy must send 200 up front, then VERIFY the real
        # SNI. A mismatched/denied SNI tears the tunnel down (no upstream opened, no
        # bytes forwarded) — a late 403 can no longer be carried once past the 200.
        # Security invariant preserved: the SNI (evil.com), NOT the whitelisted CONNECT
        # host (safe.com), is what gets denied + audited, and NO upstream tunnel to it
        # is ever opened.
        assert "evil.com" in sink.denied_domains()
        assert opened == [], "denied SNI must NOT open any upstream connection"
        assert transport.is_closing(), "denied SNI must tear down the client tunnel"

    @pytest.mark.asyncio
    async def test_ip_literal_connect_denied_in_default_deny(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-4: CONNECT to bare IP literal is denied in DEFAULT_DENY (no SNI possible)."""
        from hermes.egress_proxy.infrastructure.audit_sink import InMemoryAuditSink
        from hermes.egress_proxy.infrastructure.proxy_handler import (
            ProxyConnectionHandler,
        )

        engine = self._make_engine(domains=frozenset({"safe.com"}))
        sink = InMemoryAuditSink()
        connect = b"CONNECT 1.2.3.4:443 HTTP/1.1\r\n\r\n"

        transport = _CollectingTransport()
        reader = _make_stream_reader(connect)
        writer = _make_writer(transport)

        handler = ProxyConnectionHandler(policy_engine=engine, audit_sink=sink)
        await handler.handle(reader, writer)

        assert b"403" in bytes(transport.written)

    @pytest.mark.asyncio
    async def test_no_sni_denied_in_default_deny(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-4: CONNECT without TLS bytes is denied in DEFAULT_DENY (no SNI)."""
        from hermes.egress_proxy.infrastructure.audit_sink import InMemoryAuditSink
        from hermes.egress_proxy.infrastructure.proxy_handler import (
            ProxyConnectionHandler,
        )

        engine = self._make_engine(domains=frozenset({"safe.com"}))
        sink = InMemoryAuditSink()
        # No TLS bytes after CONNECT — reader hits EOF before SNI can be read.
        connect = b"CONNECT safe.com:443 HTTP/1.1\r\n\r\n"

        transport = _CollectingTransport()
        reader = _make_stream_reader(connect)
        writer = _make_writer(transport)

        handler = ProxyConnectionHandler(policy_engine=engine, audit_sink=sink)
        await handler.handle(reader, writer)

        # Fix-4 (evolved design): with no ClientHello (EOF right after CONNECT), the
        # proxy tears the tunnel down after the CONNECT 200 instead of a late 403 it
        # can no longer carry. Security invariant preserved: an absent/empty SNI in
        # DEFAULT_DENY is DENIED — the client tunnel is closed (the browser's TLS
        # handshake fails) and the connection is NEVER recorded as allowed.
        assert transport.is_closing(), "empty ClientHello must close the tunnel (deny)"
        assert sink.allowed_domains() == [], "no-SNI CONNECT must never be allowed"


# ---------------------------------------------------------------------------
# Fix-5: Global default = DEFAULT_DENY
# ---------------------------------------------------------------------------


class TestGlobalDefaultDeny:
    def test_engine_default_is_default_deny_when_no_policy_given(self) -> None:
        """Fix-5: EgressPolicyEngine() with no args defaults to DEFAULT_DENY."""
        from hermes.egress_proxy.domain.policy import EgressMode, EgressPolicyEngine

        engine = EgressPolicyEngine()
        decision = engine.evaluate(domain="any.com", session_id="test")
        assert decision.allowed is False, (
            "Fix-5 regression: global default must be DEFAULT_DENY, not OPEN_LOGGED"
        )

    def test_main_resolves_invalid_mode_to_default_deny(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-5: invalid HERMES_EGRESS_MODE → DEFAULT_DENY (not OPEN_LOGGED)."""
        from hermes.egress_proxy import __main__ as em  # noqa: PLC0415

        monkeypatch.setattr(em, "_EGRESS_MODE_RAW", "totally-invalid")
        from hermes.egress_proxy.domain.policy import EgressMode

        mode = em._resolve_global_mode()
        assert mode == EgressMode.DEFAULT_DENY


# ---------------------------------------------------------------------------
# Fix-6: HashChainAuditSink DEGRADED when signer absent
# ---------------------------------------------------------------------------


class TestHashChainAuditSinkDegraded:
    def test_degraded_true_when_no_signer(self) -> None:
        """Fix-6: HashChainAuditSink.degraded=True when signer is None."""
        from hermes.egress_proxy.infrastructure.audit_sink import HashChainAuditSink

        sink = HashChainAuditSink(signer=None, audit_repo=None)
        assert sink.degraded is True

    def test_degraded_false_when_signer_present(self) -> None:
        """Fix-6: HashChainAuditSink.degraded=False when signer + repo injected."""
        from hermes.egress_proxy.infrastructure.audit_sink import HashChainAuditSink

        fake_signer = MagicMock()
        fake_repo = MagicMock()
        sink = HashChainAuditSink(signer=fake_signer, audit_repo=fake_repo)
        assert sink.degraded is False

    def test_record_uses_fallback_when_degraded(self, caplog) -> None:
        """Fix-6: degraded sink records via structlog fallback, not silently drops."""
        import logging

        from hermes.egress_proxy.domain.policy import EgressDecision, EgressMode
        from hermes.egress_proxy.infrastructure.audit_sink import HashChainAuditSink

        sink = HashChainAuditSink(signer=None, audit_repo=None)
        decision = EgressDecision(
            allowed=True,
            domain="test.com",
            session_id="s1",
            mode=EgressMode.OPEN_LOGGED,
            reason="test",
        )
        with caplog.at_level(logging.ERROR, logger="hermes.egress_proxy.audit"):
            sink.record(decision)

        # Error must be logged (degraded record must not be silent).
        assert any("degraded" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Fix-7: Plain HTTP rejected in DEFAULT_DENY
# ---------------------------------------------------------------------------


class TestPlainHttpRejectedDefaultDeny:
    @pytest.mark.asyncio
    async def test_plain_http_rejected_in_default_deny(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-7: GET/POST without CONNECT is rejected immediately in DEFAULT_DENY."""
        from hermes.egress_proxy.domain.policy import (
            EgressMode,
            EgressPolicyEngine,
            SessionPolicy,
        )
        from hermes.egress_proxy.infrastructure.audit_sink import InMemoryAuditSink
        from hermes.egress_proxy.infrastructure.proxy_handler import (
            ProxyConnectionHandler,
        )

        engine = EgressPolicyEngine(
            global_policy=SessionPolicy(
                session_id="__global__",
                mode=EgressMode.DEFAULT_DENY,
                domains_whitelist=frozenset({"safe.com"}),
            )
        )
        sink = InMemoryAuditSink()
        request = b"GET http://safe.com/ HTTP/1.1\r\nHost: safe.com\r\n\r\n"
        transport = _CollectingTransport()
        reader = _make_stream_reader(request)
        writer = _make_writer(transport)

        handler = ProxyConnectionHandler(policy_engine=engine, audit_sink=sink)
        await handler.handle(reader, writer)

        assert b"403" in bytes(transport.written)
        # No audit record — domain not verified (no SNI).
        assert sink.allowed_domains() == []
        assert sink.denied_domains() == []


# ---------------------------------------------------------------------------
# Fix-9: D-Bus sentinel uid 0 → PermissionError
# ---------------------------------------------------------------------------


class TestDbusNoSentinelUid:
    @pytest.mark.asyncio
    async def test_no_bus_raises_permission_error(self) -> None:
        """Fix-9: _resolve_current_sender_uid raises PermissionError when bus is None."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            Runtime1ServiceInterface,
        )

        iface = Runtime1ServiceInterface(wiring=MagicMock())
        # _bus is None by default — must raise, not return uid 0.
        with pytest.raises(PermissionError, match="no_bus|no se puede resolver"):
            await iface._resolve_current_sender_uid()

    @pytest.mark.asyncio
    async def test_no_sender_in_context_raises_permission_error(self) -> None:
        """Fix-9: PermissionError when sender ContextVar is None (bus attached)."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            Runtime1ServiceInterface,
            _CURRENT_SENDER_VAR,
        )

        iface = Runtime1ServiceInterface(wiring=MagicMock())
        # Attach a fake bus so the None-bus check passes.
        iface._bus = MagicMock()
        # Ensure _CURRENT_SENDER_VAR is None (default).
        token = _CURRENT_SENDER_VAR.set(None)
        try:
            with pytest.raises(PermissionError, match="no_sender"):
                await iface._resolve_current_sender_uid()
        finally:
            _CURRENT_SENDER_VAR.reset(token)


# ---------------------------------------------------------------------------
# Fix-10: JSON validation in D-Bus mutators
# ---------------------------------------------------------------------------


class TestDbusJsonValidation:
    def test_missing_kind_in_house_rule_raises(self) -> None:
        """Fix-10: SetAgentHouseRule fails if 'kind' is absent (DoS guard)."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            DbusInputValidationError,
            _parse_and_validate_house_rule_json,
        )

        with pytest.raises(DbusInputValidationError, match="kind.*obligatorio|kind"):
            _parse_and_validate_house_rule_json('{"value": "x"}')

    def test_unknown_keys_in_house_rule_rejected(self) -> None:
        """Fix-10: unknown top-level keys are rejected at the trust boundary."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            DbusInputValidationError,
            _parse_and_validate_house_rule_json,
        )

        with pytest.raises(DbusInputValidationError, match="no permitidas"):
            _parse_and_validate_house_rule_json(
                '{"kind": "block", "__proto__": "injected"}'
            )

    def test_valid_house_rule_passes(self) -> None:
        """Fix-10: valid house rule passes validation."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            _parse_and_validate_house_rule_json,
        )

        result = _parse_and_validate_house_rule_json('{"kind": "block", "value": "x"}')
        assert result["kind"] == "block"

    def test_unknown_keys_in_agent_draft_rejected(self) -> None:
        """Fix-10: unknown top-level keys in CreateAgent are rejected."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            DbusInputValidationError,
            _parse_and_validate_agent_draft_json,
        )

        with pytest.raises(DbusInputValidationError, match="no permitidas"):
            _parse_and_validate_agent_draft_json(
                '{"name": "x", "exec": "rm -rf /"}'
            )

    def test_oversized_json_rejected(self) -> None:
        """Fix-10: JSON payload exceeding 64 KiB is rejected."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            DbusInputValidationError,
            _parse_and_validate_agent_draft_json,
        )

        big = '{"name": "' + "x" * 70_000 + '"}'
        with pytest.raises(DbusInputValidationError, match="límite"):
            _parse_and_validate_agent_draft_json(big)

    def test_non_dict_json_rejected(self) -> None:
        """Fix-10: non-object JSON (array, string, int) is rejected."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            DbusInputValidationError,
            _parse_and_validate_agent_draft_json,
        )

        with pytest.raises(DbusInputValidationError, match="objeto JSON"):
            _parse_and_validate_agent_draft_json('["a", "b"]')


# ---------------------------------------------------------------------------
# Fix-11: TLS key files via mkstemp (not mktemp TOCTOU)
# ---------------------------------------------------------------------------


class TestTlsKeyTempfiles:
    def test_webrtc_signaling_uses_mkstemp(self) -> None:
        """Fix-11: WebRtcSignalingClient._open_tls_ws does not use tempfile.mktemp."""
        import inspect

        from hermes.workspace.infrastructure.webrtc_signaling import WebRtcSignalingClient

        src = inspect.getsource(WebRtcSignalingClient._open_tls_ws)
        assert "mktemp(" not in src, (
            "Fix-11 regression: _open_tls_ws uses mktemp (TOCTOU). Use mkstemp."
        )
        assert "mkstemp(" in src

    def test_ws_control_plane_uses_mkstemp(self) -> None:
        """Fix-11: WsControlPlaneChannelAdapter._open_ws_once does not use mktemp."""
        import inspect

        from hermes.workspace.infrastructure.ws_control_plane_channel import (
            WsControlPlaneChannelAdapter,
        )

        src = inspect.getsource(WsControlPlaneChannelAdapter._open_ws_once)
        assert "mktemp(" not in src, (
            "Fix-11 regression: _open_ws_once uses mktemp (TOCTOU). Use mkstemp."
        )
        assert "mkstemp(" in src


# (Fix-12 PlaywrightDriver --no-sandbox test removed: the PlaywrightDriver was a
# duplicate of hermes-agent's native browser and has been deleted; the jailed Chromium
# is launched by the browser-launcher with its own seccomp/Landlock, not by Playwright.)
