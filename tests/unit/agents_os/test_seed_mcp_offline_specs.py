"""Regresión: cada seed MCP baked tiene que arrancar OFFLINE en un primer boot.

Bug (imagen `feat/safent-next`): el seed `powerpoint` moría en el import antes
de emitir una sola línea de JSON-RPC —

    ModuleNotFoundError: No module named 'mcp.server.fastmcp'. This is mcp 2.x,
    where FastMCP was renamed to MCPServer …

`office-powerpoint-mcp-server` declara `mcp` sin techo, así que uv resolvía el
SDK 2.0 recién publicado y el paquete (escrito contra la API v1) reventaba. El
seed lleva `--with mcp<2`.

Follow-up (mismo `feat/safent-next`): ese `mcp<2` era el ÚNICO pin de los tres
seeds uvx — excel/word seguían resolviendo `mcp` desde lo que PyPI sirviera en
el momento del bake. Verificado leyendo sus entrypoints en la caché de uv de la
imagen (`localhost/safent-runtime:next`) y forzando la resolución:

  - `excel-mcp-server` (excel_mcp/server.py) importa el MISMO símbolo v1-only
    `mcp.server.fastmcp` que rompió powerpoint — `uv run --with 'mcp>=2,<3'
    excel-mcp-server stdio` reproduce el idéntico `ModuleNotFoundError`. Hoy
    resuelve mcp 1.30.0 por accidente (vía su dependencia `fastmcp`), sin nada
    que lo garantice — lleva ahora el MISMO `mcp<2` que powerpoint.
  - `office-word-mcp-server` (word_document_server/main.py) usa el paquete
    standalone `fastmcp` (`from fastmcp import FastMCP`) y solo toca `mcp` en
    crudo para `mcp.types.ToolAnnotations` (presente en 1.x Y 2.x) — arranca
    limpio contra mcp 2.2.0. Verificado 2.x-compatible ⇒ pin EXACTO
    `mcp==2.2.0` (la versión con la que se caldeó y se verificó), no un rango.

Invariantes que se fijan aquí:
  (1) todo seed uvx lleva un `--with mcp…` explícito — ninguno puede resolver
      su SDK `mcp` transitivo desde lo que PyPI sirva en el momento del bake.
  (2) el spec con el que el Containerfile CALIENTA la caché de uv debe ser
      idéntico, token a token, al argv del seed. uv cachea el entorno por sus
      requisitos y el arranque real añade `--offline`: cualquier diferencia
      entre los dos (una restricción en uno y no en el otro) es un fallo de
      caché en el primer boot, sin red para arreglarlo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_FILE = _REPO_ROOT / "ops" / "agents-os-edition" / "seed" / "mcp-servers.json"
_CONTAINERFILE = _REPO_ROOT / "ops" / "container" / "Containerfile"


def _seeds() -> list[dict]:
    return json.loads(_SEED_FILE.read_text(encoding="utf-8"))


def _warmed_specs() -> list[list[str]]:
    """Los `spec` del bucle `for spec in … ; do … uvx $spec` del Containerfile."""
    source = _CONTAINERFILE.read_text(encoding="utf-8")
    start = source.index('for spec in "--from excel-mcp-server==0.1.8')
    end = source.index("; do", start)
    return [spec.split() for spec in re.findall(r'"([^"]+)"', source[start:end])]


def _seed_argv(server_id: str) -> list[str]:
    return next(s["argv"] for s in _seeds() if s["server_id"] == server_id)


class TestPowerpointSeedPinsMcpV1:
    def test_powerpoint_argv_constrains_mcp_below_2(self) -> None:
        argv = _seed_argv("powerpoint")
        assert "--with" in argv
        assert argv[argv.index("--with") + 1] == "mcp<2"

    def test_constraint_comes_after_the_from_package(self) -> None:
        """El gate del scanner (`_scanner_can_analyze_argv`) resuelve el PRIMER
        `--from`/`--with` que encuentra: si `--with mcp<2` se colara delante,
        el argv se analizaría contra `mcp<2` en vez de contra el paquete."""
        argv = _seed_argv("powerpoint")
        assert argv.index("--from") < argv.index("--with")

    def test_scanner_still_accepts_the_constrained_argv(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _scanner_can_analyze_argv,
        )

        assert _scanner_can_analyze_argv(_seed_argv("powerpoint"))


class TestExcelSeedPinsMcpV1:
    """excel_mcp/server.py hace `from mcp.server.fastmcp import FastMCP` — el MISMO
    símbolo v1-only que rompió powerpoint. Hoy resuelve mcp 1.30.0 por accidente (vía
    su dependencia `fastmcp`); verificado que revienta idénticamente sin el pin
    (`uv run --with 'mcp>=2,<3' excel-mcp-server stdio` → el mismo ModuleNotFoundError)."""

    def test_excel_argv_constrains_mcp_below_2(self) -> None:
        argv = _seed_argv("excel")
        assert "--with" in argv
        assert argv[argv.index("--with") + 1] == "mcp<2"

    def test_excel_uses_audited_fastmcp_override(self) -> None:
        argv = _seed_argv("excel")
        assert argv[argv.index("--from") + 1] == "excel-mcp-server==0.1.8"
        assert argv[argv.index("--overrides") + 1] == (
            "/usr/share/hermes/seed/excel-mcp-overrides.txt"
        )
        override_path = (
            _REPO_ROOT
            / "ops"
            / "agents-os-edition"
            / "seed"
            / "excel-mcp-overrides.txt"
        )
        override = override_path.read_text(encoding="utf-8")
        assert "fastmcp==3.2.0" in override

    def test_constraint_comes_after_the_from_package(self) -> None:
        argv = _seed_argv("excel")
        assert "--from" in argv
        assert argv.index("--from") < argv.index("--with")

    def test_scanner_still_accepts_the_constrained_argv(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _scanner_can_analyze_argv,
        )

        assert _scanner_can_analyze_argv(_seed_argv("excel"))


class TestWordSeedPinsMcpV2Exact:
    """word_document_server/main.py usa el paquete standalone `fastmcp`
    (`from fastmcp import FastMCP`) y solo toca `mcp` en crudo para
    `mcp.types.ToolAnnotations` — presente en 1.x y 2.x por igual. Verificado
    2.x-compatible (arranca limpio contra mcp 2.2.0) ⇒ pin EXACTO, no un rango: el
    objetivo no es "cualquier 2.x" (sin verificar) sino la versión con la que se
    caldeó y se verificó."""

    def test_word_argv_pins_mcp_exact(self) -> None:
        argv = _seed_argv("word")
        assert "--with" in argv
        assert argv[argv.index("--with") + 1] == "mcp==2.2.0"

    def test_constraint_comes_after_the_from_package(self) -> None:
        argv = _seed_argv("word")
        assert argv.index("--from") < argv.index("--with")

    def test_scanner_still_accepts_the_pinned_argv(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _scanner_can_analyze_argv,
        )

        assert _scanner_can_analyze_argv(_seed_argv("word"))


class TestEverySeedCarriesAnExplicitMcpPin:
    """Invariante general: ningún seed uvx puede resolver su SDK `mcp` transitivo
    desde lo que PyPI sirva en el momento del bake — cada uno lleva un `--with
    mcp…` explícito, revisado. Un cuarto seed uvx que se añada sin este pin debe
    fallar AQUÍ, no en un boot en producción 120s después."""

    def test_every_seed_pins_mcp(self) -> None:
        for seed in _seeds():
            argv = seed["argv"]
            if argv[0] != "uvx":
                continue
            assert "--with" in argv, f"{seed['server_id']}: sin --with mcp<pin> en argv"
            pin = argv[argv.index("--with") + 1]
            assert pin.startswith("mcp"), (
                f"{seed['server_id']}: --with {pin!r} no fija el SDK mcp"
            )


class TestWarmedSpecsMatchSeedArgv:
    def test_every_seed_argv_is_warmed_verbatim(self) -> None:
        warmed = _warmed_specs()
        for seed in _seeds():
            argv = seed["argv"]
            assert argv[0] == "uvx", f"{seed['server_id']}: seed no-uvx sin calentar"
            assert argv[1:] in warmed, (
                f"{seed['server_id']}: el Containerfile calienta {warmed!r}, "
                f"que no incluye {argv[1:]!r} — `uvx --offline` no encontrará "
                "ese entorno en la caché en el primer boot"
            )
