//! Pure selection logic for which locally-cached Safent images are safe to
//! remove once a boot has confirmed the engine/companion containers already
//! run the digests `runtime-bundle.json`/`runtime-manifest.lock` pinned.
//!
//! Real incident (owner's Mac, 16-sep): after installing 0.9.31 the boot
//! failed at `pull_engine` with "no space left on device" inside the
//! bundled podman machine's own container storage — 8 superseded engine
//! images and 6 superseded companion images had accumulated because nothing
//! ever pruned one after an update. `engine_adapter.rs` lists local images
//! via `podman image ls --format json` and hands them to [`images_to_remove`]
//! here, then removes exactly what comes back — the same "pure planner /
//! impure executor" split `reconcile.rs` already uses for `RepairAction`.

use crate::domain::ImageRef;

/// One image `podman image ls --format json` reports as present locally —
/// one entry per `RepoDigests` value (an image this product ever pulled,
/// under a single reference, always produces exactly one).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalImage {
    pub repository: String,
    pub digest: String,
    pub created_unix: u64,
    /// `podman image ls`'s own `Containers` count, `> 0`. Never a removal
    /// candidate: forcing a live container's image out from under it is
    /// never what "superseded" means. `engine_adapter.rs`'s `remove_image`
    /// also never passes `--force` — two independent guards against the
    /// same mistake, not one relied on alone.
    pub in_use: bool,
}

/// Every local image in one of `our_repositories`, NOT pinned by `pinned`,
/// NOT `in_use`, and NOT among the `keep_rollback` most-recently-created
/// survivors of ITS OWN repository — kept on purpose, so an update that
/// turns out bad still has its immediately previous image on disk.
///
/// Images outside `our_repositories` are never even considered: a caller
/// that accidentally lists someone else's image can never have it widen
/// into a removal here — the allow-list, not caller discipline, is what
/// makes "never touch other repositories" true.
pub fn images_to_remove(
    local: &[LocalImage],
    our_repositories: &[&str],
    pinned: &[ImageRef],
    keep_rollback: usize,
) -> Vec<LocalImage> {
    our_repositories
        .iter()
        .flat_map(|repository| prunable_in_repository(local, repository, pinned, keep_rollback))
        .collect()
}

fn prunable_in_repository(
    local: &[LocalImage],
    repository: &str,
    pinned: &[ImageRef],
    keep_rollback: usize,
) -> Vec<LocalImage> {
    let mut candidates: Vec<&LocalImage> = local
        .iter()
        .filter(|image| image.repository == repository)
        .filter(|image| !image.in_use)
        .filter(|image| !is_pinned(image, pinned))
        .collect();
    candidates.sort_by(|a, b| b.created_unix.cmp(&a.created_unix));
    candidates
        .into_iter()
        .skip(keep_rollback)
        .cloned()
        .collect()
}

fn is_pinned(image: &LocalImage, pinned: &[ImageRef]) -> bool {
    pinned
        .iter()
        .any(|p| p.repository == image.repository && p.digest == image.digest)
}

#[cfg(test)]
mod tests {
    use super::*;

    const ENGINE: &str = "ghcr.io/devwspito/safent";
    const COMPANION: &str = "ghcr.io/devwspito/safent-ads";

    fn image(repo: &str, digest: &str, created_unix: u64) -> LocalImage {
        LocalImage {
            repository: repo.into(),
            digest: digest.into(),
            created_unix,
            in_use: false,
        }
    }

    fn pin(repo: &str, digest: &str) -> ImageRef {
        ImageRef::new(repo, digest).unwrap()
    }

    #[test]
    fn keeps_the_pinned_digest_and_one_rollback_per_repository_removes_the_rest() {
        // The real incident, reproduced: 8 superseded engine + 6 superseded
        // companion images, nothing ever pruned.
        let local = vec![
            image(ENGINE, "sha256:current", 500),
            image(ENGINE, "sha256:rollback", 400),
            image(ENGINE, "sha256:ancient-1", 300),
            image(ENGINE, "sha256:ancient-2", 200),
            image(COMPANION, "sha256:c-current", 500),
            image(COMPANION, "sha256:c-rollback", 400),
            image(COMPANION, "sha256:c-ancient", 300),
        ];
        let pinned = vec![
            pin(ENGINE, "sha256:current"),
            pin(COMPANION, "sha256:c-current"),
        ];

        let removed = images_to_remove(&local, &[ENGINE, COMPANION], &pinned, 1);

        let digests: Vec<&str> = removed.iter().map(|i| i.digest.as_str()).collect();
        assert_eq!(digests.len(), 3, "{digests:?}");
        for kept in [
            "sha256:current",
            "sha256:rollback",
            "sha256:c-current",
            "sha256:c-rollback",
        ] {
            assert!(!digests.contains(&kept), "{kept} must survive: {digests:?}");
        }
        for gone in ["sha256:ancient-1", "sha256:ancient-2", "sha256:c-ancient"] {
            assert!(
                digests.contains(&gone),
                "{gone} must be removed: {digests:?}"
            );
        }
    }

    #[test]
    fn an_image_still_backing_a_container_is_never_removed_even_when_superseded() {
        let mut stale = image(ENGINE, "sha256:stale-but-running", 100);
        stale.in_use = true;
        let local = vec![image(ENGINE, "sha256:current", 500), stale];

        let removed = images_to_remove(&local, &[ENGINE], &[pin(ENGINE, "sha256:current")], 0);

        assert!(removed.is_empty());
    }

    #[test]
    fn an_image_outside_our_repositories_is_never_touched() {
        let local = vec![image("ghcr.io/someone-else/tool", "sha256:foreign", 100)];

        let removed = images_to_remove(&local, &[ENGINE], &[], 0);

        assert!(removed.is_empty());
    }

    #[test]
    fn nothing_to_remove_when_everything_is_pinned_or_within_the_rollback_window() {
        let local = vec![
            image(ENGINE, "sha256:current", 500),
            image(ENGINE, "sha256:rollback", 400),
        ];

        let removed = images_to_remove(&local, &[ENGINE], &[pin(ENGINE, "sha256:current")], 1);

        assert!(removed.is_empty());
    }

    #[test]
    fn a_digest_reused_under_a_different_repository_is_not_confused_with_the_pinned_one() {
        let local = vec![image("ghcr.io/devwspito/safent-ads", "sha256:current", 500)];

        let removed = images_to_remove(&local, &[COMPANION], &[pin(ENGINE, "sha256:current")], 0);

        assert_eq!(
            removed.len(),
            1,
            "a shared digest string is not a shared identity"
        );
    }
}
