"""GEPA Offline Skill Evolution CLI.

Usage:
    python -m hermes.training.evolution [OPTIONS]

This is an OFFLINE process. It does NOT run inside the agent loop.
Typical use: scheduled cron job or operator-initiated command on the OS.

What it does:
    1. Reads PROPOSAL_REJECTED audit entries from the audit DB.
    2. Identifies skills that failed >= min_failures times.
    3. Runs GEPAEvolutionEngine (DSPy + GEPA optimizer) over each candidate.
    4. Prints SkillEvolutionProposal JSON to stdout (one per line).
    5. The operator submits each proposal to the capability broker with
       a valid HITL approval token. GEPA never writes skills directly.

INVARIANT: This CLI produces proposals (JSON to stdout). It NEVER writes
           any SKILL.md file. The governance pipeline (broker → HITL →
           SkillStoreAdapter → HMAC sign) is the only write path.

Environment variables required for the LLM model:
    HERMES_EVOLUTION_MODEL   Model string (default: openai/gpt-4o-mini)
    OPENAI_API_KEY           or equivalent for your provider
    HERMES_EVOLUTION_STEPS   GEPA optimization steps per skill (default: 5)

Requires [evolution] extra:
    pip install "hermes-runtime[evolution]"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

logger = logging.getLogger(__name__)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hermes.training.evolution",
        description="GEPA offline skill evolution — produces proposals, never writes skills",
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="Tenant UUID to scope audit log reads",
    )
    parser.add_argument(
        "--audit-db",
        type=Path,
        default=Path("/var/lib/hermes/shell-state.db"),
        help="Path to audit SQLite DB (default: /var/lib/hermes/shell-state.db)",
    )
    parser.add_argument(
        "--skill-store",
        type=Path,
        default=Path("/var/lib/hermes/skills"),
        help="Path to skill store root (default: /var/lib/hermes/skills)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("HERMES_EVOLUTION_MODEL", "openai/gpt-4o-mini"),
        help="Model string for GEPA optimizer",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=int(os.environ.get("HERMES_EVOLUTION_STEPS", "5")),
        help="GEPA optimization steps per skill (default: 5)",
    )
    parser.add_argument(
        "--min-failures",
        type=int,
        default=2,
        help="Minimum failure count to evolve a skill (default: 2)",
    )
    parser.add_argument(
        "--engine",
        choices=["gepa", "heuristic"],
        default="gepa",
        help="Evolution engine to use (default: gepa; heuristic for testing without dspy)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    return parser


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=getattr(logging, level),
        stream=sys.stderr,
    )


def _build_engine(args: argparse.Namespace) -> object:
    if args.engine == "heuristic":
        from hermes.training.application.skill_evolution import (  # noqa: PLC0415
            HeuristicEvolutionEngine,
        )
        return HeuristicEvolutionEngine(min_failures=args.min_failures)

    # GEPA engine — fails loudly if dspy not installed
    from hermes.training.infrastructure.gepa_evolution_engine import (  # noqa: PLC0415
        DSPyNotInstalledError,
        GEPAConfig,
        GEPAEvolutionEngine,
    )
    try:
        config = GEPAConfig(model=args.model, max_steps=args.max_steps)
        return GEPAEvolutionEngine(config=config)
    except DSPyNotInstalledError as exc:
        logger.error("hermes.evolution.cli.dspy_missing: %s", exc)
        sys.exit(1)


def _proposal_to_json(proposal: object) -> str:
    """Serialize a SkillEvolutionProposal to JSON (one line)."""
    return json.dumps(
        {
            "proposal_id": str(proposal.proposal_id),  # type: ignore[union-attr]
            "skill_name": proposal.skill_name,  # type: ignore[union-attr]
            "tenant_id": str(proposal.tenant_id),  # type: ignore[union-attr]
            "generated_by": proposal.generated_by,  # type: ignore[union-attr]
            "rationale": proposal.rationale,  # type: ignore[union-attr]
            "proposed_skill_md": proposal.proposed_skill_md,  # type: ignore[union-attr]
            "_broker_submission": {
                "tool_name": "skill_manage",
                "parameters": {
                    "action": "edit",
                    "name": proposal.skill_name,  # type: ignore[union-attr]
                    "content": proposal.proposed_skill_md,  # type: ignore[union-attr]
                },
                "note": (
                    "Submit as ToolCallProposal(tool_name='skill_manage', ...) "
                    "to the capability broker with a valid HITL approval token."
                ),
            },
        },
        ensure_ascii=False,
    )


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    _setup_logging(args.log_level)

    try:
        tenant_id = UUID(args.tenant_id)
    except ValueError:
        logger.error("Invalid tenant-id: %r (must be a valid UUID)", args.tenant_id)
        sys.exit(1)

    engine = _build_engine(args)

    from hermes.training.application.skill_evolution import (  # noqa: PLC0415
        EvolutionOrchestrator,
    )

    orchestrator = EvolutionOrchestrator(
        engine=engine,
        audit_db_path=args.audit_db,
        skill_store_root=args.skill_store,
    )

    logger.info(
        "hermes.evolution.cli.start tenant=%s engine=%s db=%s store=%s",
        str(tenant_id)[:8],
        args.engine,
        args.audit_db,
        args.skill_store,
    )

    proposals = orchestrator.run_offline_pass(tenant_id=tenant_id)

    if not proposals:
        logger.info("hermes.evolution.cli.done proposals=0")
        return

    for proposal in proposals:
        print(_proposal_to_json(proposal))  # noqa: T201

    logger.info(
        "hermes.evolution.cli.done proposals=%d engine=%s",
        len(proposals),
        args.engine,
    )
    logger.info(
        "hermes.evolution.cli.next_step "
        "Submit each proposal to the capability broker with a HITL approval token. "
        "NEVER write SKILL.md files directly."
    )


if __name__ == "__main__":
    main()
