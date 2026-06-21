"""security_hook — pre/post_tool_call hooks that gate ALL Hermes tool calls.

Registered at engine startup via hermes_cli.plugins.get_plugin_manager()._hooks.
Once registered, every tool call dispatched by hermes-agent passes through
get_pre_tool_call_block_message(), which calls invoke_hook("pre_tool_call", ...)
on all registered callbacks before execution.

Gate order (fail-closed — a single BLOCK from any step short-circuits):
  1. Kill-switch  — broker's AgentStatePort.is_paused() → block if paused.
  2. Native hardline floor — detect_hardline_command() from tools.approval.
  3. Self-jailbreak guard  — _check_terminal_self_jailbreak() blocks any
     terminal command that would stop/disable/mask/kill/rm a protected
     Hermes service or its filesystem artefacts. Terminal, inapelable.
  4. Native command guards — check_all_command_guards() for terminal tools.
  5. Native code guard   — check_execute_code_guard() for execute_code.
  6. Denylist gate       — broker._check_denylist() for os_native service ops.

Post-execution audit:
  post_tool_call hook signs every outcome (allowed or denied) into the
  AuditHashChainSigner + audit repo that the engine was configured with.
  Signing is non-fatal: audit failures are logged at ERROR but never block
  execution (the chain is WORM-append-only; a missing entry is detected by
  integrity verification, not by crashing the agent loop).

Thread-safety:
  run_conversation executes in a thread-pool executor; the hooks run on that
  same thread. Any async call (broker.dispatch, signer.append_and_persist)
  must be bridged via asyncio.run_coroutine_threadsafe with the engine_loop.

Capa: runtime (infrastructure-facing). Depends on hermes-agent (lazy imports)
and on the application/infrastructure layers of hermes-runtime. NEVER imported
by domain or application layers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hermes.agents_os.application.audit_hash_chain import (
        AuditHashChainSigner,
        AuditKind,
    )
    from hermes.capabilities.domain.ports import SignedAuditRepositoryPort
    from hermes.tasks.domain.ports import AgentStatePort

logger = logging.getLogger("hermes.runtime.security_hook")

# Timeout for async bridges (kill-switch check + audit persist).
_ASYNC_BRIDGE_TIMEOUT_S: float = 5.0

# Terminal tool names as reported to the hook.
_TERMINAL_TOOLS: frozenset[str] = frozenset({"run_command", "run_terminal", "terminal"})
# Execute-code tool names.
_CODE_TOOLS: frozenset[str] = frozenset({"execute_code", "run_code"})


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}


def _run_async_bridge(
    coro: "Any",
    engine_loop: "asyncio.AbstractEventLoop",
    timeout: float = _ASYNC_BRIDGE_TIMEOUT_S,
) -> Any:
    """Run an async coroutine from a sync thread via the engine's event loop.

    Returns the coroutine result, or raises TimeoutError / Exception on failure.
    """
    future = asyncio.run_coroutine_threadsafe(coro, engine_loop)
    return future.result(timeout=timeout)


def _check_kill_switch(
    agent_state: "AgentStatePort",
    engine_loop: "asyncio.AbstractEventLoop",
) -> bool:
    """Return True if the agent is paused (kill-switch active)."""
    try:
        return _run_async_bridge(agent_state.is_paused(), engine_loop)
    except Exception as exc:
        # Fail-closed: if we can't check the kill-switch, treat as paused.
        logger.error(
            "hermes.security_hook.kill_switch_check_failed error=%r — blocking (fail-closed)",
            exc,
        )
        return True


def _check_hardline_native(command_str: str) -> str | None:
    """Check detect_hardline_command (native hermes-agent floor). Returns block msg or None."""
    try:
        from tools.approval import detect_hardline_command  # noqa: PLC0415

        is_hardline, description = detect_hardline_command(command_str)
        if is_hardline:
            return (
                f"BLOCKED (hardline native floor): {description or 'unconditional blocklist'}"
            )
    except ImportError:
        pass
    return None


def _check_command_guards_native(command_str: str) -> str | None:
    """Check check_all_command_guards (native hermes-agent guards). Returns block msg or None."""
    try:
        from tools.approval import check_all_command_guards  # noqa: PLC0415

        result = check_all_command_guards(command_str, "local")
        if not result.get("approved", True):
            return result.get("message") or "BLOCKED by native command guard"
    except ImportError:
        pass
    return None


def _check_code_guard_native(code_str: str) -> str | None:
    """Check check_execute_code_guard (native hermes-agent guard). Returns block msg or None."""
    try:
        from tools.approval import check_execute_code_guard  # noqa: PLC0415

        result = check_execute_code_guard(code_str, "local")
        if not result.get("approved", True):
            return result.get("message") or "BLOCKED by native code guard"
    except ImportError:
        pass
    return None


def _extract_command_str(tool_name: str, args: dict[str, Any]) -> str | None:
    """Extract a command string from tool args for terminal-type tools."""
    if tool_name not in _TERMINAL_TOOLS:
        return None
    cmd = args.get("command") or args.get("argv") or args.get("cmd")
    if isinstance(cmd, list):
        return " ".join(str(t) for t in cmd)
    if isinstance(cmd, str):
        return cmd
    return None


def _extract_code_str(tool_name: str, args: dict[str, Any]) -> str | None:
    """Extract code string from tool args for execute_code-type tools."""
    if tool_name not in _CODE_TOOLS:
        return None
    return args.get("code") or args.get("source")


# ---------------------------------------------------------------------------
# Self-jailbreak guard — terminal commands that silence the security kernel
# ---------------------------------------------------------------------------

# systemctl sub-commands that MUTATE service state (stop, disable the service).
# Read-only sub-commands (status, is-active, show, list-units) are NOT in this
# set and will pass through unhindered.
_SYSTEMCTL_MUTATION_VERBS: frozenset[str] = frozenset({
    "stop",
    "disable",
    "mask",
    "kill",
    "restart",
    "try-restart",
    "reload-or-restart",
    "force-reload",
    "isolate",
    "halt",
    "poweroff",
    "reboot",
})

# Signal-send / process-kill binaries that can murder the kernel processes.
_PROCESS_KILL_BINARIES: frozenset[str] = frozenset({"kill", "pkill", "killall", "sigkill"})

# Command-prefix wrappers that re-exec the real command as a later argument.
# A self-jailbreak hides behind these (e.g. `sudo systemctl disable hermes-runtime`,
# `env systemctl stop hermes-runtime`). We must peel them off the FRONT of the
# token list — together with their own flags and VAR=val assignments — to reach
# the binary that actually mutates the protected service.
_COMMAND_PREFIX_BINARIES: frozenset[str] = frozenset({
    "sudo",
    "doas",
    "su",
    "env",
    "nice",
    "ionice",
    "nohup",
    "setsid",
    "stdbuf",
    "timeout",
    "time",
    "command",
    "exec",
    "xargs",
    "watch",
    "chroot",
    "unbuffer",
    "runuser",
    "busybox",
})

# Shells that re-exec a payload via `-c`.
_SHELL_BINARIES: frozenset[str] = frozenset({"bash", "sh", "dash", "zsh", "ash"})

# Binaries that re-exec a command string given after a `-c` flag. Includes the
# privilege shells `su`/`runuser` (they also accept `-c "<cmd>"`). Used by the
# quote-agnostic payload extractor — a self-jailbreak hides its real mutation in
# this remainder, quoted or not.
_SHELL_C_BINARIES: frozenset[str] = _SHELL_BINARIES | frozenset({"su", "runuser"})

# Flags that introduce the inline command string. `--command` is the long form
# accepted by bash/su; everything after it is the payload.
_SHELL_C_FLAGS: frozenset[str] = frozenset({"-c", "--command"})

# Indirect-exec binaries whose REMAINING ARGUMENTS are re-interpreted as a
# command to execute. The red-team weaponized these to slip a mutation past the
# guard:  `eval "systemctl stop hermes-runtime"`, `source ./payload`,
# `. ./payload`. We do NOT execute or read the file (source/.) — we re-scan the
# textual remainder as a fresh command segment so an inline `eval "<mutation>"`
# is caught. A `source <file>` exposes only the filename (no inline mutation to
# match) and falls through harmlessly.
_INDIRECT_EXEC_BINARIES: frozenset[str] = frozenset({"eval", "source", "."})

# Shells that read their script from STDIN (no `-c`): `... | bash`,
# `bash <<< "..."`, `bash <<EOF ... EOF`, `bash < file`, `bash <(...)`. When a
# shell is fed a script through any of these dataflow channels, the UPSTREAM
# producer's literal payload (the echo/printf/base64 body, the here-string body,
# the here-doc body, the process-substitution body) is what the shell executes —
# so the guard must re-scan that producer text, not just the bare `bash` token.
_STDIN_SHELL_BINARIES: frozenset[str] = _SHELL_BINARIES | frozenset({"su", "runuser"})

# Here-string operator `<<<` and its body up to the next shell separator. The
# body (`bash <<< "systemctl stop hermes-runtime"`) is the script fed to stdin.
_HERESTRING = re.compile(r"<<<\s*('([^']*)'|\"([^\"]*)\"|(\S+))")

# Here-doc opener `<< [-] [\"'] DELIM [\"']`. We capture the DELIMITER so the
# body lines (up to a line equal to DELIM) can be reconstructed as the script
# fed to stdin (`bash <<EOF\nsystemctl stop hermes-runtime\nEOF`).
_HEREDOC_OPENER = re.compile(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?")

# Process substitution `<(...)` / `>(...)`. The body is EXECUTED by the shell,
# and when the substitution is the script argument of a shell (`bash <(echo
# systemctl stop hermes-runtime)`) the body's OUTPUT becomes the script. We
# re-scan the body as a fresh candidate (covers the body-executed case) AND the
# holistic backstop catches the literal protected-service mutation.
_PROCESS_SUBST = re.compile(r"[<>]\(([^()]*)\)")

# Pipe operator splitting a producer from a consumer. Used to recover the
# upstream producer of a `producer | bash` pipe-into-shell so its literal payload
# is re-scanned. (`_SEGMENT_SEPARATOR` ALSO splits on `|`, which by itself loses
# the producer→consumer relationship — hence this dedicated recovery.)
_PIPE_SPLIT = re.compile(r"\s*\|\s*")

# Command-substitution syntaxes: `$(...)` and backtick `` `...` ``. An attacker
# hides a mutation either as the SUBSTITUTION BODY (executed:
# `$(systemctl stop hermes-runtime)`) or as the SUBSTITUTION RESULT used as an
# argument (`systemctl stop $(echo hermes-runtime)`). The body is re-scanned as a
# fresh candidate; the result case is fail-closed in the unit/path guards (a
# token still containing a substitution cannot be proven safe).
_DOLLAR_SUBST = re.compile(r"\$\(([^()]*)\)")
_BACKTICK_SUBST = re.compile(r"`([^`]*)`")

# A BUNDLED short-flag cluster whose LAST letter is `c` (e.g. `-lc`, `-xc`,
# `-ic`, `-lxc`). POSIX shells accept clustered single-letter options, and the
# `-c` must come LAST in the cluster because it consumes the next argument as the
# command string. The red-team weaponized this (`sh -lc "..."`, `bash -xc ...`)
# to slip past a guard that only matched the standalone `-c`/`--command` tokens.
# We accept only lowercase single-letter clusters so a long flag like
# `--config` (which does NOT introduce a command payload) never matches.
_BUNDLED_SHELL_C_FLAG = re.compile(r"^-[a-z]*c$")


def _is_shell_c_flag(token: str) -> bool:
    """Return True if *token* is a shell flag that introduces a command payload.

    Matches the canonical `-c` / `--command`, AND any bundled short-flag cluster
    ending in `c` (`-lc`, `-xc`, `-ic`, ...), since the shell treats the argument
    after such a cluster as the command string. `-c` itself also matches the
    bundled regex (it is the degenerate single-letter cluster).
    """
    return token in _SHELL_C_FLAGS or bool(_BUNDLED_SHELL_C_FLAG.match(token))

# Shell-operator separators that chain multiple commands in one string.
# Each resulting segment is checked independently (a mutation hidden after
# `true &&` or `foo;` must still be caught).
_SEGMENT_SEPARATOR = re.compile(r"\s*(?:&&|\|\||[;|&\n])\s*")

# Bounded iteration cap for shell-wrapper unwrapping (defends against pathological
# input while comfortably covering any realistic nesting depth).
_MAX_UNWRAP_DEPTH: int = 8

# Filesystem mutation binaries that could erase unit files or kernel binaries.
_FS_MUTATION_BINARIES: frozenset[str] = frozenset({"rm", "mv", "chattr", "mount", "umount"})

# Protected filesystem path prefixes (unit files and kernel executables).
# Any rm/mv/chattr against these subtrees is blocked.
_PROTECTED_PATH_PREFIXES: tuple[str, ...] = (
    "/usr/lib/systemd/system/hermes-",
    "/usr/libexec/hermes/",
    "/usr/bin/hermes",
    "/usr/lib/hermes/",
    "/var/lib/hermes/",
    "/etc/hermes/",
)


# Match a `-c <quoted-payload>` shell invocation: `bash -c "..."`, `sh -c '...'`,
# `/usr/bin/zsh -c "..."`, the bundled-cluster forms (`sh -lc "..."`,
# `bash -xc '...'`), and the privilege-shells `su -c "..."` / `runuser -c`.
# The leading binary must be a whole token (anchored at start or after a space/;)
# so an unrelated word ending in "sh" does not trigger a spurious unwrap. The
# command-introducing flag is `-c` OR a lowercase short-flag cluster ending in
# `c` (`-[a-z]*c`), matching the authoritative _extract_shell_c_payloads().
_SHELL_WRAPPER = re.compile(
    r"(?:^|[\s;&|])(?:[/\w.-]*/)?(?:bash|sh|dash|zsh|ash|su|runuser)\s+"
    r"(?:[^'\"\s]+\s+)*-[a-z]*c\s+(['\"])(.*?)\1",
    re.DOTALL | re.IGNORECASE,
)


def _flatten_shell_command(command_str: str) -> str:
    """Unwrap one layer of a shell-wrapper via the (quoted) regex fast-path.

    Handles a single occurrence of:
      bash -c "..."  /  sh -c '...'  /  /usr/bin/bash -c "..."  /  zsh -c "..."
    Returns the inner payload if found, otherwise the original string.

    NOTE: this regex requires a BALANCED quote and therefore MISSES unquoted
    (`bash -c systemctl stop ...`) and unbalanced (`bash -c 'systemctl ...`)
    payloads. It is kept only as a fast-path; the authoritative, quote-agnostic
    extraction is _extract_shell_c_payloads(), which every guard path consults.
    """
    m = _SHELL_WRAPPER.search(command_str)
    if m:
        return m.group(2)
    return command_str


def _extract_shell_c_payloads(command_str: str) -> list[str]:
    """Return the inner payload of the FIRST (outermost) shell `-c` in *command_str*.

    Quote-agnostic: handles quoted, unquoted and unbalanced-quote payloads alike,
    because it does NOT depend on shlex parsing the whole string or on a balanced
    closing quote. The first token that is a shell binary (bash/sh/dash/zsh/ash/
    su/runuser, path-qualified or not) followed by a `-c`/`--command` flag has the
    ENTIRE remainder of the command after that flag reconstructed as the inner
    payload — this is what the shell itself would execute, so the guard must see
    exactly that text. A surrounding matched quote pair, if present, is peeled.

    Only the outermost layer is returned (as a single-element list); deeper
    re-nesting is revealed one layer at a time by the caller's bounded recursion,
    which keeps cost linear in nesting depth instead of exploding combinatorially
    on a `bash -c bash -c bash -c ...` payload bomb.
    """
    tokens = _tokenize_loose(command_str)
    n = len(tokens)
    i = 0
    while i < n:
        binary = _binary_basename(tokens[i])
        if binary in _SHELL_C_BINARIES:
            j = i + 1
            # Skip this shell's own flags until we reach a command-introducing
            # flag (`-c`, `--command`, or a bundled cluster ending in `c` such as
            # `-lc`/`-xc`/`-ic`), or stop at the first non-flag token (no command
            # flag → not a payload-bearing shell).
            while j < n and tokens[j].startswith("-"):
                if _is_shell_c_flag(tokens[j]):
                    remainder = " ".join(tokens[j + 1 :])
                    return [_strip_matched_quotes(remainder)]
                j += 1
        i += 1
    return []


def _tokenize_loose(command_str: str) -> list[str]:
    """Whitespace-tokenize WITHOUT requiring balanced quotes.

    shlex.split() raises on an unbalanced quote, which an attacker can weaponize
    (`bash -c 'systemctl stop hermes-runtime`) to make a quote-aware tokenizer
    bail out and skip the payload. A naive whitespace split never bails, so the
    `-c` flag and its remainder are always discoverable. We keep the raw tokens
    (quotes included) and let _strip_matched_quotes() peel a clean pair if any.
    """
    return command_str.split()


def _strip_matched_quotes(text: str) -> str:
    """Strip one surrounding matched quote pair from *text* if present.

    `'systemctl stop hermes-runtime'` → `systemctl stop hermes-runtime`
    An UNbalanced leading quote (`'systemctl stop ...`) is left as-is except for
    the stray quote char, so the binary token (`systemctl`) is still exposed to
    tokenization downstream.
    """
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] in "'\"" and stripped[-1] == stripped[0]:
        return stripped[1:-1]
    # Unbalanced leading quote: drop just the stray opener so `systemctl`/etc.
    # becomes the first real token for the guard.
    if stripped[:1] in "'\"":
        return stripped[1:]
    return stripped


def _unwrap_shell_to_fixpoint(command_str: str) -> str:
    """Repeatedly unwrap shell `-c` wrappers until no more layers remain.

    Triple-/N-nested `bash -c "bash -c '...'"` re-nesting is collapsed here,
    bounded by _MAX_UNWRAP_DEPTH to defend against pathological input. The
    outer command and each inner payload are also checked as segments by the
    caller, so even an UNquoted re-nesting still routes through the guard.
    """
    current = command_str
    for _ in range(_MAX_UNWRAP_DEPTH):
        nxt = _flatten_shell_command(current)
        if nxt == current:
            break
        current = nxt
    return current


def _extract_command_substitutions(command_str: str) -> list[str]:
    """Return the bodies of every `$(...)` / backtick `` `...` `` substitution.

    The body of a command substitution is EXECUTED by the shell, so a mutation
    hidden inside (`$(systemctl stop hermes-runtime)`, `` `systemctl stop x` ``)
    must be re-scanned as a fresh command. We return each body as a separate
    candidate; the caller re-queues them through the same guards. Nested or
    sequential substitutions are matched non-overlapping; deeper nesting is
    revealed one layer at a time by the caller's bounded worklist.
    """
    bodies: list[str] = []
    for m in _DOLLAR_SUBST.finditer(command_str):
        body = m.group(1).strip()
        if body:
            bodies.append(body)
    for m in _BACKTICK_SUBST.finditer(command_str):
        body = m.group(1).strip()
        if body:
            bodies.append(body)
    return bodies


def _has_command_substitution(token: str) -> bool:
    """Return True if *token* bears a command-substitution / expansion marker.

    Used by the unit/path guards to FAIL CLOSED: a systemctl unit argument whose
    value is produced at runtime (`systemctl stop $(echo hermes-runtime)`,
    `` systemctl stop `echo x` ``, `systemctl stop ${SVC}`) cannot be statically
    proven to NOT target a protected service, so it is treated as protected.

    Matches on SUBSTRING markers (`$(`, backtick, `${`) rather than a balanced
    `$(...)` regex, because shlex/whitespace tokenization fragments a
    substitution across tokens (`$(echo` + `hermes-runtime)`); any token carrying
    a marker is therefore suspect.
    """
    return "$(" in token or "`" in token or "${" in token


def _substitution_names_protected_target(candidate: str) -> bool:
    """Fail-closed catch for substitution-built commands that NAME a protected target.

    A double-indirection (`eval "$(echo systemctl stop hermes-runtime)"`,
    `eval "$(printf 'systemctl stop hermes-runtime')"`) builds the real mutation
    from the OUTPUT of an inner command, which cannot be evaluated statically —
    re-scanning the inner body (`echo systemctl ...`) sees only a benign `echo`.
    But the literal protected-service name is sitting in the candidate text right
    next to a substitution marker. So: if the candidate both (a) contains a
    command substitution AND (b) textually contains a protected-service token,
    treat it as a self-jailbreak (fail-closed). This does NOT fire on benign
    `systemctl restart $(echo nginx)` (no protected token) or `cat $(which py)`.
    """
    if not _has_command_substitution(candidate):
        return False
    for raw in candidate.replace("(", " ").replace(")", " ").replace("`", " ").split():
        token = _strip_matched_quotes(raw).strip("'\"")
        if token.startswith("hermes-") or token == "hermes":
            return True
        if token and _is_protected_service(token):
            return True
    return False


def _extract_indirect_exec_payloads(tokens: list[str]) -> list[str]:
    """Return the re-scannable command remainder of an `eval`/`source`/`.` call.

    `[eval, "systemctl stop hermes-runtime"]` → ['systemctl stop hermes-runtime']
    `[eval, systemctl, stop, hermes-runtime]` → ['systemctl stop hermes-runtime']
    *tokens* is expected ALREADY prefix-stripped (so `sudo eval "..."` reaches
    here as `[eval, ...]`). The binary must be the FIRST token. The entire
    remainder is reconstructed and any surrounding matched quote pair is peeled,
    mirroring how the shell joins `eval`'s arguments before executing.
    `source <file>` / `. <file>` expose only a filename (no inline mutation),
    which falls through the downstream guards harmlessly.
    """
    if not tokens:
        return []
    if _binary_basename(tokens[0]) not in _INDIRECT_EXEC_BINARIES:
        return []
    # Collapse ALL leading consecutive indirect-exec binaries in one step
    # (`eval eval eval systemctl stop hermes-runtime`) so a deep chain cannot
    # outrun the bounded recursion / worklist budget.
    i = 0
    n = len(tokens)
    while i < n and _binary_basename(tokens[i]) in _INDIRECT_EXEC_BINARIES:
        i += 1
    remainder = " ".join(tokens[i:]).strip()
    if not remainder:
        return []
    return [_strip_matched_quotes(remainder)]


def _command_feeds_stdin_shell(command_str: str) -> bool:
    """Return True if *command_str* pipes/redirects a script INTO a shell.

    Detects the consumer side of the pipe-into-shell dataflow class:
      `... | bash`, `base64 -d | sh`, `bash <<< "..."`, `bash <<EOF`,
      `bash < file`, `bash <(...)`.
    A shell that reads its script from stdin (no `-c`) executes whatever the
    upstream producer / here-body emits — so the literal producer payload must be
    re-scanned. We look for a shell binary token that is EITHER the consumer of a
    pipe OR carries a stdin-redirect (`<`, `<<`, `<<<`, `<(`).
    """
    # Pipe consumers: any segment after a `|` whose first real token is a shell.
    for piece in _PIPE_SPLIT.split(command_str):
        piece = piece.strip()
        if not piece:
            continue
        toks = _strip_command_prefixes(_tokenize_loose(piece))
        if toks and _binary_basename(toks[0]) in _STDIN_SHELL_BINARIES:
            # A consumer shell with NO `-c` payload reads its script from stdin
            # (the pipe). A `bash -c "..."` consumer is handled by the existing
            # `-c` payload path, so we only flag the stdin-fed form here.
            if not _extract_shell_c_payloads(piece):
                return True
    # Stdin redirects / here-strings / here-docs / process-subst directed at a
    # shell anywhere in the command.
    if _HERESTRING.search(command_str) or _HEREDOC_OPENER.search(command_str):
        return _contains_stdin_shell_token(command_str)
    return False


def _contains_stdin_shell_token(command_str: str) -> bool:
    """Return True if any whole token is a shell binary (stdin-fed candidate)."""
    for tok in _tokenize_loose(command_str):
        if _binary_basename(tok) in _STDIN_SHELL_BINARIES:
            return True
    return False


def _extract_stdin_shell_payloads(command_str: str) -> list[str]:
    """Return literal scripts fed to a shell via pipe / here-string / here-doc.

    For `producer | bash`, the producer's literal output is the script. We cannot
    run the producer, but a plaintext producer (`echo systemctl stop hermes-...`,
    `printf '...'`) carries the mutation in its OWN arguments — so we return the
    producer text stripped of the echo/printf wrapper as a re-scannable candidate.
    For `bash <<< "<body>"` and `bash <<EOF\n<body>\nEOF` the body IS the script.
    Each returned string is re-queued through the full guard set by the caller.
    """
    payloads: list[str] = []

    # Here-strings: `<<< "systemctl stop hermes-runtime"`.
    for m in _HERESTRING.finditer(command_str):
        body = m.group(2) or m.group(3) or m.group(4) or ""
        if body.strip():
            payloads.append(body.strip())

    # Here-docs: body lines between the opener and a line == DELIM.
    payloads.extend(_extract_heredoc_bodies(command_str))

    # Pipe-into-shell: recover each upstream producer when a downstream segment is
    # a stdin-fed shell. The producer's own arguments carry the literal mutation.
    pieces = [p.strip() for p in _PIPE_SPLIT.split(command_str) if p.strip()]
    if len(pieces) >= 2:
        downstream_is_shell = False
        for piece in pieces[1:]:
            toks = _strip_command_prefixes(_tokenize_loose(piece))
            if toks and _binary_basename(toks[0]) in _STDIN_SHELL_BINARIES \
                    and not _extract_shell_c_payloads(piece):
                downstream_is_shell = True
                break
        if downstream_is_shell:
            for piece in pieces:
                payloads.append(_strip_producer_wrapper(piece))

    return [p for p in payloads if p]


def _extract_heredoc_bodies(command_str: str) -> list[str]:
    """Return the body text of every here-doc (`<<DELIM ... DELIM`) in *command_str*."""
    bodies: list[str] = []
    lines = command_str.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        opener = _HEREDOC_OPENER.search(lines[i])
        if opener:
            delim = opener.group(1)
            body_lines: list[str] = []
            j = i + 1
            while j < n and lines[j].strip() != delim:
                body_lines.append(lines[j])
                j += 1
            body = " ".join(b.strip() for b in body_lines if b.strip())
            if body:
                bodies.append(body)
            i = j + 1
            continue
        i += 1
    return bodies


# Producer binaries whose plaintext arguments become the piped script.
_PIPE_PRODUCER_BINARIES: frozenset[str] = frozenset({"echo", "printf"})

# `echo` flags that change interpretation but are NOT part of the payload:
#   -e enable escapes, -E disable escapes, -n suppress newline (and clusters
#   like -ne / -en). We strip them and ALWAYS decode escapes anyway (printf and
#   `echo -e` both emit them), so a `\n`/`\xNN`/octal hidden separator can never
#   keep the protected-service token glued to escape debris.
_ECHO_FLAG = re.compile(r"^-[neE]+$")

# ANSI-C C-escape sequences: \n \t \r \a \b \f \v \\ \" \' \0, \xHH hex,
# \0OOO / \OOO octal, \uHHHH / \UHHHHHHHH Unicode. Decoded to their real chars so
# a mutation split by an escaped separator (`printf "systemctl stop hermes-runtime\n" | bash`,
# `echo -e "systemctl\x20stop\x20hermes-runtime"`) — or whole tokens hidden behind
# Unicode escapes (`printf '\U00000073ystemctl stop hermes-runtime'`) — is
# re-flattened into a clean token run the mutation guards can match.
_C_ESCAPE = re.compile(
    r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{1,2}|[0-7]{1,3}|.)"
)

_C_ESCAPE_SIMPLE: dict[str, str] = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "e": "\x1b",
    "0": "\0",
    "\\": "\\",
    '"': '"',
    "'": "'",
}


def _decode_c_escapes(text: str) -> str:
    """Decode C/ANSI-C escape sequences (`\\n`, `\\xNN`, `\\OOO`, `\\uNNNN`, `\\UNNNNNNNN`) to real chars.

    Mirrors what `printf` and `echo -e` do to their format string before the
    bytes reach the shell on the other side of the pipe. A self-jailbreak hides
    its separator (or even whole tokens) behind these escapes so the literal
    `systemctl stop hermes-runtime` never appears verbatim — decoding re-exposes
    it. Unknown escapes (`\\z`) collapse to the bare char, matching shell
    `echo -e` behaviour and never inventing payload.
    """
    def _sub(m: "re.Match[str]") -> str:
        seq = m.group(1)
        if seq[0] in "xuU":
            try:
                return chr(int(seq[1:], 16))
            except (ValueError, OverflowError):
                # malformed/out-of-range \uXXXX/\UXXXXXXXX (e.g. \U7fffffff) → leave
                # raw, never raise (a raise here would fail-closed = false-positive block).
                return seq
        if seq[0] in "01234567":
            return chr(int(seq, 8) & 0xFF)
        return _C_ESCAPE_SIMPLE.get(seq, seq)

    return _C_ESCAPE.sub(_sub, text)


def _normalize_ansi_c_quoting(text: str) -> str:
    """Rewrite `$'...'` ANSI-C-quoted spans into their decoded literal content.

    `$'systemctl stop hermes-runtime'` and `$'systemctl\\x20stop\\x20hermes...'`
    both collapse to the real command text, so the bundled `$'...'` form cannot
    smuggle escapes (or the whole payload) past the mutation guards. Only the
    `$'...'` body is decoded; surrounding text is left intact.
    """
    return re.sub(
        r"\$'((?:\\.|[^'\\])*)'",
        lambda m: _decode_c_escapes(m.group(1)),
        text,
    )


def _strip_producer_wrapper(piece: str) -> str:
    """Strip a leading `echo`/`printf` from a pipe producer to expose its payload.

    `echo systemctl stop hermes-runtime` → `systemctl stop hermes-runtime`
    `printf 'systemctl stop hermes-runtime'` → `systemctl stop hermes-runtime`
    `printf "systemctl stop hermes-runtime\\n"` → `systemctl stop hermes-runtime`
    `echo -e "systemctl\\x20stop\\x20hermes-runtime"` → `systemctl stop hermes-runtime`
    Any other producer (`base64 -d`, `curl ...`) is returned verbatim — its
    plaintext is not statically recoverable here, but the holistic backstop and
    the literal protected-service scan provide defense-in-depth.

    The producer's escape sequences (`\\n`, `\\xNN`, octal) AND `$'...'` ANSI-C
    quoting are decoded to real chars — exactly what `printf`/`echo -e`/the shell
    emit on the far side of the pipe — so an escaped separator can never keep a
    protected-service token welded to escape debris.
    """
    piece = _normalize_ansi_c_quoting(piece)
    toks = _tokenize_loose(piece)
    if not toks:
        return ""
    if _binary_basename(toks[0]) in _PIPE_PRODUCER_BINARIES:
        rest = toks[1:]
        # Drop echo's interpretation flags (-e/-n/-E/clusters); they are not
        # payload. printf's first arg is the format string (no such flags).
        while rest and _ECHO_FLAG.match(rest[0]):
            rest = rest[1:]
        remainder = " ".join(rest)
        # Drop a leading/trailing quote, then decode C escapes so an escaped
        # separator (`...hermes-runtime\n`) becomes a real char the guards skip.
        return _decode_c_escapes(_strip_matched_quotes(remainder)).strip()
    return piece


def _split_command_segments(command_str: str) -> list[str]:
    """Split a command string on shell chaining operators into segments.

    Operators: ;  &&  ||  |  &  newline. A protected-service mutation hidden
    after `true &&` / `foo;` / a pipe must still be inspected, so each segment
    is returned for independent analysis.
    """
    parts = _SEGMENT_SEPARATOR.split(command_str)
    return [p.strip() for p in parts if p.strip()]


# Basenames that are themselves a mutation/shell target — once one of these is
# seen, prefix-stripping stops (we have reached the real command).
_TARGET_BINARIES: frozenset[str] = (
    frozenset({"systemctl", "service"})
    | _PROCESS_KILL_BINARIES
    | _FS_MUTATION_BINARIES
    | _SHELL_BINARIES
    | _INDIRECT_EXEC_BINARIES
)


def _strip_command_prefixes(tokens: list[str]) -> list[str]:
    """Peel leading prefix-wrapper binaries to expose the real command.

    `sudo systemctl disable hermes-runtime`        → ['systemctl', ...]
    `nice -n 10 systemctl mask hermes-runtime`     → ['systemctl', ...]
    `timeout 5 systemctl kill hermes-runtime`      → ['systemctl', ...]
    `su -c "..." root`                             → handled by caller recursion

    Each prefix's own flags, its non-flag arguments (e.g. the `10` of `nice -n 10`,
    the duration of `timeout`, the username of `su`) and leading VAR=val env
    assignments are consumed until a recognized target binary is reached. Bounded
    by token count; defends against `sudo sudo sudo ...` chains. Conservatively,
    if no prefix is present at position 0 we return the tokens unchanged, so a
    benign `echo systemctl stop ...` is NEVER reinterpreted as a mutation.
    """
    i = 0
    n = len(tokens)
    while i < n:
        # Consume `VAR=val` env assignments that precede the binary.
        if (
            "=" in tokens[i]
            and not tokens[i].startswith("-")
            and "/" not in tokens[i].split("=", 1)[0]
        ):
            i += 1
            continue
        binary = _binary_basename(tokens[i])
        if binary not in _COMMAND_PREFIX_BINARIES:
            break
        i += 1
        # Skip this prefix's flags AND its non-flag arguments (durations,
        # priorities, usernames, ...) until we hit the real target binary,
        # another prefix wrapper, or run out of tokens.
        while i < n:
            nxt = _binary_basename(tokens[i])
            if nxt in _TARGET_BINARIES or nxt in _COMMAND_PREFIX_BINARIES:
                break
            i += 1
    return tokens[i:]


def _tokenize(command_str: str) -> list[str]:
    """Split a command string into tokens, falling back gracefully on parse errors."""
    try:
        return shlex.split(command_str)
    except ValueError:
        return command_str.split()


def _binary_basename(token: str) -> str:
    """Return the basename of a path-qualified binary token."""
    return token.rsplit("/", 1)[-1].lower()


def _is_protected_service(unit_name: str) -> bool:
    """Return True if *unit_name* resolves to a protected Hermes service.

    Delegates to ProtectedServiceDenylist (single source of truth).
    Fail-closed: import errors or empty names are treated as protected.
    """
    if not unit_name:
        return True
    try:
        from hermes.capabilities.infrastructure.protected_service_denylist import (  # noqa: PLC0415
            ProtectedServiceDenylist,
        )
        return ProtectedServiceDenylist().is_protected(unit_name)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.security_hook.self_jailbreak.denylist_import_failed error=%r — fail-closed",
            exc,
        )
        return True


def _strip_flags(tokens: list[str]) -> list[str]:
    """Remove flag-style tokens (starting with '-') from a token list."""
    return [t for t in tokens if not t.startswith("-")]


def _check_systemctl_mutation(tokens: list[str]) -> bool:
    """Return True if tokens represent a systemctl mutation on a protected service."""
    # tokens[0] is already confirmed as 'systemctl' by caller
    clean = _strip_flags(tokens[1:])
    if not clean:
        return False
    verb = clean[0].lower()
    if verb not in _SYSTEMCTL_MUTATION_VERBS:
        return False
    # Every unit argument after the verb is a potential target.
    # (Substitution-derived unit names — `systemctl stop $(echo hermes-runtime)` —
    # are caught by _substitution_names_protected_target at the candidate level,
    # which fails closed ONLY when a protected name co-occurs, so a benign
    # `systemctl restart $(echo nginx)` still passes.)
    for unit in clean[1:]:
        if _is_protected_service(unit):
            return True
    return False


def _check_service_stop(tokens: list[str]) -> bool:
    """Return True if `service <unit> stop` targets a protected service."""
    # tokens[0] is 'service', tokens[1] is unit, tokens[2] is verb
    if len(tokens) < 3:
        return False
    unit = tokens[1]
    verb = tokens[2].lower()
    if verb != "stop":
        return False
    # Substitution-derived unit names handled at candidate level
    # (_substitution_names_protected_target); here only literal protected units.
    return _is_protected_service(unit)


def _check_process_kill(tokens: list[str]) -> bool:
    """Return True if kill/pkill/killall targets a protected Hermes process name."""
    # We check for known process name patterns in the argument list.
    # Numeric PIDs are not blocked (operator may need to kill unrelated procs).
    clean = _strip_flags(tokens[1:])
    for arg in clean:
        if arg.isdigit():
            continue
        # (Substitution-derived process names — `kill $(pgrep hermes-runtime)` —
        # are caught at candidate level by _substitution_names_protected_target.)
        if _is_protected_service(arg):
            return True
        # pkill -f matches full command line — check for 'hermes-' prefix.
        if arg.startswith("hermes-") or arg == "hermes":
            return True
    return False


def _check_fs_mutation(tokens: list[str]) -> bool:
    """Return True if rm/mv/chattr/mount targets a protected Hermes path."""
    for arg in tokens[1:]:
        if arg.startswith("-"):
            continue
        for prefix in _PROTECTED_PATH_PREFIXES:
            if arg.startswith(prefix) or arg == prefix.rstrip("/"):
                return True
    return False


def _check_terminal_self_jailbreak(command_str: str) -> str | None:
    """Detect terminal commands that would silence the Hermes security kernel.

    Covers:
      - systemctl stop|disable|mask|kill|restart <protected-service>
      - service <protected-service> stop
      - kill / pkill / killall targeting hermes-* process names
      - rm / mv / chattr of protected unit files or kernel binaries
      - mount / umount of protected paths
      - All of the above wrapped in bash -c / sh -c

    Read-only systemctl verbs (status, is-active, show, list-units, etc.) and
    mutations on unprotected services (e.g. nginx, cups) are NOT blocked.

    Returns a block message string if the command should be rejected, else None.
    """
    # Scan the ORIGINAL command and every nesting layer it unwraps to. We do not
    # rely on argv[0]: each candidate string is split into shell segments, each
    # segment has its prefix wrappers (sudo/env/nice/timeout/...) peeled off, and
    # the resulting real command is matched against the mutation guards.
    #
    # Candidate gathering is breadth-first over a worklist so that the inner
    # payload of EVERY shell `-c` (quoted, unquoted, or unbalanced) is itself
    # re-scanned for shell `-c` re-nesting — defeating arbitrarily nested
    # `bash -c "bash -c systemctl stop hermes-runtime"` chains without relying on
    # shlex balance. Bounded by _MAX_UNWRAP_DEPTH expansions.
    # Holistic backstop FIRST: a pipe-into-shell / here-string / here-doc /
    # process-substitution splits the mutation across producer and consumer, so
    # the per-segment guards (which sever on `|`) miss it. Flatten the whole
    # command and re-scan as a single token run. Defense-in-depth — the targeted
    # re-queuing below also recovers the literal payloads.
    if _holistic_command_is_self_jailbreak(command_str):
        return _SELF_JAILBREAK_MSG

    seen: set[str] = set()
    worklist: list[str] = [command_str]
    expansions = 0
    while worklist and expansions < _MAX_UNWRAP_DEPTH * 4:
        candidate = worklist.pop()
        # Quoted fast-path unwrap (kept for parity); the loose extractor below is
        # the authoritative, quote-agnostic source of inner payloads.
        unwrapped = _unwrap_shell_to_fixpoint(candidate)
        if unwrapped != candidate:
            worklist.append(unwrapped)
            expansions += 1

        # Double-indirection fail-closed: a substitution-built command that
        # textually names a protected service (`eval "$(echo systemctl stop
        # hermes-runtime)"`) cannot be evaluated statically — block it.
        if _substitution_names_protected_target(candidate):
            return _SELF_JAILBREAK_MSG

        # Command-substitution bodies (`$(...)`, `` `...` ``) anywhere in this
        # candidate are EXECUTED by the shell — re-queue each body as a fresh
        # candidate so a mutation hidden inside (`$(systemctl stop hermes-...)`)
        # is re-scanned through every guard.
        for body in _extract_command_substitutions(candidate):
            if body and body not in seen:
                worklist.append(body)
                expansions += 1

        # Pipe-into-shell / here-string / here-doc: when this candidate feeds a
        # script INTO a stdin-reading shell (`echo systemctl stop hermes-runtime
        # | bash`, `bash <<< "..."`, `bash <<EOF ... EOF`), the upstream producer
        # payload / here-body IS the script. Re-queue each literal payload so the
        # full guard set runs on it (the segment-split loop alone would only see a
        # benign `echo ...` and a bare `bash`).
        if _command_feeds_stdin_shell(candidate):
            for payload in _extract_stdin_shell_payloads(candidate):
                if payload and payload not in seen:
                    worklist.append(payload)
                    expansions += 1

        # Process-substitution bodies (`<(...)`, `>(...)`) are EXECUTED by the
        # shell; when used as a shell's script argument (`bash <(echo systemctl
        # stop hermes-runtime)`) the body output becomes the script. Re-queue
        # each body as a fresh candidate.
        for m in _PROCESS_SUBST.finditer(candidate):
            body = m.group(1).strip()
            if body and body not in seen:
                worklist.append(body)
                expansions += 1

        for segment in _split_command_segments(candidate):
            if segment in seen:
                continue
            seen.add(segment)
            if _segment_is_self_jailbreak(segment):
                return _SELF_JAILBREAK_MSG
            # Reconstruct the post-`-c` remainder of every shell invocation in
            # this segment and queue each as a fresh candidate to re-scan.
            for payload in _extract_shell_c_payloads(segment):
                if payload and payload not in seen:
                    worklist.append(payload)
                    expansions += 1
            # `eval "<cmd>"` / `eval <cmd>` (and source/.) re-exec their
            # remainder as a command — queue it as a fresh candidate. Tokens are
            # prefix-stripped so `sudo eval "..."` is reached too.
            for payload in _extract_indirect_exec_payloads(
                _strip_command_prefixes(_tokenize(segment))
            ):
                if payload and payload not in seen:
                    worklist.append(payload)
                    expansions += 1
    return None


def _segment_is_self_jailbreak(segment: str, _depth: int = 0) -> bool:
    """Return True if a single command segment mutates a protected service.

    Peels leading prefix wrappers, then matches the exposed binary against the
    systemctl / service / process-kill / fs-mutation guards. If the segment is
    itself a shell `-c` re-nesting, recurse into the inner payload. Recursion is
    bounded by _MAX_UNWRAP_DEPTH so pathological `bash -c bash -c ...` chains
    cannot exhaust the stack.
    """
    if _depth >= _MAX_UNWRAP_DEPTH:
        # Depth exhausted: treat the residual as suspicious only if it still
        # matches a mutation guard below (no further recursion). Fall through.
        pass

    tokens = _strip_command_prefixes(_tokenize(segment))
    if not tokens:
        return False

    binary = _binary_basename(tokens[0])

    # Inner shell re-nesting exposed only after prefix-stripping (e.g.
    # `sudo bash -c "systemctl disable hermes-runtime"`, or the quote-evasion
    # variants `sudo bash -c systemctl stop hermes-runtime` /
    # `sudo bash -c 'systemctl stop hermes-runtime`): reconstruct the post-`-c`
    # remainder quote-agnostically and recurse into each inner segment. This does
    # NOT rely on shlex balance — it is the authoritative payload-inspection path.
    if binary in _SHELL_C_BINARIES and _depth < _MAX_UNWRAP_DEPTH:
        for payload in _extract_shell_c_payloads(segment):
            for inner_segment in _split_command_segments(payload):
                if _segment_is_self_jailbreak(inner_segment, _depth + 1):
                    return True

    # Indirect exec exposed after prefix-stripping (`sudo eval "systemctl stop
    # hermes-runtime"`): the remainder of eval/source/. is re-executed as a
    # command — reconstruct it quote-agnostically and recurse into each segment.
    if binary in _INDIRECT_EXEC_BINARIES and _depth < _MAX_UNWRAP_DEPTH:
        for payload in _extract_indirect_exec_payloads(tokens):
            for inner_segment in _split_command_segments(payload):
                if _segment_is_self_jailbreak(inner_segment, _depth + 1):
                    return True

    # Command substitution whose RESULT/BODY is buried in this segment. The body
    # is executed — recurse into it; this also covers a substitution used as a
    # bare command (`$(systemctl stop hermes-runtime)`). The fail-closed unit
    # guard below additionally catches the result-as-argument case.
    if _has_command_substitution(segment) and _depth < _MAX_UNWRAP_DEPTH:
        for body in _extract_command_substitutions(segment):
            for inner_segment in _split_command_segments(body):
                if _segment_is_self_jailbreak(inner_segment, _depth + 1):
                    return True

    if binary == "systemctl" and _check_systemctl_mutation(tokens):
        return True
    if binary == "service" and _check_service_stop(tokens):
        return True
    if binary in _PROCESS_KILL_BINARIES and _check_process_kill(tokens):
        return True
    if binary in _FS_MUTATION_BINARIES and _check_fs_mutation(tokens):
        return True

    return False


# Shell metacharacters that fragment a command into producer/consumer/body
# pieces. The holistic backstop strips these so a protected-service mutation that
# was split across a pipe/redirect/here-string is re-exposed as a flat token run
# for one final `_segment_is_self_jailbreak` pass.
_SHELL_METACHARS = re.compile(r"[|&;<>()`]+|<<<|<<-?")


def _holistic_command_is_self_jailbreak(command_str: str) -> bool:
    """Backstop: re-scan the WHOLE command with shell metacharacters flattened.

    The per-segment guards split on `|`/`;`/`&&`, which severs a pipe-into-shell
    (`echo systemctl stop hermes-runtime | bash`) into a benign `echo ...` and a
    bare `bash`, so neither segment matches. This backstop strips ALL shell
    operators/redirects/substitution punctuation and the producer wrappers
    (`echo`/`printf`/`base64`/quotes), then re-runs the mutation guards on the
    residual flat token run. A protected-service mutation that was hidden by the
    dataflow plumbing is thereby re-exposed.

    Conservative by construction: it only fires when the FLATTENED text still
    parses as a recognized mutation binary acting on a protected target — benign
    `echo hello | bash`, `cat f | sh`, `systemctl status hermes-runtime | grep x`
    do NOT match (no mutation verb / no protected target).

    C-escape sequences (`\\n`, `\\xNN`, octal) and `$'...'` ANSI-C quoting are
    decoded FIRST, so a producer that smuggles the mutation behind escapes
    (`printf "systemctl stop hermes-runtime\\n" | bash`, `echo -e
    "systemctl\\x20stop\\x20hermes-runtime" | sh`) is flattened into the same
    clean token run as the literal form.
    """
    decoded = _decode_c_escapes(_normalize_ansi_c_quoting(command_str))
    flat = _SHELL_METACHARS.sub(" ", decoded)
    flat = flat.replace("'", " ").replace('"', " ")
    # Drop producer / decoder wrappers and shell binaries so the residual begins
    # at the real mutation binary (`systemctl`, `kill`, `rm`, ...).
    noise = (
        _PIPE_PRODUCER_BINARIES
        | _STDIN_SHELL_BINARIES
        | _COMMAND_PREFIX_BINARIES
        | _INDIRECT_EXEC_BINARIES
        | frozenset({"base64", "cat", "tee", "xxd", "od"})
    )
    tokens = [t for t in flat.split() if _binary_basename(t) not in noise]
    # Re-run the mutation guards over every suffix that starts at a target binary,
    # so `... systemctl stop hermes-runtime ...` is caught regardless of leading
    # decoder/flag debris.
    n = len(tokens)
    for start in range(n):
        binary = _binary_basename(tokens[start])
        if binary in _TARGET_BINARIES:
            if _segment_is_self_jailbreak(" ".join(tokens[start:])):
                return True
    return False


_SELF_JAILBREAK_MSG: str = (
    "REJECTED: el agente no puede desactivar el kernel de seguridad del SO "
    "(anti-autopirateo, inapelable)."
)


def make_pre_tool_call_hook(
    *,
    agent_state: "AgentStatePort",
    engine_loop: "asyncio.AbstractEventLoop",
    broker: Any,
) -> Any:
    """Build and return the pre_tool_call hook callback.

    The returned callable is registered directly on the PluginManager._hooks
    list. It receives the kwargs that model_tools.get_pre_tool_call_block_message
    passes to invoke_hook("pre_tool_call", ...) and returns either a block dict
    or None.

    Args:
        agent_state: Port for reading pause state (kill-switch).
        engine_loop: The running asyncio event loop (for async bridges).
        broker: CapabilityBroker instance (for denylist access).
    """
    from hermes.capabilities.tool_delicacy import hook_mfa_block  # noqa: PLC0415
    from hermes.capabilities.tool_policy import ToolPolicyStore  # noqa: PLC0415

    _tool_policy = ToolPolicyStore()

    def _pre_tool_call_hook(
        tool_name: str = "",
        args: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, str] | None:
        """Gate every tool call. Returns block directive or None to allow."""
        safe_args = args if isinstance(args, dict) else {}

        try:
            # Step 1: Kill-switch — checked first, always inapelable.
            if _check_kill_switch(agent_state, engine_loop):
                logger.info(
                    "hermes.security_hook.pre.kill_switch_blocked tool=%s", tool_name
                )
                return _block("agent paused — kill-switch active (CTRL-12)")

            # Step 1.5: Per-command policy (Security/Policies UI, P4.B). A command
            # the owner disabled is denied here — deterministic, the LLM may propose
            # it but the gate refuses. Default "Equilibrado" = on except high-risk.
            # Fail-open ONLY on store error (the cage/denylist/MFA gates are the real
            # security floor and always apply regardless).
            try:
                if tool_name and not _tool_policy.is_enabled(tool_name):
                    logger.info(
                        "hermes.security_hook.pre.policy_disabled tool=%s", tool_name
                    )
                    return _block(
                        f"'{tool_name}' está deshabilitada en Seguridad/Políticas. "
                        "Actívala ahí para usarla."
                    )
            except Exception:  # noqa: BLE001 — policy is a usability layer, not the floor
                pass

            # Step 1.6: MFA-on-dangers (owner decision 2026-06-19; coherence audit fix).
            # Gates NATIVE dangers that bypass the broker. hook_mfa_block encapsulates the
            # full decision (single source in tool_delicacy): MOST_DELICATE native
            # (skill_manage/cronjob/delegate_task) ALWAYS needs MFA — the escape hatch
            # NEVER frees self-widening; cage-escaping DELICATE (send_message/discord/ha)
            # needs MFA only while mfa_on_dangers is ON; caged-exec / cage-contained /
            # reads / capability+external tools are handled elsewhere (gateway, cage,
            # broker HITL). SECURITY gate → not swallowed: errors fail-CLOSED via the
            # outer handler; the flag accessor itself fails-safe to ON.
            if tool_name and hook_mfa_block(
                tool_name, mfa_on_dangers=_tool_policy.mfa_on_dangers()
            ):
                # Per-action owner approval (pause→approve→resume): query the durable
                # approval gate by the action digest. If the owner already approved THIS
                # exact action (web UI + MFA-at-gate), consume the single-use token and
                # ALLOW; otherwise register a pending approval (surfaces in the web UI)
                # and BLOCK with a retry hint. ALL in the hook → covers concurrent AND
                # sequential paths. FAIL-CLOSED: any error → block (never raise, which
                # invoke_hook would swallow into ALLOW — red-team finding 2).
                block_msg = _resolve_native_danger_approval(
                    tool_name, safe_args, broker, engine_loop
                )
                if block_msg is not None:
                    logger.info(
                        "hermes.security_hook.pre.native_danger_pending tool=%s", tool_name
                    )
                    return _block(block_msg)
                logger.info(
                    "hermes.security_hook.pre.native_danger_approved tool=%s", tool_name
                )

            # Step 2: Native hardline floor — detect_hardline_command.
            # Applies to terminal commands; also catches shell-wrapper payloads.
            command_str = _extract_command_str(tool_name, safe_args)
            if command_str is not None:
                msg = _check_hardline_native(command_str)
                if msg is not None:
                    logger.warning(
                        "hermes.security_hook.pre.hardline_blocked tool=%s", tool_name
                    )
                    return _block(msg)

                # Step 3: Self-jailbreak guard — terminal commands that would
                # stop/disable/mask/kill/rm a protected Hermes service or its
                # filesystem artefacts. Terminal and inapelable (anti-autopirateo).
                msg = _check_terminal_self_jailbreak(command_str)
                if msg is not None:
                    logger.warning(
                        "hermes.security_hook.pre.self_jailbreak_blocked tool=%s cmd=%r",
                        tool_name,
                        command_str,
                    )
                    return _block(msg)

                # Step 4: Full command guards (native approval.py).
                msg = _check_command_guards_native(command_str)
                if msg is not None:
                    logger.warning(
                        "hermes.security_hook.pre.command_guard_blocked tool=%s", tool_name
                    )
                    return _block(msg)

            # Step 5: Code guard for execute_code-type tools.
            code_str = _extract_code_str(tool_name, safe_args)
            if code_str is not None:
                msg = _check_code_guard_native(str(code_str))
                if msg is not None:
                    logger.warning(
                        "hermes.security_hook.pre.code_guard_blocked tool=%s", tool_name
                    )
                    return _block(msg)

            # Step 6: Denylist anti-autopirateo for os_native service ops.
            # The broker already gates these in dispatch(), but the hook fires
            # before _invoke_tool reaches the broker — this is defense-in-depth
            # for the sequential path where tools may bypass _invoke_tool.
            denylist_block = _check_broker_denylist(broker, tool_name, safe_args)
            if denylist_block is not None:
                logger.warning(
                    "hermes.security_hook.pre.denylist_blocked tool=%s", tool_name
                )
                return _block(denylist_block)

        except Exception as exc:
            # Fail-closed: unexpected error in the gate → block.
            logger.error(
                "hermes.security_hook.pre.gate_exception tool=%s error=%r — blocking (fail-closed)",
                tool_name,
                exc,
            )
            return _block(f"security gate error (fail-closed): {type(exc).__name__}")

        return None  # allow

    return _pre_tool_call_hook


_NATIVE_DANGER_GATE_TIMEOUT_S: float = 30.0


def _resolve_native_danger_approval(
    tool_name: str, args: dict[str, Any], broker: Any, engine_loop: Any
) -> str | None:
    """Per-action owner approval for a cage-escaping NATIVE danger.

    Returns None if the owner has ALREADY approved this exact action (the single-use
    token is consumed here → ALLOW). Returns a block message otherwise (a pending row
    is registered so it surfaces in the web approvals UI; the owner approves with MFA
    and the agent re-attempts the action → it then executes).

    FAIL-CLOSED: the gate/loop missing or ANY error → a block message (never raises;
    a raise here would be swallowed by invoke_hook into ALLOW — red-team finding 2).
    Execution stays on the caller's (conversation) thread; this only does bounded async
    gate I/O via run_coroutine_threadsafe.
    """
    import asyncio  # noqa: PLC0415
    import hashlib  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    from uuid import UUID, uuid4  # noqa: PLC0415

    gate = getattr(broker, "_approval_gate", None) if broker is not None else None
    if gate is None or engine_loop is None:
        return ("requiere aprobación del dueño con MFA, pero el buzón de aprobaciones "
                "no está disponible — bloqueado (fail-closed).")

    def _await(coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, engine_loop).result(
            timeout=_NATIVE_DANGER_GATE_TIMEOUT_S
        )

    try:
        safe = args if isinstance(args, dict) else {}
        digest = hashlib.sha256(
            (tool_name + "\x00" + _json.dumps(safe, sort_keys=True, default=str)).encode(
                "utf-8", "replace"
            )
        ).hexdigest()

        # Already approved? Consume the single-use token (binds to THIS action) → allow.
        pid = _await(gate.approved_proposal_for_digest(digest))
        if pid is not None:
            token = _await(gate.approved_token_for(pid))
            if token and _await(gate.verify_token(proposal_id=pid, token=token)):
                return None  # owner-approved + consumed → ALLOW exactly once

        # Not approved → register a pending row (web UI shows it) + block with a hint.
        from hermes.capabilities.domain.ports import ConsentContext, RiskLevel  # noqa: PLC0415

        _await(gate.register_pending(
            proposal_id=uuid4(),
            work_item_id=UUID(int=0),
            consent_context=ConsentContext(operator_id=None, tenant_id=UUID(int=0)),
            risk=RiskLevel.HIGH,
            justification=f"{tool_name} actúa fuera de la jaula y requiere aprobación del dueño",
            parameters_redacted=safe,
            tool_name=tool_name,
            action_digest=digest,
        ))
        return ("requiere aprobación del dueño con MFA. Apruébala en el panel de "
                "aprobaciones; al reintentar la misma acción tras aprobar, se ejecutará.")
    except Exception as exc:  # noqa: BLE001 — security gate: never raise (would ALLOW)
        logger.error(
            "hermes.security_hook.native_danger_gate_error tool=%s err=%r — block (fail-closed)",
            tool_name, exc,
        )
        return "error en el buzón de aprobaciones — bloqueado (fail-closed)."


def _check_broker_denylist(broker: Any, tool_name: str, args: dict[str, Any]) -> str | None:
    """Check the broker's denylist for os_native service mutation ops.

    Reuses the OsNativeDispatcher._denylist that the broker already holds.
    Returns a block message if the service is protected, None otherwise.
    Fail-open if the dispatcher or denylist is not available.
    """
    _service_ops = frozenset({"start_service", "stop_service", "restart_service"})
    if tool_name not in _service_ops:
        return None

    unit = args.get("unit")
    if not unit:
        return None

    try:
        dispatcher = getattr(broker, "_os_native_dispatcher", None)
        if dispatcher is None:
            return None
        denylist = getattr(dispatcher, "_denylist", None)
        if denylist is None:
            return None
        if denylist.is_protected_canonical(unit):
            return (
                f"BLOCKED (denylist anti-autopirateo): operación '{tool_name}' "
                f"sobre servicio protegido '{unit}' — frenos del agente son "
                "inviolables (CTRL-P2-2/NFR-002)"
            )
    except Exception as exc:
        logger.debug(
            "hermes.security_hook.denylist_check_error tool=%s error=%r — skip (fail-open)",
            tool_name,
            exc,
        )
    return None


def make_post_tool_call_hook(
    *,
    signer: "AuditHashChainSigner",
    audit_repo: "SignedAuditRepositoryPort",
    engine_loop: "asyncio.AbstractEventLoop",
) -> Any:
    """Build and return the post_tool_call hook callback.

    Signs every tool execution (allow or deny) into the audit hash-chain.
    Signing is non-fatal: any error is logged at ERROR level and swallowed —
    the agent loop must not crash because of an audit failure.

    The callback fires after every tool call (including blocked ones, which
    emit status="blocked" from model_tools._emit_post_tool_call_hook).
    """

    def _post_tool_call_hook(
        tool_name: str = "",
        args: dict[str, Any] | None = None,
        result: Any = None,
        status: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        task_id: str = "",
        duration_ms: int = 0,
        **kwargs: Any,
    ) -> None:
        """Append a signed audit entry for every tool execution."""
        from hermes.agents_os.application.audit_hash_chain import AuditKind  # noqa: PLC0415

        audit_kind = (
            AuditKind.PROPOSAL_EXECUTED
            if status not in ("blocked", "error")
            else AuditKind.PROPOSAL_REJECTED
        )
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "status": status or "unknown",
            "duration_ms": duration_ms,
            "task_id": task_id,
        }
        if error_type:
            payload["error_type"] = error_type
        # Never log args or result — may contain PII/secrets.

        try:
            _run_async_bridge(
                signer.append_and_persist(
                    audit_kind=audit_kind,
                    actor="security_hook",
                    description=f"tool_call: {tool_name} → {status or 'unknown'}",
                    payload=payload,
                    audit_repo=audit_repo,
                ),
                engine_loop,
            )
        except Exception as exc:
            logger.error(
                "hermes.security_hook.post.audit_failed tool=%s error=%r",
                tool_name,
                exc,
            )

    return _post_tool_call_hook


def register_security_hooks(
    *,
    agent_state: "AgentStatePort",
    engine_loop: "asyncio.AbstractEventLoop",
    broker: Any,
    signer: "AuditHashChainSigner",
    audit_repo: "SignedAuditRepositoryPort",
) -> None:
    """Register both pre_tool_call and post_tool_call hooks on the global PluginManager.

    Idempotent within a process restart. Must be called AFTER the broker,
    agent_state, signer, and audit_repo are fully constructed — i.e. after
    _build_real_broker() returns in __main__._run().

    Uses get_plugin_manager()._hooks directly (same internal path as
    PluginContext.register_hook) because we are not a plugin manifest — we are
    the runtime kernel registering its own security gate.
    """
    try:
        from hermes_cli.plugins import get_plugin_manager  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "hermes.security_hook.register: hermes_cli.plugins unavailable — "
            "pre/post_tool_call hooks NOT registered. Native hermes-agent gate absent."
        )
        return

    manager = get_plugin_manager()

    pre_hook = make_pre_tool_call_hook(
        agent_state=agent_state,
        engine_loop=engine_loop,
        broker=broker,
    )
    post_hook = make_post_tool_call_hook(
        signer=signer,
        audit_repo=audit_repo,
        engine_loop=engine_loop,
    )

    manager._hooks.setdefault("pre_tool_call", []).append(pre_hook)
    manager._hooks.setdefault("post_tool_call", []).append(post_hook)

    logger.info(
        "hermes.security_hook.registered: pre_tool_call + post_tool_call hooks active "
        "(kill-switch + hardline + self-jailbreak + command/code guards + denylist + audit)"
    )
