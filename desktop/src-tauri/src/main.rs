// Prevent an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// Safent desktop — a THIN native-webview shell over the local Safent web UI.
//
// Its ONLY own screen is the install/loading animation; everything else is the web UI.
// On a fresh machine it fires the SAME one-line curl bootstrap the user would run by
// hand (get-safent.sh: installs the `safent` CLI, pulls the image, starts the cage,
// installs the UI-update agent), streaming its progress to the animation. Then it asks
// `safent url` for the local URL and loads it in the platform webview (WKWebView /
// WebView2 / WebKitGTK). All product + container logic stays in the `safent` CLI /
// the container — the shell adds none.

use std::io::{BufRead, BufReader, Read};
use std::process::{Command, Stdio};
use tauri::Manager;

const BOOTSTRAP_URL: &str =
    "https://raw.githubusercontent.com/devwspito/safent-runtime/main/get-safent.sh";

// Diagnostic self-test (opt-in via SAFENT_SELFTEST=1): after the UI loads, run a real
// chat round-trip INSIDE the webview and paint the verdict — proves SSE streaming works
// in this platform's webview.
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

/// GUI apps launched from Finder / the dock inherit a MINIMAL PATH (/usr/bin:/bin:…),
/// so the `safent` script cannot find `podman`/`docker` (installed in /opt/homebrew/bin
/// or /usr/local/bin). Hand every child an augmented PATH covering the common locations.
fn augmented_path() -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Ok(p) = std::env::var("PATH") {
        if !p.is_empty() {
            parts.push(p);
        }
    }
    for p in ["/opt/homebrew/bin", "/usr/local/bin", "/opt/podman/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"] {
        parts.push(p.to_string());
    }
    if let Some(home) = std::env::var_os("HOME") {
        parts.push(format!("{}/.local/bin", home.to_string_lossy()));
    }
    parts.join(":")
}

/// The installed `safent` CLI path, or None if it is not installed yet.
fn installed_safent() -> Option<String> {
    if let Ok(b) = std::env::var("SAFENT_BIN") {
        let b = b.trim();
        if !b.is_empty() && std::path::Path::new(b).is_file() {
            return Some(b.to_string());
        }
    }
    for c in ["/opt/homebrew/bin/safent", "/usr/local/bin/safent", "/usr/bin/safent"] {
        if std::path::Path::new(c).is_file() {
            return Some(c.to_string());
        }
    }
    if let Some(home) = std::env::var_os("HOME") {
        let p = std::path::Path::new(&home).join(".local/bin/safent");
        if p.is_file() {
            return Some(p.to_string_lossy().into_owned());
        }
    }
    None
}

/// True if a container engine (podman preferred, docker accepted) is already installed.
fn has_engine() -> bool {
    let candidates = [
        "/opt/podman/bin/podman",
        "/opt/homebrew/bin/podman",
        "/usr/local/bin/podman",
        "/usr/bin/podman",
        "/opt/homebrew/bin/docker",
        "/usr/local/bin/docker",
        "/usr/bin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ];
    candidates.iter().any(|p| std::path::Path::new(p).is_file())
}

// Install Podman on macOS from the OFFICIAL .pkg (podman.io/docs/installation → macOS
// installer). Downloads the latest universal .pkg and installs it with ONE native admin
// password dialog (no terminal). Fedora CoreOS VM + machine are set up later by `safent`.
#[cfg(target_os = "macos")]
const PODMAN_INSTALL_MAC: &str = r#"set -e
echo "Buscando la última versión de Podman…"
JSON="$(curl -fsSL https://api.github.com/repos/containers/podman/releases/latest)"
URL="$(printf '%s' "$JSON" | grep -oE 'https://[^"]*podman-installer-macos-universal\.pkg' | head -1)"
[ -n "$URL" ] || URL="$(printf '%s' "$JSON" | grep -oE 'https://[^"]*podman-installer-macos-[^"]*\.pkg' | head -1)"
[ -n "$URL" ] || { echo "No encontré el instalador oficial de Podman."; exit 1; }
echo "Descargando Podman…"
curl -fsSL "$URL" -o /tmp/safent-podman.pkg
echo "Instalando Podman (autoriza en la ventana de macOS)…"
osascript -e 'do shell script "installer -pkg /tmp/safent-podman.pkg -target /" with administrator privileges'
rm -f /tmp/safent-podman.pkg 2>/dev/null || true
echo "Podman instalado."
"#;

/// Install the container engine (Podman) from the UI, then continue to load Safent.
/// Fire-and-forget: returns immediately; progress + navigation happen via the window.
#[tauri::command]
fn install_podman(window: tauri::WebviewWindow) {
    std::thread::spawn(move || {
        #[cfg(target_os = "macos")]
        let res = run_and_stream(&window, "/bin/sh", &["-c", PODMAN_INSTALL_MAC]);
        #[cfg(not(target_os = "macos"))]
        let res: Result<(), String> = Err(
            "La instalación con un clic está disponible en macOS. En este sistema instala \
             Podman desde https://podman.io/docs/installation y reabre Safent."
                .to_string(),
        );

        match res {
            Ok(()) => match ensure_and_resolve(&window) {
                Ok(url) => match url.parse::<tauri::Url>() {
                    Ok(parsed) => {
                        let _ = window.navigate(parsed);
                    }
                    Err(e) => show_error(&window, &format!("URL inválida '{url}': {e}")),
                },
                Err(e) => show_error(&window, &e),
            },
            Err(e) => show_error(&window, &format!("No pude instalar Podman: {e}")),
        }
    });
}

fn js_escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('`', "\\`").replace('$', "\\$").replace('<', "\\u003c")
}

/// Push a live status line to the install animation (window.__safentProgress).
fn progress(window: &tauri::WebviewWindow, msg: &str) {
    let m = msg.trim();
    if m.is_empty() {
        return;
    }
    let _ = window.eval(&format!(
        "window.__safentProgress && window.__safentProgress(`{}`);",
        js_escape(m)
    ));
}

/// Show a human error inside the (already-visible) loader window instead of a blank page.
fn show_error(window: &tauri::WebviewWindow, message: &str) {
    eprintln!("safent-desktop: {message}");
    let _ = window.eval(&format!(
        "window.__safentError && window.__safentError(`{}`);",
        js_escape(message)
    ));
}

/// Spawn a command and stream BOTH stdout+stderr, line by line, to the install animation.
/// Err(last line) on non-zero exit. Used for the curl bootstrap.
fn run_and_stream(window: &tauri::WebviewWindow, program: &str, args: &[&str]) -> Result<(), String> {
    let mut child = Command::new(program)
        .args(args)
        .env("PATH", augmented_path())
        .env("SAFENT_NO_BROWSER", "1") // the app shows the UI itself — don't pop the browser
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("no pude ejecutar el instalador: {e}"))?;

    let mut handles = Vec::new();
    let pipes: [Option<Box<dyn Read + Send>>; 2] = [
        child.stdout.take().map(|p| Box::new(p) as Box<dyn Read + Send>),
        child.stderr.take().map(|p| Box::new(p) as Box<dyn Read + Send>),
    ];
    for pipe in pipes {
        if let Some(p) = pipe {
            let w = window.clone();
            handles.push(std::thread::spawn(move || {
                let mut last = String::new();
                for line in BufReader::new(p).lines().map_while(Result::ok) {
                    if !line.trim().is_empty() {
                        progress(&w, &line);
                        last = line;
                    }
                }
                last
            }));
        }
    }
    let status = child.wait().map_err(|e| format!("error esperando el instalador: {e}"))?;
    let mut last = String::new();
    for h in handles {
        if let Ok(l) = h.join() {
            if !l.is_empty() {
                last = l;
            }
        }
    }
    if status.success() {
        Ok(())
    } else if last.is_empty() {
        Err(format!("el instalador salió con código {}", status.code().unwrap_or(-1)))
    } else {
        Err(last)
    }
}

/// Run `sh <safent> url`: stream its stderr (progress) to the animation, capture stdout,
/// return the resolved `http://…/?k=…` URL. `safent url` also pulls/starts on its own.
fn run_url(window: &tauri::WebviewWindow, bin: &str) -> Result<String, String> {
    let mut child = Command::new("/bin/sh")
        .arg(bin)
        .arg("url")
        .env("PATH", augmented_path())
        .env("SAFENT_NO_BROWSER", "1") // the app shows the UI itself — don't pop the browser
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("no pude ejecutar 'safent url': {e}"))?;

    let w = window.clone();
    let stderr = child.stderr.take();
    let stderr_handle = std::thread::spawn(move || {
        let mut last = String::new();
        if let Some(p) = stderr {
            for line in BufReader::new(p).lines().map_while(Result::ok) {
                if !line.trim().is_empty() {
                    progress(&w, &line);
                    last = line;
                }
            }
        }
        last
    });

    let mut out = String::new();
    if let Some(mut p) = child.stdout.take() {
        let _ = p.read_to_string(&mut out);
    }
    let status = child.wait().map_err(|e| format!("error esperando 'safent url': {e}"))?;
    let last_err = stderr_handle.join().unwrap_or_default();

    if !status.success() {
        let detail = if last_err.is_empty() {
            format!("código {}", status.code().unwrap_or(-1))
        } else {
            last_err
        };
        return Err(format!("'safent url' falló: {detail}"));
    }

    let url = out
        .lines()
        .rev()
        .map(str::trim)
        .find(|l| l.starts_with("http"))
        .map(str::to_string)
        .unwrap_or_default();
    if url.is_empty() {
        return Err(if last_err.is_empty() {
            "'safent url' no devolvió una URL".to_string()
        } else {
            format!("'safent url' no devolvió una URL: {last_err}")
        });
    }
    Ok(url)
}

/// Ensure Safent is installed + running (bootstrapping via the curl on first run) and
/// return the local web UI URL, streaming all progress to the install animation.
fn ensure_and_resolve(window: &tauri::WebviewWindow) -> Result<String, String> {
    if let Ok(u) = std::env::var("SAFENT_URL") {
        let u = u.trim().to_string();
        if !u.is_empty() {
            return Ok(u);
        }
    }

    let bin = match installed_safent() {
        Some(b) => b,
        None => {
            // First run on a fresh machine: fire the SAME one-liner the user would run.
            progress(window, "Instalando Safent por primera vez…");
            run_and_stream(window, "/bin/sh", &["-c", &format!("curl -fsSL {BOOTSTRAP_URL} | sh")])?;
            installed_safent().ok_or_else(|| {
                "El instalador terminó pero no encuentro el comando 'safent'. Abre una \
                 terminal y prueba: safent url"
                    .to_string()
            })?
        }
    };

    run_url(window, &bin)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![install_podman])
        .setup(|app| {
            // macOS: build a menu WITHOUT an Edit submenu. Tauri's default menu puts
            // Cmd+C/V/X/A on the Edit items as key-equivalents, and AppKit resolves those
            // via performKeyEquivalent: BEFORE the keydown reaches the WKWebView — so the
            // page never sees Cmd+V and VncView's paste handler (the existing local→jail
            // xclip → RFB Ctrl+V flow) never fires in Live/Teaching. Dropping the Edit
            // submenu lets those keydowns reach the web content: WebKit's built-in editing
            // handles them in normal inputs (chat), and VncView drives the jailed browser.
            // The app + window submenus stay so Quit and window controls still work.
            #[cfg(target_os = "macos")]
            {
                use tauri::menu::{MenuBuilder, SubmenuBuilder};
                let app_menu = SubmenuBuilder::new(app, "Safent")
                    .about(None)
                    .separator()
                    .services()
                    .separator()
                    .hide()
                    .hide_others()
                    .show_all()
                    .separator()
                    .quit()
                    .build()?;
                let window_menu = SubmenuBuilder::new(app, "Window")
                    .minimize()
                    .maximize()
                    .separator()
                    .close_window()
                    .build()?;
                let menu = MenuBuilder::new(app).item(&app_menu).item(&window_menu).build()?;
                app.set_menu(menu)?;
            }

            let window = app
                .get_webview_window("main")
                .expect("main window must exist (defined in tauri.conf.json)");

            // Do everything OFF the main thread so the install animation stays live and the
            // window never freezes during the (possibly minutes-long) first run.
            std::thread::spawn(move || {
                // No container engine yet → show the one-click "Instalar Podman" screen and
                // wait for the button (which invokes install_podman → continues from there).
                if std::env::var("SAFENT_URL").is_err() && !has_engine() {
                    let _ = window.eval("window.__safentNeedsPodman && window.__safentNeedsPodman();");
                    return;
                }
                match ensure_and_resolve(&window) {
                    Ok(url) => match url.parse::<tauri::Url>() {
                        Ok(parsed) => {
                            if let Err(e) = window.navigate(parsed) {
                                show_error(&window, &format!("no pude abrir '{url}': {e}"));
                            } else if std::env::var("SAFENT_SELFTEST").is_ok() {
                                std::thread::sleep(std::time::Duration::from_secs(7));
                                let _ = window.eval(SELFTEST_JS);
                            }
                        }
                        Err(e) => show_error(&window, &format!("URL inválida '{url}': {e}")),
                    },
                    Err(e) => show_error(&window, &e),
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Safent desktop");
}
