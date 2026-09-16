"""_validate_mcp_env (MCP-05, spec 025 matriz) — validated pattern + deny-list
replacing the fixed BYOK env-key allowlist.

Root cause (live-verified): the MCP form's OWN placeholder (McpView.tsx:1148,
`mcp.env.label`: "BRAVE_API_KEY=br-xxx") named a key that was never in the
old `_MCP_BYOK_ENV_KEYS` frozenset — anyone who followed the UI's own example
got a raw 400 `clave de env no permitida` back. Covers:
  - any plausible env-var name (`^[A-Z][A-Z0-9_]{2,63}$`) is now accepted,
    including the exact placeholder example;
  - the deny-list (PATH, LD_*, PYTHON*, HERMES_*, NODE_OPTIONS, SSL_CERT_*,
    http(s)_proxy variants) still rejects genuinely dangerous names;
  - HOME is the one deliberate exception — accepted here (R16: a MANAGED_
    REMOTE/OAuth-bridge draft's McpSpec.env legitimately carries it, and
    rejecting it here would hard-fail add_mcp_server before scan/prefetch/
    connect) even though the launcher never honours it as an override (see
    test_r16_mcp_bridge_handshake.py::TestLauncherHomeOwnership);
  - no error message ever echoes a VALUE, only key names;
  - OD_DAEMON_URL's URL validation is unchanged.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    _MCP_ENV_DENY_EXACT_CORE,
    _MCP_ENV_DENY_PREFIXES_CORE,
    _validate_mcp_env,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LAUNCHER_SCRIPT = _REPO_ROOT / "ops/agents-os-edition/scripts/hermes-mcp-launcher"

# Security review 2026-09-10 (H-1): every one of these must be denied by
# BOTH gates. Union of the coordinator's explicit list and the review's own
# reimplementation-and-enumerate finding (~50 names that passed before this
# fix); values are the exact/prefix-expanded names actually exercised.
#
# Deliberately EXCLUDED here (each has its own dedicated test elsewhere,
# not a gap): NODE_EXTRA_CA_CERTS and ADS_BEARER are denied at the DAEMON
# (TestAdsCompanionSecretsAreFillOnly) but the LAUNCHER must still forward
# them once the daemon has injected the trusted value (companion-bridge
# carve-outs — TestLauncherOnlyCarveOuts below); TMPDIR/XDG_DATA_HOME are
# denied at the DAEMON but allowed at the LAUNCHER via _INTERNAL_ENV_KEYS
# (daemon-SET, trusted, never reachable from caller input because the
# daemon gate already denies a caller-supplied one upstream).
_H1_DANGEROUS_NAMES = [
    # code-execution / interpreter / shell hijack
    "NODE_PATH", "ELECTRON_RUN_AS_NODE", "BASH_ENV", "ENV", "SHELLOPTS", "PS4", "IFS",
    "PERL5OPT", "PERL5LIB", "RUBYOPT", "RUBYLIB", "GODEBUG", "GOFLAGS", "CLASSPATH",
    "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "NIX_LD", "MALLOC_CONF", "GCONV_PATH",
    "LOCPATH", "UV_OFFLINE",
    # TLS-trust / MITM
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # proxy family
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY",
    # tmp/terminal identity
    "TERMINFO", "TERMINFO_DIRS", "TEMP", "TMP",
    # prefix family — one representative name per prefix, matching the
    # coordinator's own exploit examples
    "GIT_SSH_COMMAND", "GIT_SSL_CAINFO", "GIT_EXTERNAL_DIFF", "GIT_CONFIG_COUNT",
    "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_PYTHON",
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
    "NPM_CONFIG_REGISTRY", "NPM_CONFIG_PREFIX",
    "XDG_RUNTIME_DIR",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "JAVA_HOME", "JDK_HOME", "_JAVA_OPTIONS",
    "SAFENT_ADS_IMAGE", "SAFENT_STATE",
    "DOTNET_STARTUP_HOOKS",
]


def _load_launcher_module():
    loader = importlib.machinery.SourceFileLoader(
        "hermes_mcp_launcher_h1_test", str(_LAUNCHER_SCRIPT)
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_mcp_launcher_h1_test", _LAUNCHER_SCRIPT, loader=loader
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestH1DenyListCoversTheReviewsFullNameSet:
    """Security review 2026-09-10, verdict SHIP WITH FIXES — H-1's own
    reimplementation of both gates found ~50 names that passed both. Every
    one of them must now be denied by BOTH the daemon and the launcher."""

    @pytest.mark.parametrize("key", _H1_DANGEROUS_NAMES)
    def test_denied_at_the_daemon(self, key: str) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({key: "value"})

    @pytest.mark.parametrize("key", _H1_DANGEROUS_NAMES)
    def test_denied_at_the_launcher(self, key: str) -> None:
        module = _load_launcher_module()
        assert module._is_allowed_env_key(key) is False, key

    def test_brave_api_key_the_mcp_05_driver_still_passes_both_gates(self) -> None:
        assert _validate_mcp_env({"BRAVE_API_KEY": "br-xxx"}) == {"BRAVE_API_KEY": "br-xxx"}
        module = _load_launcher_module()
        assert module._is_allowed_env_key("BRAVE_API_KEY") is True


class TestLauncherOnlyCarveOuts:
    """The 4 names deliberately excluded from _H1_DANGEROUS_NAMES above —
    each one denied at the DAEMON (so a caller can never supply it) but
    still allowed through the LAUNCHER's own gate, because by the time
    anything reaches the launcher it can only be the daemon's OWN trusted
    value, never caller input."""

    def test_node_extra_ca_certs_denied_at_daemon_but_forwarded_by_launcher(self) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"NODE_EXTRA_CA_CERTS": "x"})
        module = _load_launcher_module()
        assert module._is_allowed_env_key("NODE_EXTRA_CA_CERTS") is True

    def test_ads_bearer_denied_at_daemon_but_forwarded_by_launcher(self) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"ADS_BEARER": "x"})
        module = _load_launcher_module()
        assert module._is_allowed_env_key("ADS_BEARER") is True

    @pytest.mark.parametrize("key", ["TMPDIR", "XDG_DATA_HOME"])
    def test_daemon_internal_cache_dirs_denied_at_daemon_but_allowed_at_launcher(
        self, key: str
    ) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({key: "x"})
        module = _load_launcher_module()
        assert module._is_allowed_env_key(key) is True
        assert key in module._INTERNAL_ENV_KEYS


class TestDenyListParityWithTheLauncher:
    """The two gates are deliberately duplicated (no runtime cross-import
    across the root-privilege boundary — see both files' own comments), so
    nothing enforces they stay in sync except a test. This is that test:
    the CORE deny set (everything except the few documented, one-name
    asymmetries — HOME, ADS_BEARER, XDG_CONFIG_HOME/MCP_REMOTE_CONFIG_DIR,
    NODE_EXTRA_CA_CERTS's launcher carve-out) must be byte-identical."""

    def test_deny_exact_core_matches(self) -> None:
        module = _load_launcher_module()
        assert module._BYOK_ENV_DENY_EXACT_CORE == _MCP_ENV_DENY_EXACT_CORE

    def test_deny_prefixes_match(self) -> None:
        module = _load_launcher_module()
        assert module._BYOK_ENV_DENY_PREFIXES == _MCP_ENV_DENY_PREFIXES_CORE


class TestUvOfflineIsNonOverridable:
    """Security review 2026-09-10 (MEDIUM): UV_OFFLINE is now deny-listed
    (H-1) AND _ALWAYS_FORWARDED_ENV_KEYS reads from the real os.environ, not
    from the caller-mergeable `env` dict — belt-and-braces, so a future
    deny-list gap can never let a caller's UV_OFFLINE value win."""

    def test_denied_at_both_gates(self) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"UV_OFFLINE": "0"})
        module = _load_launcher_module()
        assert module._is_allowed_env_key("UV_OFFLINE") is False

    def test_build_jailed_cmd_sources_it_from_os_environ_not_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_launcher_module()
        monkeypatch.setenv("UV_OFFLINE", "1")
        # `env` stands in for `sanitised` with a value that would only be
        # there if some future bug let a caller override it — the daemon's
        # own real environ must win regardless.
        env = {"UV_OFFLINE": "0"}
        cmd = module._build_jailed_cmd(["uvx", "some-pkg"], env, frozenset())
        assert "--setenv=UV_OFFLINE=1" in cmd
        assert "--setenv=UV_OFFLINE=0" not in cmd


class TestPreviouslyRejectedButPlausibleKeysAreNowAccepted:
    def test_the_forms_own_placeholder_example_is_accepted(self) -> None:
        """The exact repro (spec 025 matriz MCP-05): BRAVE_API_KEY=br-xxx,
        verbatim from McpView.tsx's own `mcp.env.label` placeholder."""
        result = _validate_mcp_env({"BRAVE_API_KEY": "br-xxx"})
        assert result == {"BRAVE_API_KEY": "br-xxx"}

    @pytest.mark.parametrize(
        "key",
        ["BRAVE_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY", "GITHUB_TOKEN", "SLACK_BOT_TOKEN"],
    )
    def test_any_plausible_published_server_secret_name_is_accepted(self, key: str) -> None:
        result = _validate_mcp_env({key: "secret-value"})
        assert result == {key: "secret-value"}

    def test_the_old_curated_pack_still_validates_unchanged(self) -> None:
        """The fixed set this replaces is now just a SUBSET of what the
        pattern accepts — nothing that used to work should stop working.
        ADS_BEARER/NODE_EXTRA_CA_CERTS are EXCLUDED here on purpose (security
        review H-1 follow-up): they are now fill-only, see
        TestAdsCompanionSecretsAreFillOnly below."""
        result = _validate_mcp_env(
            {
                "REPLICATE_API_TOKEN": "r1",
                "CONTEXT7_API_KEY": "c1",
                "OPENAI_BASE_URL": "http://vllm.local",
                "OPENAI_API_KEY": "sk-1",
            }
        )
        assert len(result) == 4


class TestAdsCompanionSecretsAreFillOnly:
    """Security review 2026-09-10 (H-1 follow-up): the module's own prior
    comment claimed "a caller passing a non-empty value would be ignored,
    not trusted" for ADS_BEARER — _autowire_companion_env's `if not
    resolved_env.get(...)` fill-only-when-EMPTY check did not actually
    enforce that (a non-empty caller value was never overwritten). Denying
    both at THIS caller-facing gate makes the claim true: the only way
    either key ever gets a value is _autowire_companion_env's own fill step
    at connect time, never a caller-supplied add_mcp_server draft."""

    def test_ads_bearer_denied(self) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"ADS_BEARER": "attacker-supplied"})

    def test_node_extra_ca_certs_denied(self) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"NODE_EXTRA_CA_CERTS": "/tmp/attacker-ca.pem"})


class TestDenyListStillRejectsDangerousNames:
    @pytest.mark.parametrize(
        "key",
        [
            "PATH",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "HERMES_OPERATOR_ID",
            "HERMES_HOME",
            "NODE_OPTIONS",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        ],
    )
    def test_dangerous_key_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({key: "value"})

    def test_error_message_never_echoes_the_value(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            _validate_mcp_env({"LD_PRELOAD": "super-secret-payload-path"})
        assert "super-secret-payload-path" not in str(exc_info.value)

    def test_lowercase_dangerous_key_also_rejected_by_the_pattern(self) -> None:
        """The allow PATTERN itself only ever matches upper-case names — a
        lowercase dangerous key never even reaches the deny-list check."""
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"path": "value"})


class TestHomeIsTheOneDeliberateException:
    """R16 (test_r16_mcp_bridge_handshake.py) — preserved verbatim."""

    def test_home_is_accepted_at_this_validation_layer(self) -> None:
        result = _validate_mcp_env({"HOME": "/var/lib/hermes"})
        assert result == {"HOME": "/var/lib/hermes"}


class TestValidationStillWorksAsBefore:
    def test_non_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="diccionario"):
            _validate_mcp_env(["not", "a", "dict"])

    def test_non_string_key_raises(self) -> None:
        with pytest.raises(ValueError, match="no es string"):
            _validate_mcp_env({1: "value"})

    def test_empty_value_raises(self) -> None:
        with pytest.raises(ValueError, match="no vacío"):
            _validate_mcp_env({"BRAVE_API_KEY": ""})

    def test_non_string_value_raises(self) -> None:
        with pytest.raises(ValueError, match="no vacío"):
            _validate_mcp_env({"BRAVE_API_KEY": 12345})

    def test_od_daemon_url_still_validated_as_url(self) -> None:
        with pytest.raises(ValueError, match="URL"):
            _validate_mcp_env({"OD_DAEMON_URL": "not-a-url"})

    def test_od_daemon_url_accepts_a_real_url(self) -> None:
        result = _validate_mcp_env({"OD_DAEMON_URL": "https://od.example.com"})
        assert result == {"OD_DAEMON_URL": "https://od.example.com"}

    def test_key_too_short_rejected(self) -> None:
        """Pattern requires >= 3 chars total (`^[A-Z][A-Z0-9_]{2,63}$`)."""
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"AB": "value"})

    def test_key_too_long_rejected(self) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"A" * 65: "value"})
