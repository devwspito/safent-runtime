// Prevent an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// Safent desktop — a THIN native-webview shell over the local Safent web UI.
//
// It adds ZERO product logic: it resolves the SAME URL the browser/curl UI uses (via
// the `safent` CLI) and loads it in the platform webview (WebKitGTK / WKWebView /
// WebView2). All container + secret handling stays in the `safent` CLI (single source).
// Delete the desktop/ directory and the curl + browser flow is completely unaffected.

use std::process::Command;
use tauri::Manager;

// Diagnostic self-test (opt-in via SAFENT_SELFTEST=1): after the daemon UI loads,
// run a real chat round-trip INSIDE the webview — POST a message, open the SSE chat
// stream, count events — and paint the verdict as a banner. This is the streaming
// "spike" the platform webview must pass (WebKitGTK / WKWebView / WebView2); it proves
// same-origin fetch + EventSource work in the app context, not just in a browser.
const SELFTEST_JS: &str = r#"(async () => {
  const b = document.createElement('div');
  b.id = '__safent_selftest';
  b.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483647;background:#0b0d10;color:#5b8cff;font:600 18px/1.4 system-ui,sans-serif;padding:12px 16px;border-bottom:2px solid #5b8cff';
  b.textContent = 'SSE self-test: starting…';
  document.body.appendChild(b);
  try {
    const r = await fetch('/api/v1/chat', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_message:'Responde solo: pong.'})});
    const j = await r.json().catch(() => ({}));
    const task = j.task_id;
    if (!task) { b.textContent = 'SSE self-test: FAIL — no task_id (http ' + r.status + ')'; b.style.color = '#ff6b6b'; return; }
    let events = 0, deltas = 0, done = false;
    const es = new EventSource('/api/v1/chat/stream/' + task);
    const tick = () => { b.textContent = 'SSE self-test: streaming… task=' + task.slice(0,8) + ' events=' + events + ' deltas=' + deltas; };
    es.onmessage = (e) => { events++; try { const d = JSON.parse(e.data); if (d.kind === 'delta' || d.delta) deltas++; if (d.kind === 'done') { done = true; es.close(); } } catch (_) {} tick(); };
    es.onerror = () => {};
    setTimeout(() => {
      try { es.close(); } catch (_) {}
      const ok = events > 0;
      b.textContent = 'SSE self-test: ' + (ok ? 'OK' : 'FAIL') + ' — events=' + events + ' deltas=' + deltas + ' done=' + done;
      b.style.color = ok ? '#4caf50' : '#ff6b6b';
      b.style.borderBottomColor = ok ? '#4caf50' : '#ff6b6b';
    }, 20000);
  } catch (err) { b.textContent = 'SSE self-test: ERROR ' + err; b.style.color = '#ff6b6b'; }
})();"#;

/// Resolve the Safent web UI URL.
///
/// Priority:
///   1. `SAFENT_URL` env — explicit override (dev / testing / advanced users).
///   2. `<SAFENT_BIN or "safent"> url` — ensures the container is running and prints
///      the `http://localhost:PORT/?k=<secret>` URL.
/// Locate the `safent` CLI. GUI apps launched from Finder / the dock / a desktop
/// launcher do NOT inherit the shell PATH, so a bare "safent" fails even when it is
/// installed — probe the common install locations before falling back to PATH.
fn find_safent_bin() -> String {
    if let Ok(b) = std::env::var("SAFENT_BIN") {
        if !b.trim().is_empty() {
            return b;
        }
    }
    for c in ["/usr/local/bin/safent", "/opt/homebrew/bin/safent", "/usr/bin/safent"] {
        if std::path::Path::new(c).is_file() {
            return c.to_string();
        }
    }
    if let Some(home) = std::env::var_os("HOME") {
        let p = std::path::Path::new(&home).join(".local/bin/safent");
        if p.is_file() {
            return p.to_string_lossy().into_owned();
        }
    }
    "safent".to_string() // last resort: rely on PATH (works when launched from a shell)
}

fn resolve_url() -> Result<String, String> {
    if let Ok(u) = std::env::var("SAFENT_URL") {
        let u = u.trim().to_string();
        if !u.is_empty() {
            return Ok(u);
        }
    }

    let bin = find_safent_bin();
    let output = Command::new(&bin)
        .arg("url")
        .output()
        .map_err(|e| format!("could not run '{bin} url': {e}"))?;

    if !output.status.success() {
        return Err(format!(
            "'{bin} url' failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }

    let url = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if url.is_empty() {
        return Err("'safent url' returned no URL".to_string());
    }
    Ok(url)
}

/// Show a human error inside the (already-visible) loader window instead of a blank page.
fn show_error(window: &tauri::WebviewWindow, message: &str) {
    eprintln!("safent-desktop: {message}");
    // Escape for a JS template literal.
    let safe = message
        .replace('\\', "\\\\")
        .replace('`', "\\`")
        .replace('$', "\\$")
        .replace('<', "\\u003c");
    let _ = window.eval(&format!("window.__safentError && window.__safentError(`{safe}`);"));
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let window = app
                .get_webview_window("main")
                .expect("main window must exist (defined in tauri.conf.json)");

            match resolve_url() {
                Ok(url) => match url.parse::<tauri::Url>() {
                    Ok(parsed) => {
                        // Navigate the loader window to the live daemon UI. The page then
                        // runs as a normal same-origin web app (SSE/WS, cookies, the ?k=
                        // handshake) exactly as it does in a browser.
                        if let Err(e) = window.navigate(parsed) {
                            show_error(&window, &format!("could not open '{url}': {e}"));
                        } else if std::env::var("SAFENT_SELFTEST").is_ok() {
                            // Opt-in streaming spike: run the SSE round-trip once the SPA loaded.
                            let w = window.clone();
                            std::thread::spawn(move || {
                                std::thread::sleep(std::time::Duration::from_secs(7));
                                let _ = w.eval(SELFTEST_JS);
                            });
                        }
                    }
                    Err(e) => show_error(&window, &format!("invalid URL '{url}': {e}")),
                },
                Err(e) => show_error(&window, &e),
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Safent desktop");
}
