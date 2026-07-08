"""hermes.instance CLI — pair / unpair / status.

Usage (inside the container):
  python3 -m hermes.instance pair --stdin [--cloud <url>]
  python3 -m hermes.instance unpair
  python3 -m hermes.instance status

Security: the pairing code is read from stdin (--stdin flag), NOT from
argv.  Reading from argv exposes the code in process listings (ps aux) and
shell history.  The `safent pair` CLI passes the code via stdin:
  echo "$_code" | $RT exec -i $NAME python3 -m hermes.instance pair --stdin

The shell-server runs on the same machine so we share the same DB path
(HERMES_SHELL_DB env var or the default /var/lib/hermes/shell-state.db).
The vault requires master.key to exist at /var/lib/hermes/master.key.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_DB_PATH = Path(
    os.environ.get("HERMES_SHELL_DB", "/var/lib/hermes/shell-state.db")
)
_DEFAULT_CLOUD = os.environ.get(
    "SAFENT_CLOUD_ENDPOINT", "https://cloud.safent.run"
)


def _build_store():  # type: ignore[return]  # returns SQLiteAssociationStore
    from hermes.instance.association_store import SQLiteAssociationStore  # noqa: PLC0415
    from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

    vault = SecretsVault()
    return SQLiteAssociationStore(db_path=_DB_PATH, vault=vault)


def _build_pairing_service(*, cloud_endpoint: str):  # type: ignore[return]
    from hermes.agents_os.application.node_enrollment import NodeEnrollmentService  # noqa: PLC0415
    from hermes.agents_os.application.tenant_binding_service import TenantBindingService  # noqa: PLC0415
    from hermes.instance.infrastructure.http_control_plane_client import HttpControlPlaneClient  # noqa: PLC0415
    from hermes.instance.pairing_service import PairingService  # noqa: PLC0415

    return PairingService(
        enrollment=NodeEnrollmentService(),
        binding=TenantBindingService(),
        store=_build_store(),
        client=HttpControlPlaneClient(cloud_endpoint=cloud_endpoint),
    )


def cmd_pair(cloud_endpoint: str, *, use_stdin: bool) -> None:
    from hermes.instance.pairing_service import PairingError  # noqa: PLC0415

    if use_stdin:
        code = sys.stdin.readline().strip()
        if not code:
            print("[x] No pairing code received on stdin.", file=sys.stderr)
            sys.exit(1)
    else:
        print(
            "[x] Use --stdin to provide the pairing code securely via stdin.\n"
            "    Example: echo '<code>' | python3 -m hermes.instance pair --stdin",
            file=sys.stderr,
        )
        sys.exit(1)

    svc = _build_pairing_service(cloud_endpoint=cloud_endpoint)
    try:
        assoc = svc.pair(code=code, cloud_endpoint=cloud_endpoint)
    except PairingError as exc:
        print(f"[x] Pairing failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("[ok] Instance associated successfully.")
    print(f"     tenant_id : {assoc.tenant_id}")
    print(f"     instance  : {assoc.instance_id}")
    print(f"     edition   : associate")
    print(f"     paired_at : {assoc.paired_at}")


def cmd_unpair() -> None:
    store = _build_store()
    if not store.is_associated():
        print("[ok] Not associated — nothing to unpair.")
        return
    store.clear()
    from hermes.instance.pairing_service import remove_enterprise_marker  # noqa: PLC0415

    remove_enterprise_marker()  # config-sync goes inert again (no more policy pulls)
    print("[ok] Instance unpaired.  Edition reverted to community.")


def cmd_status() -> None:
    store = _build_store()
    edition = store.edition()
    assoc = store.get()
    if assoc is None:
        print(json.dumps({"edition": "community", "associated": False}))
        return
    print(
        json.dumps(
            {
                "edition": edition,
                "associated": True,
                "instance_id": assoc.instance_id,
                "tenant_id": assoc.tenant_id,
                "paired_at": assoc.paired_at,
                "last_applied_version": assoc.last_applied_version,
                "state": assoc.state,
            }
        )
    )


def _usage() -> None:
    print(
        "Usage:\n"
        "  echo <code> | python3 -m hermes.instance pair --stdin [--cloud <url>]\n"
        "  python3 -m hermes.instance unpair\n"
        "  python3 -m hermes.instance status"
    )


def main() -> None:
    args = sys.argv[1:]
    if not args:
        _usage()
        sys.exit(1)

    subcmd = args[0]

    if subcmd == "pair":
        remaining = args[1:]
        cloud = _DEFAULT_CLOUD
        use_stdin = False
        i = 0
        while i < len(remaining):
            if remaining[i] == "--stdin":
                use_stdin = True
            elif remaining[i] == "--cloud" and i + 1 < len(remaining):
                cloud = remaining[i + 1]
                i += 1
            i += 1
        cmd_pair(cloud, use_stdin=use_stdin)

    elif subcmd == "unpair":
        cmd_unpair()

    elif subcmd == "status":
        cmd_status()

    else:
        print(f"[x] Unknown subcommand: {subcmd}", file=sys.stderr)
        _usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
