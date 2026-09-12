"""safent-ads companion (024/T091) — SPEND classification drift guard.

`tool_sensitivity._SAFENT_ADS_WRITE_TOOLS` is a hand-curated mirror of the
ads repo's OWN write-tool catalog
(`safent_ads.mcp.presentation.catalog._WRITE_CATALOG`). A hand-curated
mirror can silently rot the day the ads team adds a new write tool there
without a matching entry here — the new tool would fall through to "plain"
(no SPEND, no MFA tier) with nobody noticing.

This test reads the ADS REPO'S OWN `catalog.py` FILE at test time (never
imports `safent_ads` — it is a separate, uninstalled package) and asserts,
for every tool it currently declares:

  - every `_WRITE_CATALOG` name classifies SPEND under its qualified
    `mcp__safent-ads__<tool>` name.
  - every plain `_CATALOG` (read) name does NOT classify SPEND.

Cross-repo by design (024's companion lives in a sibling checkout) — skips
loudly with the reason when that checkout is not present, rather than
silently reporting a false pass.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from hermes.capabilities.tool_sensitivity import SensitivityCategory, sensitivity

pytestmark = pytest.mark.unit


def test_child_creation_proposal_is_spend_only_for_exact_ads_slug() -> None:
    assert SensitivityCategory.SPEND in sensitivity("mcp__safent-ads__propose_ad_child", {})
    assert SensitivityCategory.SPEND not in sensitivity("mcp__other__propose_ad_child", {})


_ADS_REPO = Path(
    os.environ.get("SAFENT_ADS_REPO", str(Path.home() / "Desktop" / "safent-ads"))
)
_ADS_CATALOG = _ADS_REPO / "src/safent_ads/mcp/presentation/catalog.py"


def _tool_names(source: str, var_name: str) -> tuple[str, ...]:
    """Extract the first-element string literal of every tuple assigned to
    *var_name* (`_CATALOG`/`_WRITE_CATALOG`'s `(name, description, args_model)`
    shape) — via the AST, never `import`, so this has zero dependency on the
    ads package being installed."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        target = _assigned_name(node)
        if target != var_name:
            continue
        value = node.value
        if not isinstance(value, ast.Tuple):
            continue
        names = []
        for elt in value.elts:
            if not isinstance(elt, ast.Tuple) or not elt.elts:
                continue
            first = elt.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.append(first.value)
        return tuple(names)
    raise AssertionError(f"{var_name} not found in {_ADS_CATALOG} — catalog.py changed shape?")


def _assigned_name(node: ast.AST) -> str | None:
    """`_CATALOG`/`_WRITE_CATALOG` are annotated module-level assignments
    (`_CATALOG: tuple[...] = (...)`, an `AnnAssign`) — plain `Assign` handled
    too in case that ever changes."""
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


@pytest.fixture(scope="module")
def ads_catalog_source() -> str:
    if not _ADS_CATALOG.is_file():
        pytest.skip(
            f"safent-ads checkout not found at {_ADS_REPO} "
            "(set SAFENT_ADS_REPO to override) — skipping the drift guard"
        )
    return _ADS_CATALOG.read_text()


@pytest.fixture(scope="module")
def ads_write_tool_names(ads_catalog_source: str) -> tuple[str, ...]:
    return _tool_names(ads_catalog_source, "_WRITE_CATALOG")


@pytest.fixture(scope="module")
def ads_read_tool_names(ads_catalog_source: str) -> tuple[str, ...]:
    return _tool_names(ads_catalog_source, "_CATALOG")


class TestNoDriftBetweenCatalogAndSpendClassification:
    def test_catalog_files_were_parsed_and_are_non_empty(
        self, ads_write_tool_names: tuple[str, ...], ads_read_tool_names: tuple[str, ...]
    ) -> None:
        """Guards the parser itself: an empty result would make every
        assertion below vacuously true."""
        assert len(ads_write_tool_names) > 0
        assert len(ads_read_tool_names) > 0

    def test_every_write_tool_in_the_real_catalog_is_spend(
        self, ads_write_tool_names: tuple[str, ...]
    ) -> None:
        for tool in ads_write_tool_names:
            qualified = f"mcp__safent-ads__{tool}"
            result = sensitivity(qualified, {})
            assert SensitivityCategory.SPEND in result, (
                f"{qualified!r} is in the ads catalog's _WRITE_CATALOG but is "
                "NOT classified SPEND — tool_sensitivity._SAFENT_ADS_WRITE_TOOLS "
                "has drifted from catalog.py, add it there"
            )

    def test_every_read_tool_in_the_real_catalog_is_not_spend(
        self, ads_read_tool_names: tuple[str, ...]
    ) -> None:
        for tool in ads_read_tool_names:
            qualified = f"mcp__safent-ads__{tool}"
            result = sensitivity(qualified, {})
            assert SensitivityCategory.SPEND not in result, (
                f"{qualified!r} is a READ tool in the ads catalog but is "
                "classified SPEND — check tool_sensitivity._SAFENT_ADS_WRITE_TOOLS"
            )

    def test_write_and_read_tool_names_never_overlap(
        self, ads_write_tool_names: tuple[str, ...], ads_read_tool_names: tuple[str, ...]
    ) -> None:
        """Sanity check on the source catalog itself — if this ever fails,
        the two assertions above would silently contradict each other."""
        assert set(ads_write_tool_names).isdisjoint(ads_read_tool_names)
