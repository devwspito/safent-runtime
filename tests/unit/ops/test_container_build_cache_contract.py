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


def test_source_installer_preserves_cache_and_data_by_default():
    script = _read("ops/container/install.sh")
    assert "builder prune" not in script
    assert "image prune" not in script
    assert "date +%s" not in script
    assert 'SAFENT_FACTORY_RESET:-0' in script
    assert 'SAFENT_FACTORY_RESET:-0}" = "1"' in script
