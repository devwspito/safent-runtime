//! `plan_update` — contracts/update.md §3, "¿hay algo que actualizar?". A pure
//! function: no clock, no filesystem, no network. Both platform keys are
//! caller-supplied so this module never has to know Tauri's own target-string
//! convention (`darwin-aarch64`, ...) or reimplement it — see
//! `tauri_updater.rs` for where the real ones come from.

use super::types::{
    RuntimeManifest, TauriManifest, UpdatePiece, UpdatePieceKind, UpdatePlan, VersionSet,
};

/// Returns `None` when there is NOTHING newer — the caller must not draw an
/// "Actualizar" button in that case (FR-015). Never returns a plan with an
/// empty `pieces` list; that would be the same bug wearing a different shape.
pub fn plan_update(
    current: &VersionSet,
    runtime_manifest: &RuntimeManifest,
    tauri_manifest: &TauriManifest,
    app_platform_key: &str,
    container_platform_key: &str,
) -> Option<UpdatePlan> {
    let to_app = next_app_version(current, tauri_manifest, app_platform_key);
    let to_engine_digest = next_digest(
        &current.engine_digest,
        runtime_manifest.engine.get(container_platform_key),
    );
    let to_companion_digest =
        next_companion_digest(current, runtime_manifest, container_platform_key);

    let mut pieces = Vec::new();
    if to_app.is_some() {
        pieces.push(UpdatePiece {
            kind: UpdatePieceKind::App,
            size_bytes: None,
        });
    }
    if to_engine_digest.is_some() {
        pieces.push(UpdatePiece {
            kind: UpdatePieceKind::Engine,
            size_bytes: None,
        });
    }
    if to_companion_digest.is_some() {
        pieces.push(UpdatePiece {
            kind: UpdatePieceKind::Companion,
            size_bytes: None,
        });
    }
    if pieces.is_empty() {
        return None;
    }

    Some(UpdatePlan {
        from: current.clone(),
        app_must_go_first: current.app < runtime_manifest.min_app_version,
        to_app,
        to_engine_digest,
        to_companion_digest,
        pieces,
    })
}

/// `app`: semver compare against `latest.json` for THIS platform. No entry
/// for the platform -> that piece simply does not enter the plan (never an
/// error: an unserved platform is not "behind", it is out of scope).
fn next_app_version(
    current: &VersionSet,
    tauri_manifest: &TauriManifest,
    app_platform_key: &str,
) -> Option<semver::Version> {
    let entry = tauri_manifest.platforms.get(app_platform_key)?;
    let _ = &entry.signature; // metadata only; UpdatePorts must verify the downloaded artifact
    (tauri_manifest.version > current.app).then(|| tauri_manifest.version.clone())
}

/// `engine`/`companion`: compare DIGESTS, never versions — a digest that did
/// not change means nothing to do even if the human-readable version moved
/// (data-model.md `VersionSet` invariant).
fn next_digest(current_digest: &str, published: Option<&String>) -> Option<String> {
    let published = published?;
    (published != current_digest).then(|| published.clone())
}

/// The companion only enters the plan when it is INSTALLED right now
/// (contracts/update.md §3: "El companero solo entra en el plan si esta instalado").
fn next_companion_digest(
    current: &VersionSet,
    runtime_manifest: &RuntimeManifest,
    container_platform_key: &str,
) -> Option<String> {
    let installed_digest = current.companion_digest.as_deref()?;
    // Single companion today (`safent-ads`) — data-model.md Companion.
    let published = runtime_manifest
        .companion
        .get("safent-ads")?
        .get(container_platform_key);
    next_digest(installed_digest, published)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    const APP_KEY: &str = "linux-aarch64";
    const ENGINE_KEY: &str = "linux/arm64";

    fn v(s: &str) -> semver::Version {
        semver::Version::parse(s).unwrap()
    }

    fn current(app: &str, engine_digest: &str, companion_digest: Option<&str>) -> VersionSet {
        VersionSet {
            app: v(app),
            engine_digest: engine_digest.to_string(),
            companion_digest: companion_digest.map(str::to_string),
        }
    }

    fn tauri_manifest(version: &str, platform_present: bool) -> TauriManifest {
        let mut platforms = HashMap::new();
        if platform_present {
            platforms.insert(
                APP_KEY.to_string(),
                super::super::types::TauriManifestPlatform {
                    signature: "sig".into(),
                    url: "https://example/app.tar.gz".into(),
                },
            );
        }
        TauriManifest {
            version: v(version),
            platforms,
        }
    }

    fn runtime_manifest(
        manifest_version: &str,
        engine_digest: &str,
        companion_digest: Option<&str>,
        min_app_version: &str,
    ) -> RuntimeManifest {
        let mut engine = HashMap::new();
        engine.insert(ENGINE_KEY.to_string(), engine_digest.to_string());
        let mut companion = HashMap::new();
        if let Some(d) = companion_digest {
            let mut per_arch = HashMap::new();
            per_arch.insert(ENGINE_KEY.to_string(), d.to_string());
            companion.insert("safent-ads".to_string(), per_arch);
        }
        RuntimeManifest {
            schema_version: 1,
            version: v(manifest_version),
            engine,
            companion,
            min_app_version: v(min_app_version),
        }
    }

    #[test]
    fn nothing_newer_yields_no_plan_and_no_button() {
        let cur = current("0.2.0", "sha256:aaa", Some("sha256:bbb"));
        let rm = runtime_manifest("0.2.0", "sha256:aaa", Some("sha256:bbb"), "0.1.0");
        let tm = tauri_manifest("0.2.0", true);

        let plan = plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY);

        assert!(
            plan.is_none(),
            "identical digests + identical app version must not produce a plan"
        );
    }

    #[test]
    fn engine_digest_change_alone_produces_a_plan_with_only_the_engine_piece() {
        let cur = current("0.2.0", "sha256:aaa", None);
        let rm = runtime_manifest("0.2.0", "sha256:NEW", None, "0.1.0");
        let tm = tauri_manifest("0.2.0", true);

        let plan = plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY).expect("must produce a plan");

        assert_eq!(
            plan.pieces,
            vec![UpdatePiece {
                kind: UpdatePieceKind::Engine,
                size_bytes: None
            }]
        );
        assert_eq!(plan.to_engine_digest.as_deref(), Some("sha256:NEW"));
        assert!(plan.to_app.is_none());
        assert!(plan.to_companion_digest.is_none());
    }

    #[test]
    fn readable_version_bump_with_same_digest_is_not_an_update() {
        // data-model.md VersionSet invariant: digest equal -> nothing to do,
        // even though runtime_manifest.version (the human-readable label) moved.
        let cur = current("0.2.0", "sha256:aaa", None);
        let rm = runtime_manifest("0.3.0", "sha256:aaa", None, "0.1.0");
        let tm = tauri_manifest("0.2.0", true);

        assert!(plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY).is_none());
    }

    #[test]
    fn companion_digest_change_ignored_when_not_installed() {
        let cur = current("0.2.0", "sha256:aaa", None); // not installed
        let rm = runtime_manifest("0.2.0", "sha256:aaa", Some("sha256:NEW"), "0.1.0");
        let tm = tauri_manifest("0.2.0", true);

        assert!(
            plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY).is_none(),
            "a companion digest change must not surface a plan when the companion isn't installed"
        );
    }

    #[test]
    fn companion_digest_change_included_when_installed() {
        let cur = current("0.2.0", "sha256:aaa", Some("sha256:old-companion"));
        let rm = runtime_manifest("0.2.0", "sha256:aaa", Some("sha256:NEW"), "0.1.0");
        let tm = tauri_manifest("0.2.0", true);

        let plan = plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY).expect("must produce a plan");

        assert_eq!(
            plan.pieces,
            vec![UpdatePiece {
                kind: UpdatePieceKind::Companion,
                size_bytes: None
            }]
        );
        assert_eq!(plan.to_companion_digest.as_deref(), Some("sha256:NEW"));
    }

    #[test]
    fn app_newer_but_platform_missing_from_latest_json_excludes_the_app_piece() {
        let cur = current("0.2.0", "sha256:aaa", None);
        let rm = runtime_manifest("0.2.0", "sha256:aaa", None, "0.1.0");
        let tm = tauri_manifest("0.5.0", /* platform_present = */ false);

        assert!(
            plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY).is_none(),
            "an unserved platform must not be treated as 'behind'"
        );
    }

    #[test]
    fn all_three_pieces_newer_at_once() {
        let cur = current("0.1.0", "sha256:old-engine", Some("sha256:old-companion"));
        let rm = runtime_manifest(
            "0.2.0",
            "sha256:new-engine",
            Some("sha256:new-companion"),
            "0.1.0",
        );
        let tm = tauri_manifest("0.2.0", true);

        let plan = plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY).expect("must produce a plan");

        assert_eq!(plan.pieces.len(), 3);
        assert_eq!(plan.to_app, Some(v("0.2.0")));
        assert_eq!(plan.to_engine_digest.as_deref(), Some("sha256:new-engine"));
        assert_eq!(
            plan.to_companion_digest.as_deref(),
            Some("sha256:new-companion")
        );
    }

    #[test]
    fn app_must_go_first_when_current_app_is_older_than_min_app_version() {
        let cur = current("0.1.0", "sha256:aaa", None);
        let rm = runtime_manifest("0.2.0", "sha256:NEW", None, "0.2.0");
        let tm = tauri_manifest("0.2.0", true);

        let plan = plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY).expect("must produce a plan");

        assert!(plan.app_must_go_first);
    }

    #[test]
    fn app_goes_after_engine_by_default() {
        let cur = current("0.2.0", "sha256:aaa", None);
        let rm = runtime_manifest("0.2.0", "sha256:NEW", None, "0.1.0"); // min <= current
        let tm = tauri_manifest("0.2.0", true);

        let plan = plan_update(&cur, &rm, &tm, APP_KEY, ENGINE_KEY).expect("must produce a plan");

        assert!(!plan.app_must_go_first);
    }
}
