//! Build capability, NOT a release check. No updater ports are wired yet.
//! Publishing this before page scripts keeps the loader and product honest
//! without granting the remote webview another native command.

#[derive(serde::Serialize)]
struct NativeUpdaterStatus {
    status: &'static str,
    reason: &'static str,
    app_version: &'static str,
}

pub fn initialization_script() -> String {
    let status = NativeUpdaterStatus {
        status: "unavailable",
        reason: "integration_missing",
        app_version: env!("CARGO_PKG_VERSION"),
    };
    let json = serde_json::to_string(&status).expect("static native updater metadata");
    format!(
        "Object.defineProperty(window, '__safentNativeUpdater', {{value: Object.freeze({json}), writable: false, configurable: false}});"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_only_build_capability_never_a_release_check() {
        let script = initialization_script();
        assert!(script.contains("\"status\":\"unavailable\""));
        assert!(script.contains("\"reason\":\"integration_missing\""));
        assert!(script.contains(&format!(
            "\"app_version\":\"{}\"",
            env!("CARGO_PKG_VERSION")
        )));
        for forbidden in [
            "fetch(",
            "http",
            "latest_version",
            "checked_at",
            "__safentUpdate'",
            "invoke(",
        ] {
            assert!(!script.contains(forbidden), "unexpected {forbidden}");
        }
    }

    #[test]
    fn native_metadata_is_readonly_and_does_not_overwrite_daemon_signal() {
        let script = initialization_script();
        assert!(script.contains("Object.freeze("));
        assert!(script.contains("writable: false, configurable: false"));
        assert!(!script.contains("__safentLatestVersion"));
    }
}
