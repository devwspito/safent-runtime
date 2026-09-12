"""Pure preparation of a single-identity Hermes profile; does not activate it.

Never merge a personal config or environment. Callers supply the native
DEFAULT_CONFIG only to enumerate auxiliary tasks for the pinned Hermes build.
The gate in managed_llm remains closed until lifecycle and real error-route
verification are complete. No upstream monkeypatch or alternate inference SDK.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

from hermes.runtime.model_config import ModelConfig
from hermes.security.configuration_lock import configuration_lock

_PRIVATE_DIRECTORY_MODE = 0o700


def build_profile(binding: ModelConfig, native_defaults: Mapping) -> dict:
    """Return secret-free native configuration with every known aux route pinned.

    The instance-scoped key is supplied only to the native main runtime from
    the vault, not persisted in config.yaml, exported as provider env or copied
    to MCP. Hermes's live runtime context must supply auxiliary authentication;
    the real-image matrix verifies that behavior before any activation.
    """
    endpoint = urlparse(binding.base_url or "")
    if (
        not binding.managed
        or binding.native_provider != "custom"
        or not binding.model.startswith("custom/")
        or not binding.model[7:].strip()
        or not binding.api_key
        or endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("A verified instance gateway binding is required")
    model = binding.model[7:]
    auxiliary = native_defaults.get("auxiliary", {})
    if not isinstance(auxiliary, Mapping) or not auxiliary:
        raise ValueError("Pinned Hermes auxiliary task catalog is required")
    routes = {
        name: {
            "provider": "custom",
            "model": model,
            "base_url": binding.base_url,
            "api_key": "",
            "api_mode": "chat_completions",
            "fallback_chain": [],
        }
        for name, value in auxiliary.items()
        if isinstance(name, str) and isinstance(value, Mapping)
    }
    if not routes:
        raise ValueError("Pinned Hermes auxiliary task catalog is empty")
    return {
        "model": {
            "provider": "custom",
            "default": model,
            "base_url": binding.base_url,
            "api_mode": "chat_completions",
        },
        "auxiliary": {**routes, "transient_retries": 0},
        "fallback_providers": [],
        "fallback_model": None,
        "custom_providers": [],
        # Do not copy personal tools/plugins/profile hooks with arbitrary defaults.
        # Managed tool manifests must be projected separately through the jaula.
        "mcp_servers": {},
        "plugins": {},
    }


def allowed_environment(source: Mapping[str, str], profile_home: Path) -> dict[str, str]:
    """Minimal inference profile environment, NOT a full daemon launch contract.

    No inherited provider credentials, proxies, SDK profile selectors, Python
    paths or loader settings. Infrastructure env needed by the existing daemon
    must be explicitly projected by the later lifecycle implementation.
    """
    if not profile_home.is_absolute():
        raise ValueError("Corporate profile home must be absolute")
    path = str(profile_home)
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": path,
        "HERMES_HOME": path,
        "XDG_CONFIG_HOME": str(profile_home / ".config"),
        "XDG_CACHE_HOME": str(profile_home / ".cache"),
        "XDG_DATA_HOME": str(profile_home / ".local/share"),
        **{name: source[name] for name in ("LANG", "LC_ALL", "LC_CTYPE", "TZ") if source.get(name)},
    }


# Infrastructure settings of the EXISTING daemon. No prefix wildcard: new
# provider/auth/profile variables must never enter by accident.
_DAEMON_ENVIRONMENT = frozenset(
    {
        "NOTIFY_SOCKET",
        "WATCHDOG_USEC",
        "WATCHDOG_PID",
        "INVOCATION_ID",
        "DBUS_SYSTEM_BUS_ADDRESS",
        "JOURNAL_STREAM",
        "HERMES_SHELL_DB",
        "HERMES_CONSENT_DB",
        "HERMES_OPERATOR_ID",
        "HERMES_OPERATOR_UID",
        "HERMES_TENANT_ID",
        "HERMES_TZ",
        "HERMES_HEALTH_INTERVAL_S",
        "HERMES_LEASE_SECONDS",
        "HERMES_MCP_LAUNCHER",
        "HERMES_BROWSER_JAIL",
        "HERMES_CHROMIUM_EXECUTABLE",
        "HERMES_CUA_DRIVER_CMD",
        "AGENT_BROWSER_ARGS",
        "HERMES_EXEC_ASK",
        "COMPOSIO_CACHE_DIR",
        "npm_config_cache",
        "UV_CACHE_DIR",
        "TRIVY_CACHE_DIR",
        "HERMES_TOOL_EMBED_CACHE",
        "UV_TOOL_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "TMPDIR",
        "HERMES_COMPOSIO_TOOLS_TTL_S",
        "HERMES_FS_ALLOWLIST",
        "HERMES_RUNTIME_LANDLOCK",
        "HERMES_RUNTIME_LANDLOCK_ALLOW_DEGRADE",
    }
)


def daemon_environment(source: Mapping[str, str], profile_home: Path) -> dict[str, str]:
    """Project only deployment infrastructure alongside the isolated identity.

    The calling service's security configuration is preserved, not broadened.
    Explicit inference provider/model/proxy/loader settings are not inherited.
    The engine is fixed to the existing native Hermes implementation.
    """
    return {
        **allowed_environment(source, profile_home),
        **{key: source[key] for key in _DAEMON_ENVIRONMENT if key in source},
        "HERMES_ENGINE": "nous",
    }


def create_profile_home(db_path: Path, generation: int) -> Path:
    """Create a fresh private home for ONE daemon boot, never reuse personal data."""
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("Invalid corporate generation")
    from hermes.runtime.managed_profile_retention import prune_profile_homes  # noqa: PLC0415

    with configuration_lock(db_path):
        parent = db_path.absolute().parent.resolve(strict=True)
        root = parent / "managed-profiles"
        with suppress(FileExistsError):
            root.mkdir(mode=0o700)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != _PRIVATE_DIRECTORY_MODE
            ):
                raise PermissionError("Corporate profile directory is unsafe")
            # Allocation and retention share the same process/thread lock. A
            # recorded PID is conservative liveness evidence, not authority.
            prune_profile_homes(db_path)
            name = f"g{generation}-p{os.getpid()}-{secrets.token_hex(8)}"
            os.mkdir(name, mode=0o700, dir_fd=directory)
            return root / name
        finally:
            os.close(directory)


def write_profile(profile_home: Path, profile: Mapping) -> None:
    """Write one secret-free config to the newly allocated private home.

    JSON is YAML-compatible and needs no extra serializer. Existing files are
    never overwritten or merged; this home must belong exclusively to this boot.
    """
    info = profile_home.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != _PRIVATE_DIRECTORY_MODE
    ):
        raise PermissionError("Corporate profile home is unsafe")
    data = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    directory = os.open(profile_home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(
            "config.yaml",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def current_profile(db_path: Path, generation) -> dict:
    """Existing verified binding plus native defaults, only AFTER clean exec."""
    if generation.mode == "blocked":
        return {
            "model": {"provider": "custom", "default": "", "base_url": ""},
            "auxiliary": {},
            "fallback_providers": [],
            "fallback_model": None,
            "custom_providers": [],
            "mcp_servers": {},
            "plugins": {},
        }
    from hermes_cli.config import DEFAULT_CONFIG  # noqa: PLC0415

    from hermes.runtime.managed_llm import _resolve_managed_binding  # noqa: PLC0415

    return build_profile(_resolve_managed_binding(db_path), DEFAULT_CONFIG)
