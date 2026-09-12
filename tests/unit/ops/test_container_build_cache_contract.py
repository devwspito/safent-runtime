"""Build wrappers preserve useful caches and never erase user data implicitly."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_published_image_uses_content_cache_not_commit_cachebust():
    workflow = _read(".github/workflows/publish-image.yml")
    containerfile = _read("ops/container/Containerfile")
    assert "cache-from: type=gha" in workflow
    assert "cache-to: type=gha,mode=max" in workflow
    assert "FE_CACHEBUST" not in workflow
    assert "APP_CACHEBUST" not in containerfile
    assert "FE_CACHEBUST" not in containerfile
    assert "setup-qemu-action" not in workflow
    assert "runner: ubuntu-22.04-arm" in workflow
    assert "platforms: ${{ matrix.platform }}" in workflow
    assert "needs: build" in workflow
    assert "docker buildx imagetools create" in workflow


def test_local_build_does_not_rebuild_wheel_twice_or_mutate_checkout():
    script = _read("ops/container/build.sh")
    assert "python3 -m pip wheel" not in script
    assert "rm -f dist/" not in script
    assert "sed -i" not in script
    assert "date +%s" not in script


def test_local_podman_build_preserves_healthcheck_metadata():
    script = _read("ops/container/build.sh")
    assert 'basename "$RUNTIME"' in script
    assert "BUILD_FORMAT_ARGS+=(--format docker)" in script
    assert '"$RUNTIME" build "${BUILD_FORMAT_ARGS[@]}"' in script


def test_container_uses_current_mcp_import_and_blocklist_path():
    containerfile = _read("ops/container/Containerfile")
    assert "from tools.mcp_tool_discovery import get_mcp_status" in containerfile
    assert "dns-blocklists/main/wildcard/light.txt" in containerfile
    assert "dns-blocklists/main/domains/light.txt" not in containerfile


def test_container_pins_current_security_toolchain():
    containerfile = _read("ops/container/Containerfile")
    assert "ARG NPM_VERSION=11.19.1" in containerfile
    assert "ARG PLAYWRIGHT_MCP_VERSION=0.0.80" in containerfile
    assert "ARG TRIVY_VERSION=v0.74.0" in containerfile
    assert "ARG TAILSCALE_VERSION=1.102.4" in containerfile
    assert "npx --yes playwright install" not in containerfile
    assert "COPY ops/agents-os-edition/seed/excel-mcp-overrides.txt" in containerfile


def test_local_build_overrides_base_image_version_label():
    script = _read("ops/container/build.sh")
    containerfile = _read("ops/container/Containerfile")
    assert '--build-arg SAFENT_VERSION="${VERSION}"' in script
    assert 'LABEL org.opencontainers.image.version="${SAFENT_VERSION}"' in containerfile


def test_source_installer_preserves_cache_and_data_by_default():
    script = _read("ops/container/install.sh")
    assert "builder prune" not in script
    assert "image prune" not in script
    assert "date +%s" not in script
    assert 'SAFENT_FACTORY_RESET:-0' in script
    assert 'SAFENT_FACTORY_RESET:-0}" = "1"' in script
