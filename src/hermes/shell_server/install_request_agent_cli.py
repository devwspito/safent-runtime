"""install_request_agent_cli — the host agent's own consumer of the
install-request marker (contracts/install-request.md, T016).

Usage (inside the container, invoked by `safent agent` / `safent companion
install|repair` on the HOST):
  python3 -m hermes.shell_server.install_request_agent_cli claim <verb> --claimant X
  python3 -m hermes.shell_server.install_request_agent_cli resolve <verb> --success|--failure

Why a CLI, not raw shell against the marker file
--------------------------------------------------
`hermes.shell_server.install_requests` already owns the marker's format,
TTL-per-verb and claim/expiry rules (T006) — the closed vocabulary the
Constitution Principle 0 condition in plan.md holds this module to. The
host-side `safent` CLI has no Python runtime of its own and must never
re-implement JSON/expiry/claim parsing in POSIX sh (fragile, untested,
exactly the kind of second implementation `install-request.md` §4 warns
against: "misma implementacion" for both readers). This thin wrapper is the
SECOND reader (the host agent) reusing the ONE implementation, the same way
`brake_release_cli.py` reuses the daemon's D-Bus surface instead of a
shell-side re-implementation of Resume().

`claim` prints the claimed slug (or an empty line if the verb carries none)
to stdout and exits 0 on success; exits 1 with no output when there is
nothing live to claim or someone else holds a live claim — the shell caller
treats either as "nothing to do right now", never an error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def cmd_claim(verb: str, claimant: str) -> int:
    from hermes.shell_server.install_requests import VERBS, claim_request  # noqa: PLC0415

    if verb not in VERBS or verb in {"install_companion", "repair_companion"}:
        print(f"unknown verb: {verb}", file=sys.stderr)
        return 2
    claimed = claim_request(verb, claimant=claimant)
    if claimed is None:
        return 1
    print(claimed.slug or "")
    return 0


def cmd_resolve(verb: str, *, success: bool) -> int:
    from hermes.shell_server.install_requests import VERBS, resolve_request  # noqa: PLC0415

    if verb not in VERBS or verb in {"install_companion", "repair_companion"}:
        print(f"unknown verb: {verb}", file=sys.stderr)
        return 2
    resolve_request(verb, success=success)
    return 0


async def _verify_ads() -> bool:
    from hermes.agents_os.infrastructure.companion_sso_authority import (  # noqa: PLC0415
        _load_sso_private_key,
    )
    from hermes.shell_server.companion_reload_cli import _reload_companion_presence  # noqa: PLC0415

    _load_sso_private_key()
    # The daemon itself probes the authenticated TLS endpoint and validates the
    # exact wire contract. A listening port (including 401) is not readiness.
    result = await _reload_companion_presence("safent-ads")
    return (
        result.get("ok") is True
        and result.get("reachable") is True
        and result.get("state") in ("ready", "no_accounts")
    )


async def _health_ads() -> bool:
    from hermes.agents_os.infrastructure.companion_health_check import (  # noqa: PLC0415
        CompanionHealthChecker,
    )
    from hermes.agents_os.infrastructure.companion_sso_authority import (  # noqa: PLC0415
        _load_sso_private_key,
    )

    _load_sso_private_key()
    report = await CompanionHealthChecker().check("safent-ads")
    return report.state in ("ready", "no_accounts") and report.reachable is True


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    parser = argparse.ArgumentParser(prog="install_request_agent_cli")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    sub.add_parser("verify-ads", help="Reload and verify authenticated daemon health.")
    sub.add_parser("health-ads", help="Read authenticated companion health, without reload.")
    ads_claim = sub.add_parser("claim-ads", help="Claim one closed Ads install/repair intention.")
    ads_claim.add_argument("--claimant", required=True)
    for name in ("renew-ads", "resolve-ads"):
        ads = sub.add_parser(name)
        ads.add_argument("verb", choices=("install_companion", "repair_companion"))
        ads.add_argument("--claimant", required=True)
        ads.add_argument("--request-id", required=True)
        if name == "resolve-ads":
            ads_outcome = ads.add_mutually_exclusive_group(required=True)
            ads_outcome.add_argument("--success", action="store_true")
            ads_outcome.add_argument("--failure", action="store_true")

    claim_parser = sub.add_parser("claim", help="Claim a live install request.")
    claim_parser.add_argument("verb")
    claim_parser.add_argument("--claimant", required=True)

    resolve_parser = sub.add_parser("resolve", help="Release a claim; consume on success.")
    resolve_parser.add_argument("verb")
    outcome = resolve_parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--success", action="store_true")
    outcome.add_argument("--failure", action="store_true")

    args = parser.parse_args(argv)

    if args.subcommand in ("verify-ads", "health-ads"):
        try:
            probe = _verify_ads if args.subcommand == "verify-ads" else _health_ads
            return 0 if asyncio.run(probe()) else 1
        except Exception:  # noqa: BLE001 -- no endpoint/token/response echo
            print("Companion registration or authenticated health failed.", file=sys.stderr)
            return 1

    if args.subcommand == "claim-ads":
        from hermes.shell_server.install_requests import (  # noqa: PLC0415
            claim_request,
            reject_unsupported_companion_request,
        )

        reject_unsupported_companion_request()
        for verb in ("install_companion", "repair_companion"):
            claimed = claim_request(verb, claimant=args.claimant, allow_reclaim=False)
            if claimed is not None:
                print(f"{claimed.verb} {claimed.request_id}")
                return 0
        return 1
    if args.subcommand == "renew-ads":
        from hermes.shell_server.install_requests import renew_ads_request  # noqa: PLC0415

        return (
            0
            if renew_ads_request(args.verb, claimant=args.claimant, request_id=args.request_id)
            else 1
        )
    if args.subcommand == "resolve-ads":
        from hermes.shell_server.install_requests import resolve_ads_request  # noqa: PLC0415

        return (
            0
            if resolve_ads_request(
                args.verb, claimant=args.claimant, request_id=args.request_id, success=args.success
            )
            else 1
        )

    if args.subcommand == "claim":
        return cmd_claim(args.verb, args.claimant)
    return cmd_resolve(args.verb, success=args.success)


if __name__ == "__main__":
    sys.exit(main())
