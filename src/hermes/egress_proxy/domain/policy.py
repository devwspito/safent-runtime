"""Política de egress — decisión por dominio, pura y testeable.

No hay I/O aquí.  La política se construye en la capa de infraestructura
y se consulta desde el servidor proxy.

Dos modos (§3 del diseño):
  - EgressMode.OPEN_LOGGED:   permite cualquier dominio; registra cada destino.
  - EgressMode.DEFAULT_DENY:  solo dominios en ``domains_whitelist``; el resto → deny.

El modo y la whitelist son por-sesión (session_id) y se actualizan
desde el socket de control root ``/run/hermes/egress-proxy.sock``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import FrozenSet

# C1 PASS-3 (2026-06-19) — NO registry network at RUNTIME.
# -------------------------------------------------------
# The MCP RUNTIME netns is PURE default-deny: no package registry is allowed by default.
# This is the architectural fix that closes the npm-PUT exfil residual the previous pass
# documented but could not close at L4: a registry CONNECT is a blind TLS tunnel, so the
# proxy cannot tell a GET download from an `npm publish` PUT / PyPI upload. As long as the
# runtime could reach the registries, a malicious-but-scanned MCP server with a registry
# account could exfiltrate via a package upload.
#
# We remove that capability entirely from the runtime: packages are RESOLVED +
# DOWNLOADED + SCANNED at INSTALL time by the TRUSTED daemon path (Security Center's
# content scan runs there) into a per-server cache, and the MCP RUNTIME spawns OFFLINE
# from that cache (npx --offline / uvx --offline). The runtime therefore needs no
# registry network at all — there is nothing to widen, nothing to exfiltrate through.
#
# C1 PASS-4 (2026-06-19) — RE-PINNABLE MCP egress (never a paperweight).
# ---------------------------------------------------------------------
# PASS-3 pinned the MCP source IP to default-deny with an EMPTY whitelist set ONCE at
# boot and immutable. That made network-MCPs DEAD: a curated BYOK server (Open Design,
# Replicate, Context7) or any owner-installed network-MCP got a 403 on every host with no
# way to grant the host it needs. The fix here keeps the security invariant (the MCP plane
# stays default-deny and the BROWSER control-socket push can never widen it) while adding
# the missing elevation path: the OWNER can grant the SPECIFIC host(s) a network-MCP needs
# into the MCP's pinned whitelist via ``grant_to_pinned`` (driven by the egress elevation
# API). A granted network-MCP then reaches ONLY its granted host(s) through the proxy;
# everything else — evil.com, npm/PyPI, an UNgranted MCP — stays denied. Curated BYOK
# hosts WE ship and vet are pre-granted at boot so those servers work out-of-the-box.
#
# Trust boundary: ``grant_to_pinned`` is reachable ONLY from the proxy entrypoint (boot
# pre-grants) and from the control socket via a RESERVED session marker that the lower-
# trust browser jail cannot emit (the control socket is group hermes-egress: only the
# daemon's browser flow + the shell-server elevation API can connect). The MCP children
# themselves have /run/hermes InaccessiblePaths and no group membership to reach it.


class EgressMode(StrEnum):
    """Modo de filtrado de la política de egress."""

    OPEN_LOGGED = "open-logged"
    DEFAULT_DENY = "default-deny"


@dataclass(frozen=True, slots=True)
class EgressDecision:
    """Resultado de una evaluación de política."""

    allowed: bool
    domain: str
    session_id: str
    mode: EgressMode
    reason: str


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Política por sesión empujada desde el socket de control."""

    session_id: str
    mode: EgressMode
    domains_whitelist: FrozenSet[str] = field(default_factory=frozenset)


class EgressPolicyEngine:
    """Motor de decisión — stateful pero seguro para uso concurrente con GIL.

    La política activa puede cambiar en cualquier momento desde el socket
    de control (hilo separado) mientras el servidor proxy evalúa peticiones.
    Las actualizaciones son atómicas a nivel de referencia de objeto (GIL de
    CPython garantiza la asignación de referencias como operación atómica).

    Las sessions sin política registrada obtienen la política global.

    PINNED policies (C1 PASS-2 fix 2026-06-19)
    ------------------------------------------
    A *pinned* policy is keyed by a client identifier (the proxy passes the netns
    source IP as ``session_id``) and is IMMUTABLE from the control socket: neither
    ``push_policy`` nor ``replace_global`` can overwrite or shadow it. This exists so
    the MCP children — which have their OWN egress identity (a distinct source IP,
    ``10.200.1.2``, from a separate netns) — keep a fixed default-deny + registries-only
    policy even when a browser teaching/discovery session pushes ``open-logged`` over
    the control socket. Before this, the control socket called ``replace_global`` and
    one global policy governed the WHOLE netns by source IP, so flipping the browser to
    open-logged ALSO opened the MCP child to allow-all (red-team bypass #1). Pinning the
    MCP's source IP severs that link: the policy plane is per-client, and the MCP client
    is in a band the control socket cannot widen.
    """

    # Reserved control-socket session marker that routes an owner grant to the MCP's
    # PINNED policy instead of the browser's global policy. The control socket handler
    # always calls ``replace_global``; this marker lets that single entry point grant the
    # MCP plane WITHOUT widening the browser plane. Only the trusted setters (group
    # hermes-egress: the daemon browser flow + the shell-server elevation API) can emit it
    # — the browser jail cannot reach the control socket at all.
    MCP_GRANT_SESSION: str = "__mcp_grant__"

    def __init__(self, *, global_policy: SessionPolicy | None = None) -> None:
        # Fix-5: when no policy is supplied, default to DEFAULT_DENY (fail-closed).
        # OPEN_LOGGED is an explicit opt-in for discovery, not the boot default.
        self._global: SessionPolicy = global_policy or SessionPolicy(
            session_id="__global__",
            mode=EgressMode.DEFAULT_DENY,
        )
        self._sessions: dict[str, SessionPolicy] = {}
        # Pinned policies: control-socket pushes can NEVER overwrite these wholesale (see
        # above). They CAN be narrowly extended by the owner via ``grant_to_pinned`` /
        # the reserved MCP_GRANT_SESSION marker — but only ever stay DEFAULT_DENY with an
        # explicit host whitelist; the mode can never be flipped to OPEN_LOGGED here.
        self._pinned: dict[str, SessionPolicy] = {}
        # The client_id whose pinned policy receives owner MCP grants (the MCP source IP).
        # Set by ``pin_policy``; the reserved-marker grant path targets exactly this id so
        # an attacker-influenced control-socket session_id can never name a different one.
        self._mcp_client_id: str | None = None
        # C1 PASS-5: the CURATED seed floor per pinned client. The pinned whitelist is
        # ALWAYS curated_seed ∪ owner_grants — the seed is the floor that no grant/revoke
        # can drop below. Kept SEPARATE from the live whitelist so that:
        #   - ``grant_to_pinned`` unions the seed back in on EVERY recompute (the owner
        #     payload carries only owner grants; without re-uniting the seed, re-pushing
        #     the owner-only grants file WIPED the curated hosts → 403 regression).
        #   - revoke can remove an owner grant but NEVER a curated host (it stays in seed).
        # The seed is registered at ``pin_policy`` time and is immutable thereafter.
        self._pinned_seed: dict[str, FrozenSet[str]] = {}

    def pin_policy(
        self,
        *,
        client_id: str,
        policy: SessionPolicy,
        seed: FrozenSet[str] | None = None,
    ) -> None:
        """Register a pinned policy for ``client_id`` (the MCP source IP).

        Pinned policies take precedence over session and global policies in ``evaluate``
        and CANNOT be replaced wholesale via the control socket (``push_policy`` /
        ``replace_global`` skip pinned client ids). Called once at boot from the proxy
        entrypoint, never from the (lower-trust) control path.

        The pinned policy stays DEFAULT_DENY; the owner may later EXTEND its host
        whitelist via ``grant_to_pinned`` (C1 PASS-4) so a vetted/granted network-MCP can
        reach exactly the host(s) it needs — everything else stays denied. The most
        recently pinned client_id is the target of the reserved MCP_GRANT_SESSION marker.

        C1 PASS-5: ``seed`` is the CURATED floor (the BYOK hosts WE ship + vet). It is the
        immutable floor of the pinned whitelist: the effective whitelist is ALWAYS
        ``seed ∪ owner_grants``. ``grant_to_pinned`` re-unions it on every recompute and
        revoke can never drop a seeded host. Defaults to the policy's own whitelist so a
        caller that pre-builds ``seed ∪ grants`` and passes no ``seed`` still has a floor
        (back-compat); pass ``seed`` explicitly to keep the curated/granted split.
        """
        curated = (
            frozenset(d.lower().rstrip(".") for d in seed if d)
            if seed is not None
            else policy.domains_whitelist
        )
        self._pinned_seed[client_id] = curated
        # Enforce the floor immediately: the live whitelist must include the curated seed.
        self._pinned[client_id] = SessionPolicy(
            session_id=policy.session_id,
            mode=EgressMode.DEFAULT_DENY,
            domains_whitelist=curated | policy.domains_whitelist,
        )
        self._mcp_client_id = client_id

    def grant_to_pinned(self, *, client_id: str, domains: FrozenSet[str]) -> None:
        """Set the OWNER grants of ``client_id``'s pinned policy to ``domains`` (elevation).

        C1 PASS-4/5: the grant path for network-MCPs. The effective pinned whitelist is
        ALWAYS ``curated_seed ∪ owner_grants`` with the mode FORCED to DEFAULT_DENY — a
        grant can only ever ADD specific hosts on top of the curated floor, never flip the
        MCP plane to allow-all and never drop below the floor. If the client was not
        previously pinned, it is pinned now (default-deny + the granted domains as both
        seed-less floor and grants), so a grant always lands on an immutable,
        control-socket-proof entry.

        SET semantics for the OWNER-GRANT layer only: ``domains`` is the FULL desired
        owner-grant set (the elevation API recomputes it on grant/revoke), so revoking an
        owner host actually removes it. But the CURATED seed is unioned back in here on
        EVERY call — this is the class fix: re-pushing the owner-only grants file (boot
        re-apply, or any grant/revoke) can no longer WIPE the curated hosts. Called from
        the proxy entrypoint (boot) and the control socket via the reserved
        MCP_GRANT_SESSION marker (owner elevation API).
        """
        normalized = frozenset(d.lower().rstrip(".") for d in domains if d)
        prior = self._pinned.get(client_id)
        session_id = prior.session_id if prior is not None else "__mcp__"
        # The curated seed is the floor and is NEVER removable by an owner grant/revoke.
        seed = self._pinned_seed.get(client_id, frozenset())
        self._pinned[client_id] = SessionPolicy(
            session_id=session_id,
            mode=EgressMode.DEFAULT_DENY,
            domains_whitelist=seed | normalized,
        )
        if self._mcp_client_id is None:
            self._mcp_client_id = client_id

    def pinned_whitelist(self, client_id: str) -> FrozenSet[str]:
        """Return the current host whitelist of ``client_id``'s pinned policy (empty if
        none). Lets the proxy entrypoint read what is already granted (idempotent boot)."""
        policy = self._pinned.get(client_id)
        return policy.domains_whitelist if policy is not None else frozenset()

    def push_policy(self, policy: SessionPolicy) -> None:
        """Registra o reemplaza la política de una sesión.

        Llamado desde el socket de control (puede ser hilo diferente).
        La asignación de dict es atómica bajo el GIL de CPython.

        SECURITY: a pinned client id (the MCP source IP) is NEVER mutated here — the
        control socket carries an attacker-influenced ``session_id`` (the browser jail
        labels its pushes), and without this guard a crafted push could shadow the MCP's
        fixed policy.
        """
        if policy.session_id in self._pinned:
            return
        self._sessions[policy.session_id] = policy

    def remove_session(self, session_id: str) -> None:
        """Elimina la política de una sesión al finalizar."""
        self._sessions.pop(session_id, None)

    def evaluate(self, *, domain: str, session_id: str) -> EgressDecision:
        """Evalúa si ``domain`` está permitido para ``session_id``.

        Resolution order (most specific first): pinned client policy → session
        policy → global policy. Normaliza el dominio a minúsculas antes de comparar.
        """
        policy = (
            self._pinned.get(session_id)
            or self._sessions.get(session_id)
            or self._global
        )
        normalized = domain.lower().rstrip(".")
        return _apply_policy(policy=policy, domain=normalized)

    @property
    def global_policy(self) -> SessionPolicy:
        return self._global

    def policy_for(self, session_id: str) -> SessionPolicy:
        """Return the effective policy for ``session_id`` (pinned → session → global).

        Used by the proxy handler to read the active mode without duplicating the
        precedence rule (pinned MCP clients must not be treated as the global mode).
        """
        return (
            self._pinned.get(session_id)
            or self._sessions.get(session_id)
            or self._global
        )

    def replace_global(self, policy: SessionPolicy) -> None:
        """Reemplaza la política global (sesiones sin política registrada).

        SECURITY: pinned client policies are unaffected — they are resolved BEFORE the
        global in ``evaluate``/``policy_for``, so the MCP's fixed default-deny survives
        any control-socket ``open-logged`` push aimed at the browser.

        C1 PASS-4: the control socket handler funnels EVERY push through this one method.
        A push tagged with the reserved ``MCP_GRANT_SESSION`` marker is an OWNER MCP grant
        — it is routed to ``grant_to_pinned`` (extends the MCP's pinned whitelist, mode
        forced default-deny) instead of replacing the browser global. The marker is only
        emittable by the trusted setters that can reach the control socket (group
        hermes-egress); the browser jail cannot. A grant with no MCP client pinned yet is
        a no-op (nothing to extend) rather than leaking into the global plane.
        """
        if policy.session_id == self.MCP_GRANT_SESSION:
            target = self._mcp_client_id
            if target is not None:
                self.grant_to_pinned(
                    client_id=target, domains=policy.domains_whitelist
                )
            return
        self._global = policy


def _apply_policy(*, policy: SessionPolicy, domain: str) -> EgressDecision:
    """Aplica la política al dominio — función pura."""
    if policy.mode == EgressMode.OPEN_LOGGED:
        return EgressDecision(
            allowed=True,
            domain=domain,
            session_id=policy.session_id,
            mode=policy.mode,
            reason="open-logged: any domain allowed",
        )
    # C1 PASS-3: NO package-registry baseline. The MCP runtime is pure default-deny —
    # packages are pre-fetched + scanned at INSTALL time and the runtime spawns offline
    # from the cache, so the runtime never needs npm/PyPI. This closes the npm-PUT exfil
    # residual: there is no standing registry allow to ride a published-package upload on.
    # DEFAULT_DENY: comprueba whitelist + subdominios
    if _matches_whitelist(domain=domain, whitelist=policy.domains_whitelist):
        return EgressDecision(
            allowed=True,
            domain=domain,
            session_id=policy.session_id,
            mode=policy.mode,
            reason=f"default-deny: domain in whitelist ({domain})",
        )
    return EgressDecision(
        allowed=False,
        domain=domain,
        session_id=policy.session_id,
        mode=policy.mode,
        reason=f"default-deny: domain not in whitelist ({domain})",
    )


def _matches_whitelist(*, domain: str, whitelist: FrozenSet[str]) -> bool:
    """Devuelve True si ``domain`` coincide exactamente o es subdominio de algún
    dominio en ``whitelist``.

    Ejemplos:
      - ``example.com`` en whitelist → ``example.com`` OK, ``sub.example.com`` OK.
      - ``evil.com`` NOT en whitelist → deny.
    """
    for allowed in whitelist:
        if domain == allowed or domain.endswith("." + allowed):
            return True
    return False
