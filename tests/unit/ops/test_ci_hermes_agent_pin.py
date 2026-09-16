"""CI ha estado roja desde 2026-07-10 (spec 025, hallazgo #1):
`pyproject.toml` fija `hermes-agent==0.21.1` como dependencia dura pero ese
paquete NO está en PyPI (máximo publicado 0.19.0) — solo vive en el tarball de
GitHub que `ops/container/Containerfile` instala del commit fijado. El job
`unit-tests` del workflow hacía `pip install -e .` a secas y 404-eaba.

Estas pruebas fijan, sin red ni contenedor, que:
  - existe una única fuente de verdad para el commit (`ops/hermes-agent.lock`),
  - el Containerfile la lee (no vuelve a hardcodear el SHA), y
  - el workflow instala el tarball editable ANTES de `pip install -e .`, leyendo
    la MISMA fuente — así el pin de pyproject.toml se resuelve como ya
    satisfecho en vez de ir a buscarlo a PyPI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOCK_FILE = _REPO_ROOT / "ops/hermes-agent.lock"
_CONTAINERFILE = _REPO_ROOT / "ops/container/Containerfile"
_WORKFLOW = _REPO_ROOT / ".github/workflows/agents-os-edition.yml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _lock_commit() -> str:
    return _LOCK_FILE.read_text(encoding="utf-8").strip()


def test_lock_file_has_exactly_one_pinned_commit_sha() -> None:
    text = _LOCK_FILE.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1, f"ops/hermes-agent.lock debe tener una sola línea, tiene {lines!r}"
    assert _SHA_RE.match(lines[0]), f"no parece un commit SHA de git: {lines[0]!r}"


def test_pyproject_still_pins_hermes_agent_hard() -> None:
    # Si esto deja de ser cierto, la instalación especial del tarball ya no
    # hace falta y este test (y el fix) deben revisarse, no solo el pin.
    text = _PYPROJECT.read_text(encoding="utf-8")
    assert '"hermes-agent==0.21.1"' in text


def test_containerfile_reads_the_lock_file_not_a_hardcoded_sha() -> None:
    text = _CONTAINERFILE.read_text(encoding="utf-8")
    assert "ops/hermes-agent.lock" in text
    commit = _lock_commit()
    # El SHA solo debe aparecer una vez en el Containerfile: como valor leído
    # del lock, no como segunda copia hardcodeada (p.ej. un ARG con default).
    assert text.count(commit) == 0, (
        "el Containerfile hardcodea el commit en vez de leerlo de "
        "ops/hermes-agent.lock — eso es la duplicación que causó la deriva"
    )
    assert "COPY ops/hermes-agent.lock" in text
    assert 'HERMES_AGENT_COMMIT="$(tr -d \' \\t\\n\\r\' < /tmp/hermes-agent.lock)"' in text


def test_workflow_install_steps_read_the_same_lock_file() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    commit = _lock_commit()
    assert text.count(commit) == 0, (
        "el workflow hardcodea el commit en vez de leerlo de "
        "ops/hermes-agent.lock"
    )
    occurrences = text.count("ops/hermes-agent.lock")
    assert occurrences >= 2, (
        "esperaba que unit-tests E integration-tests lean el lock file "
        f"(encontrado {occurrences} veces)"
    )


def test_workflow_installs_hermes_agent_tarball_before_pip_install_dash_e_dot() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    for job_name in ("unit-tests", "integration-tests"):
        steps = jobs[job_name]["steps"]
        install_step = next(
            (s for s in steps if s.get("name") == "Install"), None
        )
        assert install_step is not None, f"{job_name}: falta el paso 'Install'"
        run = install_step["run"]

        editable_agent_idx = run.find("pip install -e /tmp/hermes-agent-src")
        project_install_idx = run.find("pip install -e . pytest")
        assert editable_agent_idx != -1, (
            f"{job_name}: no instala hermes-agent editable desde el tarball fijado"
        )
        assert project_install_idx != -1, (
            f"{job_name}: no instala el proyecto (pip install -e .)"
        )
        assert editable_agent_idx < project_install_idx, (
            f"{job_name}: instala el proyecto ANTES que hermes-agent — el pin "
            "hermes-agent==0.21.1 se resolvería contra PyPI (404) en vez de "
            "contra el editable ya instalado"
        )
        assert "ops/hermes-agent.lock" in run
