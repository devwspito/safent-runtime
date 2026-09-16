//! One-click orchestration — contracts/update.md §4. Sequences the 11 named
//! steps over a caller-supplied `UpdatePorts`, applying data-model.md's
//! `UpdatePlan` invariant: "se aplica entero o se revierte" — any failure from
//! `backup` onward restores the pre-update copy, so the run ends on exactly
//! one of two versions actually running (never a partial one, FR-020/SC-008).
//!
//! This module is coordination, not business logic: `UpdatePorts` is the seam
//! where the real CLI/updater-plugin calls live (see `tauri_updater.rs`); the
//! tests below drive it with an in-memory double, no container/network/process.

use super::types::{UpdateFailure, UpdatePlan};
use semver::Version;
use std::time::Duration;

/// Opaque handle to the pre-update copy `backup()` took. The orchestrator
/// never inspects it — only passes it back to `restore()`.
pub struct BackupHandle(pub String);

/// The seam between this orchestrator and everything effectful: `safent
/// backup`, the embedded CLI's `update --to`, and the Tauri updater plugin.
/// One cohesive trait, not five — every method exists to serve the SAME
/// transaction and is always used together by the SAME single caller.
pub trait UpdatePorts {
    /// contracts/update.md §4 step 2: declare work in progress, pause the
    /// queue, wait up to `max_wait`. Only items the daemon marks resumable are
    /// re-queued (never invented here) — the port either drains in time or not.
    fn quiesce(&self, max_wait: Duration) -> Result<(), UpdateFailure>;
    /// Steps 3+4: fetch every piece by digest, with per-layer resume, then
    /// verify manifest signature + each piece's digest/minisign.
    fn download_and_verify(&self, plan: &UpdatePlan) -> Result<(), UpdateFailure>;
    /// Step 5, the POINT OF NO RETURN: `safent backup` (or equivalent).
    fn backup(&self) -> Result<BackupHandle, UpdateFailure>;
    /// Step 6: recreate the engine container at the new digest.
    fn apply_engine(&self, digest: &str) -> Result<(), UpdateFailure>;
    /// Step 7: `compose up` the companion at the new digest + migrations.
    fn apply_companion(&self, digest: &str) -> Result<(), UpdateFailure>;
    /// Step 8+9: install the new wrapper and relaunch. Never returns on a
    /// real relaunch (the process exits) — only in the test double.
    fn apply_app_and_relaunch(&self, target: &Version) -> Result<(), UpdateFailure>;
    /// Step 10: engine health + companion bridge + VersionSet coincide.
    fn verify_ready(&self) -> Result<(), UpdateFailure>;
    /// Rollback: restore the step-5 copy and bring the OLD version back up.
    fn restore(&self, handle: &BackupHandle) -> Result<(), UpdateFailure>;
}

#[derive(Debug, PartialEq, Eq)]
pub enum UpdateOutcome {
    /// Step 11: the window ends open on the new version.
    Done { relaunched_to: Version },
    /// A step from 6 to 10 failed; the pre-update copy is back and healthy.
    RolledBack { cause: UpdateFailure },
    /// Failed before the point of no return (steps 1-4) — nothing was ever
    /// touched, so there is nothing to roll back.
    Aborted { cause: UpdateFailure },
    /// A step from 6 to 10 failed AND the restore itself failed. The contract
    /// promises this does not happen (FR-020: "no hay tercer resultado") —
    /// modeled anyway so a violation is a typed failure, never a silent one.
    RollbackFailed {
        update_cause: UpdateFailure,
        restore_cause: UpdateFailure,
    },
}

/// Runs `plan` to completion over `ports`. Pure sequencing: every effect is
/// behind the trait, so this function's own logic is exactly what the tests
/// below exercise.
pub fn run_update(plan: &UpdatePlan, ports: &dyn UpdatePorts, max_wait: Duration) -> UpdateOutcome {
    if let Err(cause) = ports.quiesce(max_wait) {
        return UpdateOutcome::Aborted { cause };
    }
    if let Err(cause) = ports.download_and_verify(plan) {
        return UpdateOutcome::Aborted { cause };
    }
    let backup = match ports.backup() {
        Ok(handle) => handle,
        Err(cause) => return UpdateOutcome::Aborted { cause },
    };

    match apply_pieces_in_order(plan, ports) {
        Ok(relaunched_to) => UpdateOutcome::Done { relaunched_to },
        Err(cause) => match ports.restore(&backup) {
            Ok(()) => UpdateOutcome::RolledBack { cause },
            Err(restore_cause) => UpdateOutcome::RollbackFailed {
                update_cause: cause,
                restore_cause,
            },
        },
    }
}

/// Steps 6-10. Order matters: the wrapper goes AFTER the engine by default,
/// but BEFORE it when `app_must_go_first` (contracts/update.md §4 "Orden
/// obligatorio") — an old wrapper that can still serve a new engine beats a
/// new wrapper that cannot start at all.
fn apply_pieces_in_order(
    plan: &UpdatePlan,
    ports: &dyn UpdatePorts,
) -> Result<Version, UpdateFailure> {
    let apply_app = |p: &UpdatePlan, ports: &dyn UpdatePorts| -> Result<(), UpdateFailure> {
        if let Some(target) = &p.to_app {
            ports.apply_app_and_relaunch(target)?;
        }
        Ok(())
    };

    if plan.app_must_go_first {
        apply_app(plan, ports)?;
    }
    if let Some(digest) = &plan.to_engine_digest {
        ports.apply_engine(digest)?;
    }
    if let Some(digest) = &plan.to_companion_digest {
        ports.apply_companion(digest)?;
    }
    if !plan.app_must_go_first {
        apply_app(plan, ports)?;
    }
    ports.verify_ready()?;

    Ok(plan.to_app.clone().unwrap_or_else(|| plan.from.app.clone()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::update::types::{UpdatePiece, UpdatePieceKind, VersionSet};
    use std::cell::RefCell;

    #[derive(Default)]
    struct RecordingPorts {
        calls: RefCell<Vec<&'static str>>,
        fail_at: Option<&'static str>,
        fail_restore: bool,
    }

    impl RecordingPorts {
        fn record_or_fail(&self, step: &'static str) -> Result<(), UpdateFailure> {
            self.calls.borrow_mut().push(step);
            if self.fail_at == Some(step) {
                return Err(UpdateFailure::DigestMismatch);
            }
            Ok(())
        }
    }

    impl UpdatePorts for RecordingPorts {
        fn quiesce(&self, _max_wait: Duration) -> Result<(), UpdateFailure> {
            self.record_or_fail("quiesce")
        }
        fn download_and_verify(&self, _plan: &UpdatePlan) -> Result<(), UpdateFailure> {
            self.record_or_fail("download_and_verify")
        }
        fn backup(&self) -> Result<BackupHandle, UpdateFailure> {
            self.record_or_fail("backup")
                .map(|_| BackupHandle("backup-1".into()))
        }
        fn apply_engine(&self, _digest: &str) -> Result<(), UpdateFailure> {
            self.record_or_fail("apply_engine")
        }
        fn apply_companion(&self, _digest: &str) -> Result<(), UpdateFailure> {
            self.record_or_fail("apply_companion")
        }
        fn apply_app_and_relaunch(&self, _target: &Version) -> Result<(), UpdateFailure> {
            self.record_or_fail("apply_app_and_relaunch")
        }
        fn verify_ready(&self) -> Result<(), UpdateFailure> {
            self.record_or_fail("verify_ready")
        }
        fn restore(&self, _handle: &BackupHandle) -> Result<(), UpdateFailure> {
            self.calls.borrow_mut().push("restore");
            if self.fail_restore {
                return Err(UpdateFailure::EngineUnhealthyAfterApply);
            }
            Ok(())
        }
    }

    fn v(s: &str) -> Version {
        Version::parse(s).unwrap()
    }

    fn full_plan() -> UpdatePlan {
        UpdatePlan {
            from: VersionSet {
                app: v("0.1.0"),
                engine_digest: "sha256:old".into(),
                companion_digest: Some("sha256:old-c".into()),
            },
            to_app: Some(v("0.2.0")),
            to_engine_digest: Some("sha256:new".into()),
            to_companion_digest: Some("sha256:new-c".into()),
            pieces: vec![
                UpdatePiece {
                    kind: UpdatePieceKind::App,
                    size_bytes: None,
                },
                UpdatePiece {
                    kind: UpdatePieceKind::Engine,
                    size_bytes: None,
                },
                UpdatePiece {
                    kind: UpdatePieceKind::Companion,
                    size_bytes: None,
                },
            ],
            app_must_go_first: false,
        }
    }

    #[test]
    fn happy_path_visits_every_step_in_order_and_ends_done() {
        let ports = RecordingPorts::default();

        let outcome = run_update(&full_plan(), &ports, Duration::from_secs(1));

        assert_eq!(
            outcome,
            UpdateOutcome::Done {
                relaunched_to: v("0.2.0")
            }
        );
        assert_eq!(
            *ports.calls.borrow(),
            vec![
                "quiesce",
                "download_and_verify",
                "backup",
                "apply_engine",
                "apply_companion",
                "apply_app_and_relaunch",
                "verify_ready"
            ]
        );
    }

    #[test]
    fn app_must_go_first_reorders_app_before_engine_and_companion() {
        let mut plan = full_plan();
        plan.app_must_go_first = true;
        let ports = RecordingPorts::default();

        let outcome = run_update(&plan, &ports, Duration::from_secs(1));

        assert_eq!(
            outcome,
            UpdateOutcome::Done {
                relaunched_to: v("0.2.0")
            }
        );
        assert_eq!(
            *ports.calls.borrow(),
            vec![
                "quiesce",
                "download_and_verify",
                "backup",
                "apply_app_and_relaunch",
                "apply_engine",
                "apply_companion",
                "verify_ready"
            ]
        );
    }

    #[test]
    fn failure_before_backup_aborts_without_ever_calling_restore() {
        let ports = RecordingPorts {
            fail_at: Some("download_and_verify"),
            ..Default::default()
        };

        let outcome = run_update(&full_plan(), &ports, Duration::from_secs(1));

        assert_eq!(
            outcome,
            UpdateOutcome::Aborted {
                cause: UpdateFailure::DigestMismatch
            }
        );
        assert!(
            !ports.calls.borrow().contains(&"restore"),
            "nothing was touched yet — there is nothing to roll back"
        );
    }

    #[test]
    fn engine_apply_failure_rolls_back() {
        let ports = RecordingPorts {
            fail_at: Some("apply_engine"),
            ..Default::default()
        };

        let outcome = run_update(&full_plan(), &ports, Duration::from_secs(1));

        assert_eq!(
            outcome,
            UpdateOutcome::RolledBack {
                cause: UpdateFailure::DigestMismatch
            }
        );
        assert_eq!(ports.calls.borrow().last(), Some(&"restore"));
        // The old version must be the one reported as running again.
    }

    #[test]
    fn companion_migration_failure_rolls_back() {
        let ports = RecordingPorts {
            fail_at: Some("apply_companion"),
            ..Default::default()
        };

        let outcome = run_update(&full_plan(), &ports, Duration::from_secs(1));

        assert_eq!(
            outcome,
            UpdateOutcome::RolledBack {
                cause: UpdateFailure::DigestMismatch
            }
        );
    }

    #[test]
    fn relaunch_failure_rolls_back() {
        let ports = RecordingPorts {
            fail_at: Some("apply_app_and_relaunch"),
            ..Default::default()
        };

        let outcome = run_update(&full_plan(), &ports, Duration::from_secs(1));

        assert_eq!(
            outcome,
            UpdateOutcome::RolledBack {
                cause: UpdateFailure::DigestMismatch
            }
        );
    }

    #[test]
    fn post_apply_health_check_failure_rolls_back() {
        let ports = RecordingPorts {
            fail_at: Some("verify_ready"),
            ..Default::default()
        };

        let outcome = run_update(&full_plan(), &ports, Duration::from_secs(1));

        assert_eq!(
            outcome,
            UpdateOutcome::RolledBack {
                cause: UpdateFailure::DigestMismatch
            }
        );
    }

    #[test]
    fn restore_itself_failing_surfaces_as_a_typed_double_failure_not_silently() {
        let ports = RecordingPorts {
            fail_at: Some("apply_engine"),
            fail_restore: true,
            ..Default::default()
        };

        let outcome = run_update(&full_plan(), &ports, Duration::from_secs(1));

        assert_eq!(
            outcome,
            UpdateOutcome::RollbackFailed {
                update_cause: UpdateFailure::DigestMismatch,
                restore_cause: UpdateFailure::EngineUnhealthyAfterApply,
            }
        );
    }

    #[test]
    fn plan_with_only_an_engine_piece_never_calls_apply_app() {
        let plan = UpdatePlan {
            from: VersionSet {
                app: v("0.2.0"),
                engine_digest: "sha256:old".into(),
                companion_digest: None,
            },
            to_app: None,
            to_engine_digest: Some("sha256:new".into()),
            to_companion_digest: None,
            pieces: vec![UpdatePiece {
                kind: UpdatePieceKind::Engine,
                size_bytes: None,
            }],
            app_must_go_first: false,
        };
        let ports = RecordingPorts::default();

        let outcome = run_update(&plan, &ports, Duration::from_secs(1));

        assert_eq!(outcome, UpdateOutcome::Done { relaunched_to: v("0.2.0") }, "relaunched_to falls back to the CURRENT app version when the app itself wasn't part of the plan");
        assert!(!ports.calls.borrow().contains(&"apply_app_and_relaunch"));
    }
}
