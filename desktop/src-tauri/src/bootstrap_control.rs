//! Serializes native bootstrap attempts and routes cancel to the exact live
//! signal. A new attempt never resets a signal still held by an old worker.
use crate::ports::CancelSignal;
use std::sync::{Arc, Mutex};

#[derive(Default)]
struct ActiveState {
    latest_id: u64,
    signal: Option<CancelSignal>,
    updating_app: bool,
    companion_signal: Option<CancelSignal>,
}
#[derive(Clone, Default)]
pub(crate) struct BootstrapControl(Arc<Mutex<ActiveState>>);

impl BootstrapControl {
    pub fn begin(&self, expected_id: Option<u64>) -> Result<BootstrapAttempt, &'static str> {
        let mut active = self.0.lock().map_err(|_| "bootstrap_control_unavailable")?;
        if expected_id.is_some_and(|id| id != active.latest_id) {
            return Err("stale_bootstrap_attempt");
        }
        if active.signal.is_some() || active.updating_app || active.companion_signal.is_some() {
            return Err("bootstrap_in_progress");
        }
        active.latest_id = active
            .latest_id
            .checked_add(1)
            .ok_or("bootstrap_control_unavailable")?;
        let signal = CancelSignal::new();
        active.signal = Some(signal.clone());
        Ok(BootstrapAttempt {
            control: self.clone(),
            signal,
            id: active.latest_id,
        })
    }

    pub fn cancel(&self, expected_id: u64) -> Result<(), &'static str> {
        let active = self.0.lock().map_err(|_| "bootstrap_control_unavailable")?;
        if expected_id != active.latest_id {
            return Err("stale_bootstrap_attempt");
        }
        let signal = active.signal.as_ref().ok_or("bootstrap_not_running")?;
        signal.set();
        Ok(())
    }

    /// Replacing the app also replaces its bundled CLI. Exclude bootstrap
    /// without changing its attempt ID, signal or authorizing cancellation.
    pub fn reserve_update(&self) -> Result<AppUpdateGuard, &'static str> {
        let mut active = self.0.lock().map_err(|_| "bootstrap_control_unavailable")?;
        if active.signal.is_some() || active.updating_app || active.companion_signal.is_some() {
            return Err("bootstrap_in_progress");
        }
        active.updating_app = true;
        Ok(AppUpdateGuard(self.clone()))
    }

    /// A closed companion request is neither a new bootstrap attempt nor an
    /// app update. Hold the same exclusion without changing loader identity.
    pub fn reserve_companion(&self, signal: CancelSignal) -> Result<CompanionGuard, &'static str> {
        let mut active = self.0.lock().map_err(|_| "bootstrap_control_unavailable")?;
        if active.signal.is_some() || active.updating_app || active.companion_signal.is_some() {
            return Err("bootstrap_in_progress");
        }
        active.companion_signal = Some(signal);
        Ok(CompanionGuard(self.clone()))
    }
}

pub(crate) struct CompanionGuard(BootstrapControl);
impl Drop for CompanionGuard {
    fn drop(&mut self) {
        if let Ok(mut active) = self.0 .0.lock() {
            active.companion_signal = None;
        }
    }
}

pub(crate) struct AppUpdateGuard(BootstrapControl);
impl Drop for AppUpdateGuard {
    fn drop(&mut self) {
        if let Ok(mut active) = self.0 .0.lock() {
            active.updating_app = false;
        }
    }
}

/// Owns the running slot through observation, repair and final notification.
/// Thread-spawn failure or unwinding drops this guard and frees the slot.
pub(crate) struct BootstrapAttempt {
    control: BootstrapControl,
    pub signal: CancelSignal,
    pub id: u64,
}

impl Drop for BootstrapAttempt {
    fn drop(&mut self) {
        if let Ok(mut active) = self.control.0.lock() {
            active.signal = None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn companion_excludes_boot_and_update_without_changing_loader_identity() {
        let control = BootstrapControl::default();
        let work = control.reserve_companion(CancelSignal::new()).unwrap();
        assert!(control.begin(Some(0)).is_err());
        assert!(control.reserve_update().is_err());
        assert!(control.reserve_companion(CancelSignal::new()).is_err());
        drop(work);
        let boot = control.begin(Some(0)).unwrap();
        assert_eq!(boot.id, 1);
        assert!(control.reserve_companion(CancelSignal::new()).is_err());
        drop(boot);
        let update = control.reserve_update().unwrap();
        assert!(control.reserve_companion(CancelSignal::new()).is_err());
        drop(update);
        assert!(control.reserve_companion(CancelSignal::new()).is_ok());
    }
    #[test]
    fn app_update_and_bootstrap_are_mutually_exclusive_without_changing_attempt_id() {
        let control = BootstrapControl::default();
        let boot = control.begin(None).unwrap();
        let id = boot.id;
        assert!(control.reserve_update().is_err());
        drop(boot);
        let updating = control.reserve_update().unwrap();
        assert!(control.begin(Some(id)).is_err());
        assert!(control.reserve_update().is_err());
        drop(updating);
        assert!(control.begin(Some(id)).is_ok());
    }
    #[test]
    fn cancel_retry_cancel_targets_the_new_active_signal() {
        let control = BootstrapControl::default();
        let first = control.begin(None).unwrap();
        let first_id = first.id;
        let old_signal = first.signal.clone();
        control.cancel(first_id).unwrap();
        assert!(first.signal.is_set());
        drop(first);
        let retry = control.begin(Some(first_id)).unwrap();
        assert!(!retry.signal.is_set());
        assert!(
            old_signal.is_set(),
            "new attempt must not reset the old worker's signal"
        );
        assert_eq!(control.cancel(first_id), Err("stale_bootstrap_attempt"));
        assert!(!retry.signal.is_set());
        control.cancel(retry.id).unwrap();
        control.cancel(retry.id).unwrap();
        assert!(retry.signal.is_set());
    }
    #[test]
    fn duplicate_retry_never_clears_pending_cancel_or_opens_a_second_attempt() {
        let control = BootstrapControl::default();
        let attempt = control.begin(None).unwrap();
        let id = attempt.id;
        control.cancel(id).unwrap();
        assert!(matches!(
            control.begin(Some(id)),
            Err("bootstrap_in_progress")
        ));
        assert!(attempt.signal.is_set());
        drop(attempt);
        let next = control.begin(Some(id)).unwrap();
        drop(next);
        assert!(matches!(
            control.begin(Some(id)),
            Err("stale_bootstrap_attempt")
        ));
    }
    #[test]
    fn cancel_without_attempt_does_not_cancel_a_future_attempt() {
        let control = BootstrapControl::default();
        assert_eq!(control.cancel(0), Err("bootstrap_not_running"));
        let attempt = control.begin(None).unwrap();
        let id = attempt.id;
        assert!(!attempt.signal.is_set());
        drop(attempt);
        assert_eq!(control.cancel(id), Err("bootstrap_not_running"));
        assert!(!control.begin(Some(id)).unwrap().signal.is_set());
    }
    #[test]
    fn simultaneous_retries_have_exactly_one_winner() {
        let control = BootstrapControl::default();
        let barrier = Arc::new(std::sync::Barrier::new(9));
        let handles: Vec<_> = (0..8)
            .map(|_| {
                let control = control.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    let attempt = control.begin(Some(0)).ok();
                    barrier.wait();
                    attempt.is_some()
                })
            })
            .collect();
        barrier.wait();
        assert_eq!(
            handles
                .into_iter()
                .map(|handle| handle.join().unwrap())
                .filter(|won| *won)
                .count(),
            1
        );
        assert!(control.begin(Some(1)).is_ok());
    }
    #[test]
    fn unwinding_releases_the_slot() {
        let control = BootstrapControl::default();
        let _ = std::panic::catch_unwind(|| {
            let _attempt = control.begin(None).unwrap();
            panic!("test worker failed");
        });
        assert!(control.begin(None).is_ok());
    }
}
