//! Native companion-only request consumer. The shared CLI owns claim, lease,
//! progress and terminal status. This worker never reads markers or starts the
//! legacy update/uninstall agent. It exists for the app lifetime, including
//! when its window is hidden.
use crate::bootstrap_control::BootstrapControl;
use crate::ports::CancelSignal;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

#[derive(Default)]
pub(crate) struct CompanionRequests {
    shutdown: Arc<(Mutex<bool>, Condvar)>,
    signal: CancelSignal,
    worker: Mutex<Option<JoinHandle<()>>>,
    exit_started: AtomicBool,
    exit_finished: AtomicBool,
}

impl CompanionRequests {
    pub fn begin_exit(&self) -> bool {
        !self.exit_started.swap(true, Ordering::SeqCst)
    }

    pub fn exit_finished(&self) -> bool {
        self.exit_finished.load(Ordering::SeqCst)
    }
    pub fn finish_exit(&self) {
        self.exit_finished.store(true, Ordering::SeqCst);
    }

    /// Idempotent after a successful boot/reconnect. The exact pinned config
    /// is captured by the caller, not reread from untrusted request metadata.
    pub fn start(
        &self,
        control: BootstrapControl,
        tick: impl Fn(&CancelSignal) + Send + 'static,
    ) -> Result<(), &'static str> {
        self.start_with_interval(control, tick, Duration::from_secs(3))
    }

    fn start_with_interval(
        &self,
        control: BootstrapControl,
        tick: impl Fn(&CancelSignal) + Send + 'static,
        interval: Duration,
    ) -> Result<(), &'static str> {
        let mut worker = self
            .worker
            .lock()
            .map_err(|_| "companion_consumer_unavailable")?;
        if worker.is_some() {
            return Ok(());
        }
        let shutdown = self.shutdown.clone();
        if *shutdown
            .0
            .lock()
            .map_err(|_| "companion_consumer_unavailable")?
        {
            return Err("companion_consumer_stopped");
        }
        let signal = self.signal.clone();
        *worker = Some(
            std::thread::Builder::new()
                .name("safent-companion-requests".into())
                .spawn(move || loop {
                    if signal.is_set() {
                        break;
                    }
                    if let Ok(_guard) = control.reserve_companion(signal.clone()) {
                        if !signal.is_set() {
                            tick(&signal);
                        }
                    }
                    let Ok(stopping) = shutdown.0.lock() else {
                        break;
                    };
                    let Ok((stopping, _)) =
                        shutdown
                            .1
                            .wait_timeout_while(stopping, interval, |stopping| !*stopping)
                    else {
                        break;
                    };
                    if *stopping {
                        break;
                    }
                })
                .map_err(|_| "companion_consumer_spawn_failed")?,
        );
        Ok(())
    }

    /// Call off the UI thread: waits for the current bounded CLI invocation.
    /// Idle/pending work stops immediately. Once the closed request command
    /// starts, it drains under the driver's stall/hard timeouts before engine
    /// shutdown; a gesture cannot kill a possibly active migration.
    pub fn stop(&self) {
        if let Ok(mut stopping) = self.shutdown.0.lock() {
            *stopping = true;
        }
        self.signal.set();
        self.shutdown.1.notify_all();
        if let Ok(mut worker) = self.worker.lock() {
            if let Some(handle) = worker.take() {
                let _ = handle.join();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc,
    };

    #[test]
    fn starts_once_and_shutdown_cancels_joins_and_releases_exclusion() {
        let worker = CompanionRequests::default();
        let control = BootstrapControl::default();
        let (started_tx, started_rx) = mpsc::channel();
        let count = Arc::new(AtomicUsize::new(0));
        let work_count = count.clone();
        worker
            .start(control.clone(), move |signal| {
                work_count.fetch_add(1, Ordering::SeqCst);
                started_tx.send(()).unwrap();
                while !signal.is_set() {
                    std::thread::yield_now();
                }
            })
            .unwrap();
        worker
            .start(control.clone(), |_| panic!("second worker"))
            .unwrap();
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(control.begin(None).is_err());
        assert!(control.reserve_update().is_err());
        worker.stop();
        worker.stop();
        assert_eq!(count.load(Ordering::SeqCst), 1);
        assert!(control.begin(None).is_ok());
        assert!(worker.start(control, |_| {}).is_err());
    }

    #[test]
    fn idle_shutdown_is_woken_without_waiting_for_poll_interval() {
        let worker = CompanionRequests::default();
        let control = BootstrapControl::default();
        let boot = control.begin(None).unwrap();
        worker
            .start_with_interval(
                control.clone(),
                |_| panic!("cannot run during bootstrap"),
                Duration::from_secs(300),
            )
            .unwrap();
        worker.stop();
        drop(boot);
        assert!(control.reserve_update().is_ok());
    }
}
