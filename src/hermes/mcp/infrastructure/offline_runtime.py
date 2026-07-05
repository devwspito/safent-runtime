"""Offline MCP runtime resolution — run the PREFETCHED bin directly, not `npx --offline`.

Root cause (2026-06-26, verified live): `npx --offline <pkg>` fails ENOTCACHED even
when the package is correctly installed and `npm install` warmed the shared cache —
npx cannot resolve the package PACKUMENT from a cache populated by `npm install` (a
known npm install↔npx cache-format mismatch). It reproduces on a FRESH cache, so it is
not a permissions issue. Running the installed bin directly DOES work:
`node <prefix>/node_modules/<pkg>/<bin>` → "Secure MCP Filesystem Server running on stdio".

Fix: the install-time prefetch installs each MCP package into a PERSISTENT per-package
prefix here; at connect time we rewrite an `npx <pkg> <args>` runtime argv into
`node <bin> <args>`. This keeps the cage IDENTICAL — still `node` inside the launcher's
netns+seccomp jail, still fully offline — it only skips npx's broken packument resolution.
Falls back to the original argv when the package was not prefetched (no worse than today).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("hermes.mcp.offline_runtime")

# Persistent per-package install root (under the safent-data volume, outside any cache).
MCP_INSTALL_ROOT = Path("/var/lib/hermes/mcp-installs")

# npx leading boolean flags to skip when locating the package spec in an argv.
_NPX_LEADING_FLAGS = frozenset(
    {"-y", "--yes", "-q", "--quiet", "--offline", "--prefer-offline"}
)


def npm_install_dir(name: str) -> Path:
    """Deterministic persistent install dir for an npm package coordinate.

    `@scope/pkg` → `<root>/npm/scope__pkg` (filesystem-safe, collision-free).
    """
    safe = name.lstrip("@").replace("/", "__")
    return MCP_INSTALL_ROOT / "npm" / safe


def _name_from_spec(spec: str) -> str:
    """Strip the version from an npm spec, preserving an `@scope/`.

    `@modelcontextprotocol/server-filesystem@2026.1.14` → `@modelcontextprotocol/server-filesystem`
    `lodash@4.17.4` → `lodash`
    """
    if spec.startswith("@"):
        at = spec.find("@", 1)
        return spec[:at] if at != -1 else spec
    at = spec.find("@")
    return spec[:at] if at != -1 else spec


def _resolve_npm_bin(name: str) -> Path | None:
    """Path to the package's entry bin inside its persistent install, or None.

    Reads `node_modules/<name>/package.json` → `bin` (str or dict; prefer the entry
    whose key matches the package basename) → falls back to `main`. The resolved path
    must stay inside the package dir (no traversal). Returns None when not prefetched.
    """
    pkgdir = (npm_install_dir(name) / "node_modules" / name).resolve()
    pj = pkgdir / "package.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    binf = data.get("bin")
    rel: str | None = None
    if isinstance(binf, str):
        rel = binf
    elif isinstance(binf, dict) and binf:
        base = name.rsplit("/", 1)[-1]
        rel = binf.get(base) or next(iter(binf.values()), None)
    if not rel:
        rel = data.get("main")
    if not isinstance(rel, str) or not rel:
        return None
    bp = (pkgdir / rel).resolve()
    try:
        bp.relative_to(pkgdir)  # reject `bin` pointing outside the package
    except ValueError:
        return None
    return bp if bp.is_file() else None


def resolve_runtime_argv(argv: list[str]) -> list[str]:
    """Rewrite an `npx <pkg> <args>` runtime argv to `node <bin> <args>` when prefetched.

    Only `npx` (npm) is rewritten; `uvx`/`node`/`python3` pass through unchanged. If the
    package has no persistent install (not prefetched / resolution failed), the original
    argv is returned — the launcher then falls back to its `npx --offline` path, no worse
    than before. Pure + side-effect-free; safe to call on every connect.
    """
    if not argv:
        return argv
    runner = argv[0].rsplit("/", 1)[-1]
    if runner != "npx":
        return argv
    rest = argv[1:]
    pkg: str | None = None
    args: list[str] = []
    for i, tok in enumerate(rest):
        if pkg is None and tok in _NPX_LEADING_FLAGS:
            continue
        if pkg is None and tok.startswith("-"):
            # an unrecognized option before the package spec — don't guess, pass through
            return argv
        pkg = tok
        args = rest[i + 1:]
        break
    if pkg is None:
        return argv
    name = _name_from_spec(pkg)
    bin_path = _resolve_npm_bin(name)
    if bin_path is None:
        return argv
    rewritten = ["node", str(bin_path), *args]
    logger.info(
        "hermes.mcp.offline_runtime.rewrote npx->%s bin=%s", "node", bin_path
    )
    return rewritten
