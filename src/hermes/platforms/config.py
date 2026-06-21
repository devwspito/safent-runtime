"""Platform module configuration — paths and runtime settings (T004).

No secrets in code. All sensitive values come from environment variables or
the daemon's key store.
"""

from __future__ import annotations

import os
from pathlib import Path

# Shell-state SQLite DB (WAL mode, single-writer daemon-owned).
# Matches the same path used by the agent registry and work queue.
SHELL_DB_PATH: Path = Path(
    os.environ.get("HERMES_SHELL_DB", "/var/lib/hermes/shell-state.db")
)

# Artifact store directory: encrypted at-rest transcripts, DOM snapshots.
# NEVER stores PII in clear text — only tokenized payloads referenced by hash.
# Residency: EU by default (GDPR). Override via HERMES_ARTIFACT_STORE.
ARTIFACT_STORE_PATH: Path = Path(
    os.environ.get("HERMES_ARTIFACT_STORE", "/var/lib/hermes/artifacts")
)

# Data residency marker — consumed by audit and compliance tools.
DATA_RESIDENCY: str = os.environ.get("HERMES_DATA_RESIDENCY", "EU")
