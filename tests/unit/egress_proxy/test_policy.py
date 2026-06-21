"""Tests de la política de egress (dominio puro, sin I/O).

Cubre:
  - open-logged: permite cualquier dominio.
  - default-deny: permite solo dominios en whitelist + subdominios.
  - default-deny: deniega dominios fuera de whitelist.
  - Normalización a minúsculas.
  - Subdominio permitido.
  - Subdominio rechazado (falso positivo de sufijo).
  - Sesión sin política registrada usa la política global.
  - push_policy actualiza la política de una sesión.
  - remove_session elimina la política de sesión.
"""

from __future__ import annotations

import pytest

from hermes.egress_proxy.domain.policy import (
    EgressMode,
    EgressPolicyEngine,
    SessionPolicy,
)

pytestmark = pytest.mark.unit

_SESSION = "10.200.0.2"  # IP del cliente en el netns


def _open_logged_policy(session_id: str = _SESSION) -> SessionPolicy:
    return SessionPolicy(session_id=session_id, mode=EgressMode.OPEN_LOGGED)


def _deny_policy(
    *,
    domains: frozenset[str],
    session_id: str = _SESSION,
) -> SessionPolicy:
    return SessionPolicy(
        session_id=session_id,
        mode=EgressMode.DEFAULT_DENY,
        domains_whitelist=domains,
    )


# ---------------------------------------------------------------------------
# open-logged
# ---------------------------------------------------------------------------


class TestOpenLogged:
    def test_allows_any_domain(self) -> None:
        engine = EgressPolicyEngine(global_policy=_open_logged_policy())
        decision = engine.evaluate(domain="example.com", session_id=_SESSION)
        assert decision.allowed is True
        assert decision.mode == EgressMode.OPEN_LOGGED

    def test_allows_evil_domain(self) -> None:
        engine = EgressPolicyEngine(global_policy=_open_logged_policy())
        decision = engine.evaluate(domain="evil.attacker.example", session_id=_SESSION)
        assert decision.allowed is True

    def test_domain_normalized(self) -> None:
        engine = EgressPolicyEngine(global_policy=_open_logged_policy())
        decision = engine.evaluate(domain="EXAMPLE.COM", session_id=_SESSION)
        assert decision.domain == "example.com"


# ---------------------------------------------------------------------------
# default-deny
# ---------------------------------------------------------------------------


class TestDefaultDeny:
    def test_allows_exact_domain(self) -> None:
        engine = EgressPolicyEngine(
            global_policy=_deny_policy(domains=frozenset({"example.com"}))
        )
        decision = engine.evaluate(domain="example.com", session_id=_SESSION)
        assert decision.allowed is True

    def test_allows_subdomain(self) -> None:
        engine = EgressPolicyEngine(
            global_policy=_deny_policy(domains=frozenset({"example.com"}))
        )
        decision = engine.evaluate(domain="api.example.com", session_id=_SESSION)
        assert decision.allowed is True

    def test_allows_deep_subdomain(self) -> None:
        engine = EgressPolicyEngine(
            global_policy=_deny_policy(domains=frozenset({"example.com"}))
        )
        decision = engine.evaluate(domain="a.b.c.example.com", session_id=_SESSION)
        assert decision.allowed is True

    def test_denies_domain_not_in_whitelist(self) -> None:
        engine = EgressPolicyEngine(
            global_policy=_deny_policy(domains=frozenset({"example.com"}))
        )
        decision = engine.evaluate(domain="evil.attacker.example", session_id=_SESSION)
        assert decision.allowed is False
        assert decision.mode == EgressMode.DEFAULT_DENY

    def test_denies_suffix_that_is_not_subdomain(self) -> None:
        """``notexample.com`` no es subdominio de ``example.com``."""
        engine = EgressPolicyEngine(
            global_policy=_deny_policy(domains=frozenset({"example.com"}))
        )
        decision = engine.evaluate(domain="notexample.com", session_id=_SESSION)
        assert decision.allowed is False

    def test_denies_empty_whitelist(self) -> None:
        engine = EgressPolicyEngine(
            global_policy=_deny_policy(domains=frozenset())
        )
        decision = engine.evaluate(domain="example.com", session_id=_SESSION)
        assert decision.allowed is False

    def test_domain_case_normalized(self) -> None:
        engine = EgressPolicyEngine(
            global_policy=_deny_policy(domains=frozenset({"example.com"}))
        )
        decision = engine.evaluate(domain="EXAMPLE.COM", session_id=_SESSION)
        assert decision.allowed is True

    def test_trailing_dot_stripped(self) -> None:
        engine = EgressPolicyEngine(
            global_policy=_deny_policy(domains=frozenset({"example.com"}))
        )
        decision = engine.evaluate(domain="example.com.", session_id=_SESSION)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Política por sesión vs global
# ---------------------------------------------------------------------------


class TestSessionVsGlobal:
    def test_unregistered_session_uses_global(self) -> None:
        global_policy = _open_logged_policy(session_id="__global__")
        engine = EgressPolicyEngine(global_policy=global_policy)
        # sesión "unknown" no registrada → política global
        decision = engine.evaluate(domain="example.com", session_id="unknown-session")
        assert decision.allowed is True
        assert decision.mode == EgressMode.OPEN_LOGGED

    def test_push_policy_overrides_global_for_session(self) -> None:
        global_policy = _open_logged_policy(session_id="__global__")
        engine = EgressPolicyEngine(global_policy=global_policy)

        session_id = "cred-session-1"
        strict_policy = _deny_policy(
            domains=frozenset({"allowed.com"}),
            session_id=session_id,
        )
        engine.push_policy(strict_policy)

        # Esta sesión queda en default-deny
        deny_decision = engine.evaluate(domain="evil.com", session_id=session_id)
        assert deny_decision.allowed is False

        # Otras sesiones siguen en open-logged
        open_decision = engine.evaluate(domain="evil.com", session_id="other-session")
        assert open_decision.allowed is True

    def test_push_policy_allows_whitelisted_domain(self) -> None:
        engine = EgressPolicyEngine()
        session_id = "cred-session-2"
        engine.push_policy(
            _deny_policy(domains=frozenset({"bank.example.com"}), session_id=session_id)
        )
        decision = engine.evaluate(domain="bank.example.com", session_id=session_id)
        assert decision.allowed is True

    def test_remove_session_reverts_to_global(self) -> None:
        global_policy = _deny_policy(
            domains=frozenset({"safe.com"}), session_id="__global__"
        )
        engine = EgressPolicyEngine(global_policy=global_policy)
        session_id = "temp-session"
        engine.push_policy(
            SessionPolicy(
                session_id=session_id,
                mode=EgressMode.OPEN_LOGGED,
            )
        )
        # Con política propia → open-logged → permite evil.com
        assert engine.evaluate(domain="evil.com", session_id=session_id).allowed is True
        # Después de remove → hereda global (default-deny) → deniega
        engine.remove_session(session_id)
        assert engine.evaluate(domain="evil.com", session_id=session_id).allowed is False

    def test_replace_global_affects_unregistered_sessions(self) -> None:
        engine = EgressPolicyEngine()
        engine.replace_global(
            SessionPolicy(
                session_id="__global__",
                mode=EgressMode.DEFAULT_DENY,
                domains_whitelist=frozenset({"safe.com"}),
            )
        )
        decision = engine.evaluate(domain="unsafe.com", session_id="any-session")
        assert decision.allowed is False
