"""Terminal install-intent detection — closes the Security Center side-door.

The Security Center gates the OFFICIAL install channels (install_hub_skill,
add_mcp_server, install_package). But an agent can also install software FROM THE
TERMINAL — `pip install X`, `npm i X`, `curl URL | sh`, `git clone … && make` —
which bypasses that scan. This module recognises install-shaped terminal commands
and extracts WHAT is being installed and WHERE FROM, so the same scan→score→gate
can be applied before execution.

Pure domain logic: argv in → InstallIntent | None out. No I/O, no scanning here.
The egress jail already controls the SOURCE DOMAIN at the kernel; this adds the
review of the SPECIFIC target (provenance/score/audit) via the Security Center.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Registry → trusted source URL used for the provenance check.
_PYPI = "https://pypi.org"
_NPM = "https://registry.npmjs.org"
_RUBYGEMS = "https://rubygems.org"
_CRATES = "https://crates.io"

# A URL anywhere in a token (for curl|sh / git clone / pip --index-url).
_URL_RE = re.compile(r"https?://[^\s'\"|;&)]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class InstallIntent:
    """A recognised software-install action extracted from a terminal command.

    ecosystem:  "pip" | "npm" | "apt" | "dnf" | "gem" | "cargo" | "go" |
                "git" | "remote-script" — for UX / scanner selection.
    identifier: human-readable target (package name or URL).
    source_url: origin for the provenance check (registry or explicit URL).
    """

    ecosystem: str
    identifier: str
    source_url: str


def _basename(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def _first_positional(args: list[str]) -> str | None:
    """First non-flag token (and not a flag's value we know takes one)."""
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a.startswith("-"):
            # --index-url URL / -i URL take a following value.
            if a in ("-i", "--index-url", "--extra-index-url", "-r", "--requirement"):
                skip_next = True
            continue
        return a
    return None


def _index_url(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a in ("-i", "--index-url") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--index-url="):
            return a.split("=", 1)[1]
    return None


def detect_install_intent(argv: list[str]) -> InstallIntent | None:  # noqa: C901, PLR0911, PLR0912
    """Classify *argv* as a software install, or return None.

    Conservative: only flags commands that clearly fetch+install external code.
    A false negative (missed install) degrades to the egress jail + broker HITL;
    a false positive would needlessly gate a benign command, so we stay tight.
    """
    if not argv:
        return None
    cmd = _basename(argv[0])
    rest = argv[1:]

    # python -m pip install … → treat as pip
    if cmd in ("python", "python3") and len(rest) >= 2 and rest[0] == "-m" and rest[1] in ("pip", "pipx"):
        cmd, rest = rest[1], rest[2:]

    # pip / pip3 / pipx install <pkg>
    if cmd in ("pip", "pip3", "pipx"):
        if rest and rest[0] == "install":
            pkg = _first_positional(rest[1:])
            if pkg:
                src = _index_url(rest[1:]) or _PYPI
                return InstallIntent(ecosystem="pip", identifier=pkg, source_url=src)
        return None

    # npm install/i/add <pkg>  (skip bare `npm install` = local deps from package.json)
    if cmd in ("npm", "pnpm", "yarn"):
        if rest and rest[0] in ("install", "i", "add"):
            pkg = _first_positional(rest[1:])
            if pkg and not pkg.startswith(".") and pkg not in (".", "..", "*"):
                return InstallIntent(ecosystem="npm", identifier=pkg, source_url=_NPM)
        return None

    # npx <pkg> — fetches + runs a package
    if cmd == "npx":
        pkg = _first_positional(rest)
        if pkg:
            return InstallIntent(ecosystem="npm", identifier=pkg, source_url=_NPM)
        return None

    # system package managers
    if cmd in ("apt", "apt-get", "dnf", "yum", "zypper", "apk"):
        if rest and rest[0] in ("install", "add"):
            pkg = _first_positional(rest[1:])
            if pkg:
                return InstallIntent(ecosystem=cmd, identifier=pkg, source_url=f"{cmd}://system")
        return None

    if cmd == "gem" and rest[:1] == ["install"]:
        pkg = _first_positional(rest[1:])
        if pkg:
            return InstallIntent(ecosystem="gem", identifier=pkg, source_url=_RUBYGEMS)
        return None

    if cmd == "cargo" and rest[:1] == ["install"]:
        pkg = _first_positional(rest[1:])
        if pkg:
            return InstallIntent(ecosystem="cargo", identifier=pkg, source_url=_CRATES)
        return None

    if cmd == "go" and rest[:2] == ["install", ]:  # `go install pkg@ver`
        pkg = _first_positional(rest[1:])
        if pkg:
            return InstallIntent(ecosystem="go", identifier=pkg, source_url="https://" + pkg.split("@")[0])
        return None

    # git clone <url>
    if cmd == "git" and rest[:1] == ["clone"]:
        url = next((a for a in rest[1:] if not a.startswith("-")), None)
        if url:
            return InstallIntent(ecosystem="git", identifier=url, source_url=url)
        return None

    # curl/wget … | sh   (the dangerous remote-script pattern). The pipe is a shell
    # construct, so this arrives as `sh -c "<script>"` / `bash -c "…"`. Scan the
    # script string for a fetch+exec shape.
    if cmd in ("sh", "bash", "zsh", "dash") and "-c" in rest:
        idx = rest.index("-c")
        script = rest[idx + 1] if idx + 1 < len(rest) else ""
        if re.search(r"\b(curl|wget)\b", script) and re.search(r"\|\s*(sh|bash|zsh)\b", script):
            m = _URL_RE.search(script)
            url = m.group(0) if m else "remote-script"
            return InstallIntent(ecosystem="remote-script", identifier=url, source_url=url)
        return None

    return None
