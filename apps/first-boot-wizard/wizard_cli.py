#!/usr/bin/env python3
"""first-boot wizard CLI agentico (placeholder demoable).

Spec 003 FR-002, FR-019..FR-023. En personal-desktop el wizard se
ejecuta vía la app GTK4; en server/workspace-only se ejecuta vía
este CLI tras el primer boot, conducido por preguntas TTS+ASR del
agente. Aquí el placeholder demoable cubre la ruta CLI síncrona
para tests E2E + setup desatendido.

Usage:
    hermes-first-boot-wizard [--profile NAME] [--unattended]

El servicio realista vendrá enchufado al runtime via DBus en una
iteración siguiente.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agents OS first-boot wizard CLI"
    )
    parser.add_argument(
        "--profile",
        choices=("personal-desktop", "workspace-only", "server"),
        required=True,
    )
    parser.add_argument(
        "--tenant-endpoint",
        help="Cloud SaaS / self-hosted control plane URL",
    )
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="Acepta defaults sin prompts (server installation)",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    from hermes.agents_os.application.first_boot_wizard import (
        InMemoryFirstBootWizard,
    )
    sys.path.insert(
        0,
        str(
            Path(__file__).resolve().parents[2]
            / "specs"
            / "003-agents-os-edition"
        ),
    )
    from contracts.first_boot_wizard_port import (  # noqa: E402
        ConsentInitialSelection,
        ExposedServiceDescriptor,
        LocaleSelection,
        NetworkDecision,
        TenantBindingDecision,
        TenantBindingIntent,
    )
    from contracts.agents_os_image_port import (  # noqa: E402
        InstallProfileKind,
    )

    wizard = InMemoryFirstBootWizard()
    snap = await wizard.start(agent_driven=False)
    sid = snap.wizard_session_id

    profile_kind = InstallProfileKind(args.profile.replace("-", "_"))
    await wizard.set_profile(
        wizard_session_id=sid, profile_kind=profile_kind
    )
    await wizard.set_locale(
        wizard_session_id=sid,
        locale=LocaleSelection(
            language_code="es",
            keyboard_layout="es",
            timezone="Europe/Madrid",
        ),
    )
    await wizard.set_network(
        wizard_session_id=sid, decision=NetworkDecision.CONNECTED
    )
    await wizard.set_tenant_binding(
        wizard_session_id=sid,
        intent=TenantBindingIntent(
            decision=TenantBindingDecision.BIND_NOW
            if args.tenant_endpoint
            else TenantBindingDecision.DEFER,
            tenant_provided_endpoint=args.tenant_endpoint,
        ),
    )
    if args.profile == "personal-desktop":
        await wizard.set_initial_consents(
            wizard_session_id=sid,
            consents=ConsentInitialSelection(
                granted=(("documents", "session"),)
            ),
        )
    await wizard.review_exposed_services(
        wizard_session_id=sid,
        services=(
            ExposedServiceDescriptor(
                service_name="hermes-runtime",
                interface="loopback:9000",
                protocol="https",
                expected_identity="hermes-runtime",
                human_description="API local del runtime",
            ),
        ),
        acknowledged=True,
    )
    completed = await wizard.finalize(wizard_session_id=sid)
    print(
        json.dumps(
            {
                "wizard_session_id": str(completed.wizard_session_id),
                "node_installation_id": str(
                    completed.produced_node_installation_id
                ),
                "state": completed.state.value,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
