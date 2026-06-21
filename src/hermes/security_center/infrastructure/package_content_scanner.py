"""PackageContentScanner — fetch + statically analyze the ACTUAL package contents.

This is the scanner that closes the "scan is theater" hole: the other scanners
only ever see the *name* of an MCP server / package (argv, identifier, source_url),
never its bytes. A malicious `npx -y evil-data-stealer-mcp` therefore used to score
a near-PASS because nothing inspected what the package actually does.

This scanner:
  1. Resolves the package coordinate from argv (npx → npm, uvx/pipx → pypi) or the
     identifier ("npm:foo", "pypi:bar"). Runners that execute a LOCAL script
     (node/python3) or argv that points at a local path (--from /opt/x, ./srv.js)
     resolve to None — there is no registry artifact to fetch, so the install gate
     rejects them (see `is_fetchable_argv`); nothing here can be made to PASS.
  2. Fetches the registry metadata and downloads the published tarball/wheel/sdist
     (bounded size + total time; the artifact is downloaded, never executed).
  3. Statically scans the extracted contents for:
       - install/postinstall lifecycle hooks (npm scripts; pypi setup.py / cmdclass),
       - import-time / module-top-level network + filesystem + process-exec +
         exfil patterns (the hallmarks of a credential/data stealer).

Security-first invariant (the whole point of C2): **absence of analysis must NOT
yield a high score.** If this scanner is asked to cover a target it understands
(an npm/pypi package) but cannot fetch or parse it, it emits a HIGH risk — a
package whose contents we could not inspect is treated as suspicious, not as clean.
It only stays silent for kinds it legitimately does not cover (e.g. composio_app,
or a local-path skill with no registry coordinate), so it never penalizes the
legitimate, already-analyzed paths.

It deliberately does NO subprocess execution of the package and NEVER runs its
install hooks — it reads files only. Network egress goes through the daemon's
normal proxied httpx (same as every other outbound call), so the egress jail
still applies.
"""

from __future__ import annotations

import io
import logging
import re
import shlex
import tarfile
import zipfile
from pathlib import Path

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Risk, Severity

logger = logging.getLogger("hermes.security_center.package_content")

# Bounds — a registry artifact for an MCP/skill is small; cap hard so a hostile
# (or merely huge) package can neither exhaust memory nor hang the gate.
_HTTP_TIMEOUT_S = 20.0
_MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024     # 40 MiB compressed
_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024  # 256 MiB (zip/tar bomb guard)
_MAX_SCANNED_FILES = 4_000
_MAX_FILE_SCAN_BYTES = 2 * 1024 * 1024     # only read first 2 MiB of any one file

_NPM_REGISTRY = "https://registry.npmjs.org"
_PYPI_JSON = "https://pypi.org/pypi"

# Runners that resolve to a *published, fetchable* package coordinate. These are
# the ONLY runners whose code this scanner can actually download and inspect.
#   npx → npm registry,  uvx/pipx → PyPI.
# `node`/`python3` are deliberately ABSENT: they execute a local script path that
# lives nowhere in a registry, so there is nothing to fetch and nothing to scan.
# The install gate must REJECT such argv (no analysis ⇒ no PASS), which is why
# this set is the single source of truth the gate consults via
# `PackageContentScanner.is_fetchable_argv`.
_NPM_RUNNERS = frozenset({"npx"})
_PYPI_RUNNERS = frozenset({"uvx", "pipx"})

# ── C2 PASS-5 — STRICT ARGV-SHAPE ALLOWLIST (replaces the per-variant blocklist) ─
# A blocklist of dangerous flags (--call/-c, --package/-p/--from/--with, …) is a
# losing game of whack-a-mole: every new interpreter-as-command form (`npx node -e`,
# `npx bash -c`, with or without --package) is another hole. The CLASS fix flips the
# polarity: an npx/uvx/pipx argv is fetchable+scannable ONLY if it matches an exact
# allowed SHAPE, and everything else is non-fetchable ⇒ REJECT.
#
# Allowed shape, after consuming OPTIONAL leading boolean flags (`-y`/`--yes` for npx;
# none required for uvx/pipx): the FIRST non-flag token must be a PUBLISHED PACKAGE
# SPEC ('[@scope/]name[@version]'). It must NOT be an interpreter/command (node, bash,
# sh, python, deno, …) and the argv must NOT carry any package-selecting / inline-exec
# option (--package/-p/--from/--with/--call/-c) BEFORE that first positional. Any token
# that runs code from somewhere other than that single published coordinate breaks the
# shape ⇒ non-fetchable. Trailing tokens AFTER the package spec are args to THAT package
# and do not affect fetchability. (uvx/pipx legitimately name the package via
# '--from <published-pkg>' / 'pipx run <pkg>'; only a LOCAL-PATH or git value there is
# off-registry — handled separately.)

# Interpreters / shells / language launchers that, as the first positional, mean the
# runner executes inline/off-registry code rather than a published package. Matched on
# the basename (so '/usr/bin/node' and 'node' both count). NOT exhaustive on purpose:
# the allowlist already requires a package SPEC shape; this set is a belt-and-braces
# rejection of the common interpreter names that also happen to be valid package-name
# shapes ('node', 'python', 'deno' are all legal npm/PyPI name shapes).
# All entries are lowercase; the membership test lowercases the token basename first,
# so the allowlist is case-insensitive (npx NODE -e / uvx PYTHON3 -c cannot bypass it).
_INTERPRETER_COMMANDS = frozenset({
    "node", "nodejs", "deno", "bun", "ts-node", "tsx",
    "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "busybox",
    "python", "python2", "python3", "py", "pypy", "pypy3",
    "ruby", "perl", "php", "lua", "rscript", "osascript",
    "env", "eval", "exec", "command", "nohup", "xargs", "time", "watch",
    "sudo", "gdb",
})

# Options that, appearing BEFORE the first positional, select WHERE the executed code
# comes from (a separate package, a local dir, an inline command string). Their presence
# before the package token breaks the strict shape — the first positional is then the
# COMMAND the runner executes (node/bash/…), not a fetchable coordinate. For uvx/pipx,
# '--from'/'--with' are the legitimate way to name a PUBLISHED package, so those two are
# validated by VALUE (published spec vs local/git) rather than blanket-rejected.
_PACKAGE_SOURCE_OPTS = frozenset({"--package", "-p", "--from", "--with"})
_INLINE_EXEC_OPTS = frozenset({"--call", "-c", "-e", "--eval", "--exec"})

# Boolean (value-less) flags each runner accepts BEFORE the package token. Only these may
# precede the first positional in a valid shape; ANY other leading option breaks it.
_NPX_LEADING_BOOL_FLAGS = frozenset({"-y", "--yes", "--quiet", "-q", "--prefer-offline", "--offline"})
_UVX_LEADING_BOOL_FLAGS = frozenset({"-q", "--quiet", "--offline", "--no-cache", "--native-tls"})

# npm package names and PyPI distribution names never contain a path separator
# (scoped npm names start with '@scope/' — handled explicitly). A token with a
# slash, a leading '.', or a filesystem-y shape is a LOCAL FILE, not a fetchable
# coordinate, and must not be mistaken for a registry package.
_LOCAL_PATH_HINT = re.compile(r"^[./~]|/|\\")

# Shape of a published package SPEC token: an optional '@scope/' segment, then a name,
# then an optional '@version' (or PEP 508 specifier for pypi). No path separators beyond
# the single scoped-name slash, no leading dot/tilde, no whitespace. A bare interpreter
# name ('node') matches this shape too — that is why _INTERPRETER_COMMANDS rejects them
# explicitly on top of the shape check.
_PKG_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_PKG_SPEC_RE = re.compile(
    rf"^(@{_PKG_NAME}/)?{_PKG_NAME}"          # [@scope/]name
    rf"([@=<>!~].*)?$"                          # optional @version / ==,>=,~=,… specifier
)
# Chars that begin a version / PEP 508 / extras suffix on a package name token.
_VERSION_SUFFIX_CHARS = "@=<>!~["


def _bare_pkg_name(tok: str) -> str:
    """Return the bare package NAME from a spec token (suffix + scope stripped).

    Strips the leading '@scope/' (keeping the basename after the slash) and cuts the
    token at the first version / PEP 508 / extras separator. So 'node@18' → 'node',
    'python3==1' → 'python3', '@scope/pkg@1.2' → 'pkg', 'requests[extras]' → 'requests'.
    """
    name = tok.rsplit("/", 1)[-1]  # drop '@scope/' (and any path-ish prefix)
    for idx, ch in enumerate(name):
        if ch in _VERSION_SUFFIX_CHARS:
            return name[:idx]
    return name

# Source-text patterns that indicate dangerous behavior if present at import time
# / module top level. We grep the whole file but only HIGH-flag when the match is
# not obviously inside a function the user must call. Static text matching is a
# heuristic — paired with the install-hook check it is a strong signal.
_EXFIL_PATTERNS: tuple[tuple[str, Severity, str], ...] = (
    # Process / shell execution.
    (r"child_process|execSync|spawnSync|\bexec\s*\(", Severity.HIGH, "process_exec"),
    (r"\bos\.system\s*\(|subprocess\.(Popen|call|run|check_output)", Severity.HIGH, "process_exec"),
    (r"\beval\s*\(|new\s+Function\s*\(|\bexec\s*\(", Severity.HIGH, "dynamic_eval"),
    # Outbound network from JS/PY at module scope (the stealer pattern).
    (r"https?://(?!registry\.npmjs\.org|pypi\.org|files\.pythonhosted\.org)", Severity.MEDIUM, "hardcoded_url"),
    (r"\brequire\(['\"](https?|node:http|http|https|net|dgram)['\"]\)", Severity.HIGH, "net_import"),
    (r"\bimport\s+(socket|urllib|http\.client|requests|ftplib|telnetlib)\b", Severity.MEDIUM, "net_import"),
    (r"fetch\s*\(|XMLHttpRequest|axios|got\(|node-fetch", Severity.MEDIUM, "net_call"),
    # Credential / secret harvesting.
    (r"process\.env|os\.environ", Severity.MEDIUM, "env_read"),
    (r"\.ssh|\.aws|\.npmrc|\.netrc|id_rsa|credentials|\.docker/config", Severity.HIGH, "cred_path"),
    (r"\.git-credentials|\.config/gcloud|\.kube/config|keychain", Severity.HIGH, "cred_path"),
    # Base64 blobs frequently used to smuggle payloads.
    (r"atob\s*\(|Buffer\.from\([^)]*['\"]base64|base64\.b64decode", Severity.MEDIUM, "obfuscation"),
)

_INSTALL_HOOK_KEYS = frozenset({"preinstall", "install", "postinstall", "prepare", "prepublish"})


class PackageContentScanner:
    """Downloads + statically analyzes the published artifact for a target.

    name="content" — gets its own policy weight so the score engine can deduct
    based on what was actually found inside the package.
    """

    name = "content"

    def __init__(self, *, http_client_factory=None) -> None:
        # Injectable for tests; defaults to the daemon's proxied httpx client.
        self._http_client_factory = http_client_factory

    async def scan(self, target: InstallTarget) -> list[Risk]:
        coord = self._resolve_coordinate(target)
        if coord is None:
            # Not a kind/shape we fetch (composio app, local skill, bare runner
            # with no package). Stay silent — do NOT manufacture a clean score.
            return []
        ecosystem, name, version = coord
        try:
            blob = await self._download_artifact(ecosystem, name, version)
        except _Unanalyzable as exc:
            # SECURITY-FIRST: we were asked to inspect a real package and could
            # not. Absence of analysis is treated as risk, never as PASS.
            return [Risk(
                category="content",
                severity=Severity.HIGH,
                message=(
                    f"No se pudo descargar/analizar el contenido de "
                    f"{ecosystem}:{name} ({exc}) — paquete no inspeccionable."
                ),
                evidence_ref=f"content:unanalyzable:{ecosystem}:{name}",
            )]
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.security.content_scan_error %s:%s: %s", ecosystem, name, exc)
            return [Risk(
                category="content",
                severity=Severity.HIGH,
                message=(
                    f"Error analizando el contenido de {ecosystem}:{name} ({exc}) — "
                    f"no se pudo verificar; tratado como riesgo."
                ),
                evidence_ref=f"content:error:{ecosystem}:{name}",
            )]
        return self._analyze_blob(ecosystem, name, blob)

    # ------------------------------------------------------------------
    # Coordinate resolution
    # ------------------------------------------------------------------

    def _resolve_coordinate(self, target: InstallTarget) -> tuple[str, str, str] | None:
        """Return (ecosystem, package_name, version) or None if not fetchable.

        ecosystem ∈ {"npm", "pypi"}.
        """
        return self.resolve_coordinate(target)

    @classmethod
    def resolve_coordinate(
        cls, target: InstallTarget
    ) -> tuple[str, str, str] | None:
        """Resolve a target to a fetchable (ecosystem, name, version) or None.

        Public + static so the install gate can ask, BEFORE connecting an MCP
        server, whether this scanner can actually fetch and inspect the code.
        None means "no analyzable coordinate" — for the gate that is a REJECT
        (no analysis ⇒ no PASS), not a silent allow.
        """
        # From explicit identifier like "npm:foo@1.2.3" / "pypi:bar".
        ident = (target.identifier or "").strip()
        for prefix, eco in (("npm:", "npm"), ("pypi:", "pypi"), ("pip:", "pypi")):
            if ident.lower().startswith(prefix):
                return cls._split_name_version(eco, ident[len(prefix):])

        # From argv (npx/uvx/pipx). The argv is fetchable ONLY if it matches the strict
        # shape (see _PKG_SPEC_RE / _INTERPRETER_COMMANDS); the helpers below return the
        # single published package SPEC or None. None ⇒ non-fetchable ⇒ REJECT.
        if target.argv:
            runner = target.argv[0].rsplit("/", 1)[-1]
            rest = target.argv[1:]
            if runner in _NPM_RUNNERS:
                pkg = cls._npm_package_from_argv(rest)
                return cls._split_name_version("npm", pkg) if pkg is not None else None
            if runner in _PYPI_RUNNERS:
                pkg = cls._pypi_package_from_argv(rest)
                return cls._split_name_version("pypi", pkg) if pkg is not None else None
            return None

        return None

    @classmethod
    def is_fetchable_argv(cls, argv: list[str]) -> bool:
        """True iff this argv resolves to a registry coordinate we can download.

        The install gate uses this as a hard precondition: an MCP whose code
        this scanner cannot fetch (a local `node script.js` / `python3 srv.py`,
        a runner outside npx/uvx/pipx, or an npx inline-exec flag like
        `--call`/`-c` that runs an arbitrary command with no package) is
        rejected — there is nothing to analyze, so it can never reach a PASS.
        """
        if not argv:
            return False
        target = InstallTarget(kind="mcp_server", identifier="argv-probe", argv=list(argv))
        return cls.resolve_coordinate(target) is not None

    # ── Strict argv-shape allowlist (C2 PASS-5) ───────────────────────────────────
    # The two helpers below return the SINGLE published package SPEC iff the argv
    # matches the allowed shape, else None. None ⇒ non-fetchable ⇒ the gate REJECTS.
    # The polarity is positive: we accept only the exact shape, so a NEW exploit form
    # (a yet-unseen interpreter, a new option) fails by DEFAULT (no special-casing).

    @classmethod
    def _is_published_pkg_spec(cls, tok: str) -> bool:
        """True iff `tok` is a published-package SPEC and NOT an interpreter/command.

        A package spec is '[@scope/]name[@version]' (no path separators beyond the one
        scoped-name slash, no leading dot/tilde). An interpreter basename ('node',
        'python3', 'bash') is rejected even though it matches the name shape, because as
        the first positional it means the runner executes off-registry inline code.
        """
        if not tok or _LOCAL_PATH_HINT.search(tok) and not tok.startswith("@"):
            return False
        # Strip the version / PEP 508 / extras suffix FIRST, then run the interpreter
        # check on the BARE name. Otherwise 'node@18' has basename 'node@18' (not 'node')
        # and slips through, letting npx fetch the real 'node' package and exec inline code.
        bare = _bare_pkg_name(tok)
        if bare.lower() in _INTERPRETER_COMMANDS:
            return False
        return _PKG_SPEC_RE.match(tok) is not None

    @classmethod
    def _npm_package_from_argv(cls, rest: list[str]) -> str | None:
        """npx strict shape → package spec, or None.

        Valid: optional leading boolean flags (-y/--yes/--quiet/--offline/…), then the
        FIRST non-flag token, which MUST be a published package spec. No package-source
        option (--package/-p/--from/--with) and no inline-exec option (--call/-c/-e) may
        precede it. Anything else breaks the shape ⇒ None. Trailing tokens after the
        package spec are args to that package and are not inspected.
        """
        for tok in rest:
            if tok.startswith("-"):
                base = tok.split("=", 1)[0]
                if base in _PACKAGE_SOURCE_OPTS or base in _INLINE_EXEC_OPTS:
                    return None  # selects/exec off-registry code before any package
                if tok in _NPX_LEADING_BOOL_FLAGS:
                    continue     # value-less npx own flag — keep scanning
                return None      # any other leading option breaks the strict shape
            # First non-flag token: it MUST be the package spec.
            return tok if cls._is_published_pkg_spec(tok) else None
        return None

    @classmethod
    def _pypi_package_from_argv(cls, rest: list[str]) -> str | None:
        """uvx/pipx strict shape → package spec, or None.

        Valid forms:
          • optional leading boolean flags, then first positional = published pkg spec
            ('uvx pkg', 'pipx run pkg' — 'run' is consumed as a leading bool token);
          • '--from <published-pkg>' / '--with <published-pkg>' naming the package by
            VALUE (the legitimate uvx/pipx way), as long as the value is a published
            spec — a LOCAL-PATH or git value there is off-registry ⇒ None.
        Inline-exec options (-c/-e/--exec), '-p/--package', interpreter positionals, and
        any other leading option break the shape ⇒ None.
        """
        i = 0
        n = len(rest)
        while i < n:
            tok = rest[i]
            if not tok.startswith("-"):
                # 'pipx run pkg': 'run' is a subcommand, not the package — skip once.
                if tok == "run":
                    i += 1
                    continue
                return tok if cls._is_published_pkg_spec(tok) else None
            base, eq, inline_val = tok.partition("=")
            if base in ("--from", "--with"):
                val = inline_val if eq else (rest[i + 1] if i + 1 < n else "")
                # The value names the package by spec; must be a published coordinate.
                return val if cls._is_published_pkg_spec(val) else None
            if base in _INLINE_EXEC_OPTS or base in ("-p", "--package", "-e", "--editable"):
                return None  # off-registry / inline-exec selector before any package
            if tok in _UVX_LEADING_BOOL_FLAGS:
                i += 1
                continue
            return None  # any other leading option breaks the strict shape
        return None

    @staticmethod
    def _split_name_version(eco: str, spec: str) -> tuple[str, str, str] | None:
        spec = spec.strip()
        if not spec:
            return None
        # A filesystem path (./srv.js, /opt/x.py, ../foo, ~/bar) is not a fetchable
        # registry coordinate — never treat it as one. Scoped npm names ('@scope/n')
        # are the sole legitimate use of '/' and are handled just below.
        if not spec.startswith("@") and _LOCAL_PATH_HINT.search(spec):
            return None
        # Scoped npm packages: @scope/name@version.
        if spec.startswith("@"):
            at = spec.rfind("@")
            if at > 0:
                return (eco, spec[:at], spec[at + 1:])
            return (eco, spec, "")
        if eco == "pypi":
            m = re.split(r"==|>=|<=|~=|!=|>|<|@", spec, maxsplit=1)
            return (eco, m[0].strip(), (m[1].strip() if len(m) > 1 else ""))
        if "@" in spec:
            name, _, ver = spec.partition("@")
            return (eco, name, ver)
        return (eco, spec, "")

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _client(self):
        if self._http_client_factory is not None:
            return self._http_client_factory()
        import httpx  # noqa: PLC0415
        return httpx.Client(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    async def _download_artifact(self, eco: str, name: str, version: str) -> bytes:
        """Resolve the tarball URL from the registry and download it (bounded)."""
        if eco == "npm":
            url = self._npm_tarball_url(name, version)
        else:
            url = self._pypi_tarball_url(name, version)
        return self._download(url)

    def _npm_tarball_url(self, name: str, version: str) -> str:
        import httpx  # noqa: PLC0415
        meta_url = f"{_NPM_REGISTRY}/{name}"
        with self._client() as client:
            try:
                resp = client.get(meta_url)
            except httpx.HTTPError as exc:
                raise _Unanalyzable(f"registry npm inalcanzable: {exc}") from exc
        if resp.status_code != 200:
            raise _Unanalyzable(f"npm metadata HTTP {resp.status_code}")
        meta = resp.json()
        ver = version or (meta.get("dist-tags") or {}).get("latest") or ""
        versions = meta.get("versions") or {}
        entry = versions.get(ver)
        if entry is None and versions:
            # fall back to the newest declared version
            ver = sorted(versions.keys())[-1]
            entry = versions.get(ver)
        if not entry:
            raise _Unanalyzable("versión npm no encontrada en el registro")
        tarball = ((entry.get("dist") or {}).get("tarball")) or ""
        if not tarball.startswith("https://"):
            raise _Unanalyzable("tarball npm sin URL https")
        return tarball

    def _pypi_tarball_url(self, name: str, version: str) -> str:
        import httpx  # noqa: PLC0415
        meta_url = (
            f"{_PYPI_JSON}/{name}/{version}/json" if version
            else f"{_PYPI_JSON}/{name}/json"
        )
        with self._client() as client:
            try:
                resp = client.get(meta_url)
            except httpx.HTTPError as exc:
                raise _Unanalyzable(f"PyPI inalcanzable: {exc}") from exc
        if resp.status_code != 200:
            raise _Unanalyzable(f"PyPI metadata HTTP {resp.status_code}")
        urls = (resp.json().get("urls")) or []
        # Prefer sdist (real source) so we can read setup.py / module top level.
        sdist = next((u for u in urls if u.get("packagetype") == "sdist"), None)
        chosen = sdist or (urls[0] if urls else None)
        if not chosen:
            raise _Unanalyzable("PyPI no expone artefactos descargables")
        url = chosen.get("url") or ""
        if not url.startswith("https://"):
            raise _Unanalyzable("artefacto PyPI sin URL https")
        return url

    def _download(self, url: str) -> bytes:
        import httpx  # noqa: PLC0415
        buf = io.BytesIO()
        with self._client() as client:
            try:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise _Unanalyzable(f"descarga HTTP {resp.status_code}")
                    for chunk in resp.iter_bytes(64 * 1024):
                        buf.write(chunk)
                        if buf.tell() > _MAX_DOWNLOAD_BYTES:
                            raise _Unanalyzable("artefacto excede el tope de descarga")
            except httpx.HTTPError as exc:
                raise _Unanalyzable(f"descarga falló: {exc}") from exc
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Static analysis
    # ------------------------------------------------------------------

    def _analyze_blob(self, eco: str, name: str, blob: bytes) -> list[Risk]:
        try:
            files = self._extract(blob)
        except _Unanalyzable as exc:
            return [Risk(
                category="content",
                severity=Severity.HIGH,
                message=f"No se pudo extraer {eco}:{name} ({exc}) — no inspeccionable.",
                evidence_ref=f"content:extract_failed:{eco}:{name}",
            )]
        if not files:
            return [Risk(
                category="content",
                severity=Severity.HIGH,
                message=f"Artefacto {eco}:{name} vacío o ilegible — no inspeccionable.",
                evidence_ref=f"content:empty:{eco}:{name}",
            )]
        risks: list[Risk] = []
        risks.extend(self._check_install_hooks(eco, files))
        risks.extend(self._check_source_patterns(files))
        return risks

    def _extract(self, blob: bytes) -> dict[str, bytes]:
        """Return {path: first-2MiB-bytes}. Guards against tar/zip bombs."""
        out: dict[str, bytes] = {}
        total = 0
        if zipfile.is_zipfile(io.BytesIO(blob)):
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or len(out) >= _MAX_SCANNED_FILES:
                        continue
                    total += info.file_size
                    if total > _MAX_UNCOMPRESSED_BYTES:
                        raise _Unanalyzable("descompresión excede el tope (posible bomba)")
                    if not self._is_scannable(info.filename):
                        continue
                    with zf.open(info) as fh:
                        out[info.filename] = fh.read(_MAX_FILE_SCAN_BYTES)
            return out
        try:
            tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:*")
        except tarfile.TarError as exc:
            raise _Unanalyzable(f"formato no reconocido: {exc}") from exc
        with tf:
            for member in tf:
                if not member.isfile() or len(out) >= _MAX_SCANNED_FILES:
                    continue
                total += member.size
                if total > _MAX_UNCOMPRESSED_BYTES:
                    raise _Unanalyzable("descompresión excede el tope (posible bomba)")
                if not self._is_scannable(member.name):
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                out[member.name] = fh.read(_MAX_FILE_SCAN_BYTES)
        return out

    @staticmethod
    def _is_scannable(path: str) -> bool:
        # path traversal guard for member names (we never write to disk, but be safe).
        if path.startswith("/") or ".." in Path(path).parts:
            return False
        low = path.lower()
        return low.endswith((
            ".js", ".cjs", ".mjs", ".ts", ".json",
            ".py", ".pyi", ".sh", ".cfg", ".toml", ".ini",
        )) or low.endswith("package.json") or low.endswith("setup.py")

    def _check_install_hooks(self, eco: str, files: dict[str, bytes]) -> list[Risk]:
        risks: list[Risk] = []
        if eco == "npm":
            risks.extend(self._npm_hooks(files))
        else:
            risks.extend(self._pypi_hooks(files))
        return risks

    def _npm_hooks(self, files: dict[str, bytes]) -> list[Risk]:
        import json  # noqa: PLC0415
        for path, data in files.items():
            base = path.rsplit("/", 1)[-1]
            if base != "package.json":
                continue
            try:
                pkg = json.loads(data.decode("utf-8", "replace"))
            except (ValueError, TypeError):
                continue
            scripts = pkg.get("scripts") or {}
            if not isinstance(scripts, dict):
                continue
            hooks = sorted(_INSTALL_HOOK_KEYS & set(scripts.keys()))
            if hooks:
                body = "; ".join(f"{h}={scripts[h]!r}" for h in hooks)
                return [Risk(
                    category="content",
                    severity=Severity.CRITICAL,
                    message=(
                        f"npm package declara hook(s) de instalación que se ejecutan "
                        f"sin intervención: {body[:300]}"
                    ),
                    evidence_ref=f"content:npm_install_hook:{','.join(hooks)}",
                )]
        return []

    def _pypi_hooks(self, files: dict[str, bytes]) -> list[Risk]:
        for path, data in files.items():
            base = path.rsplit("/", 1)[-1]
            if base != "setup.py":
                continue
            text = data.decode("utf-8", "replace")
            # setup.py runs arbitrary Python at install time; custom cmdclass /
            # install subclasses are the pypi equivalent of postinstall.
            if re.search(r"cmdclass\s*=|class\s+\w+\(install\)|class\s+\w+\(.*PostInstall", text):
                return [Risk(
                    category="content",
                    severity=Severity.CRITICAL,
                    message=(
                        "setup.py define un cmdclass/install personalizado — código "
                        "que se ejecuta al instalar (hook de instalación pypi)."
                    ),
                    evidence_ref="content:pypi_install_hook:setup_cmdclass",
                )]
            if re.search(r"os\.system|subprocess|urllib|requests|socket", text):
                return [Risk(
                    category="content",
                    severity=Severity.HIGH,
                    message="setup.py invoca red/proceso al instalar (ejecución no interactiva).",
                    evidence_ref="content:pypi_install_hook:setup_side_effect",
                )]
        return []

    def _check_source_patterns(self, files: dict[str, bytes]) -> list[Risk]:
        risks: list[Risk] = []
        seen: set[str] = set()
        for path, data in files.items():
            if path.rsplit("/", 1)[-1] in ("package.json",):
                continue
            text = data.decode("utf-8", "replace")
            for pattern, severity, tag in _EXFIL_PATTERNS:
                if tag in seen:
                    continue
                if re.search(pattern, text):
                    seen.add(tag)
                    risks.append(Risk(
                        category="content",
                        severity=severity,
                        message=f"Patrón sospechoso '{tag}' en {path[:120]}",
                        evidence_ref=f"content:pattern:{tag}",
                    ))
        return risks


class _Unanalyzable(RuntimeError):
    """Raised internally when a coverable package cannot be fetched/parsed.

    Surfaces as a HIGH risk — absence of analysis is never a clean score.
    """
