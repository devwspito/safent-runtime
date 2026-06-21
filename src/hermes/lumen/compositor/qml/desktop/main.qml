import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtWayland.Compositor
import QtWayland.Compositor.XdgShell
import QtWayland.Compositor.WlShell
import "api.js" as API
import "." // Tokens singleton — required for Tokens.X references

// Raíz: WaylandCompositor ESTÁNDAR de PySide6 (antes un C++ ClipboardCompositor de
// WhaleOS para retención de portapapeles; lo simplificamos al estándar Qt). El
// portapapeles se hará luego con wl-clipboard vía sysManager (Python).
WaylandCompositor {
    id: comp

    // Socket Wayland — las apps nativas (terminal, LibreOffice, VS Code) se conectan aquí.
    socketName: "wayland-0"

    WaylandOutput {
        sizeFollowsWindow: true
        scaleFactor: 1
        manufacturer: "LumenSO"
        model: "Virtual Display"
        window: Window {
            id: root
            visible: true
            visibility: Window.FullScreen
            flags: Qt.FramelessWindowHint | Qt.Window
            width: Screen.width
            height: Screen.height
            title: "LumenSO"
            color: Tokens.bgVoid

            // ── Base cursor — ensures mouse pointer is always visible ──
            MouseArea {
                anchors.fill: parent
                z: -1000
                acceptedButtons: Qt.NoButton
                hoverEnabled: true
                cursorShape: Qt.ArrowCursor
            }

            // ── Clipboard ──
            // NOTE: No clipboard polling timer here. The old timer was spawning
            // wl-paste processes every 5s, each creating ghost "App" windows.
            // Clipboard is now on-demand only via Ctrl+V shortcut below.
            // ── Clipboard: Ctrl+V for QML TextInput only ──
            // ONLY enabled when a QML TextInput has focus (it has an "insert" method).
            // When a ShellSurfaceItem (Chromium) has focus, this is DISABLED so
            // the key passes through to wl_keyboard → Chromium handles it natively.
            // Uses wl-paste which reads from the same whaleos-0 socket Chromium writes to.
            Shortcut {
                sequence: "Ctrl+V"
                context: Qt.ApplicationShortcut
                enabled: root.activeFocusItem && (typeof root.activeFocusItem.insert === "function")
                onActivated: {
                    var text = sysManager.pasteFromClipboard();
                    if (text.length > 0 && root.activeFocusItem) {
                        root.activeFocusItem.insert(root.activeFocusItem.cursorPosition, text);
                    }
                }
            }

            // ── Clipboard: Ctrl+C for QML TextInput only ──
            // ONLY enabled when a QML TextInput with selected text has focus.
            // Uses wl-copy to write to whaleos-0 clipboard so Chromium can also paste.
            Shortcut {
                sequence: "Ctrl+C"
                context: Qt.ApplicationShortcut
                enabled: root.activeFocusItem && root.activeFocusItem.selectedText !== undefined
                onActivated: {
                    if (root.activeFocusItem && root.activeFocusItem.selectedText) {
                        sysManager.copyToClipboard(root.activeFocusItem.selectedText);
                    }
                }
            }

            // ── Display Scale ──
            property real userScale: 1.0
            readonly property real sf: userScale * Math.max(0.5, Math.min(2.5, Math.min(root.width / 1024.0, root.height / 768.0)))

            // ── Global State / gate de arranque (3 estados) ──
            // 1) sin cuenta (sentinel ausente) → FirstBootWizard (crear usuario+
            //    contraseña + idioma/teclado). 2) con cuenta y sin login → LoginScreen
            //    (PAM real). 3) autenticado → Desktop. En hardware físico nadie entra
            //    sin la contraseña creada en el onboarding.
            property bool onboardingDone: true   // se recalcula en Component.onCompleted
            property bool loggedIn: false
            property string currentUser: "lumen"
            property string sessionId: ""
            property var openWindows: []
            property string settingsOpenTab: ""  // Set before opening Settings to jump to a tab

            // ── Teaching overlay state — written by SkillsApp, read by the overlay ──
            property string activeTeachingSession: ""
            property string activeTeachingSkillName: ""
            property bool teachingStopBusy: false
            signal stopActiveTeachingRequested()
            // Emitida tras firmar OK — SkillsApp (si está montado) refresca su lista.
            signal teachingSignedOk()
            // Emitida si stop o sign fallan — SkillsApp resetea su estado busy.
            signal teachingSignFailed()

            // "Detener y guardar" REAL desde el overlay GLOBAL. Antes solo emitía
            // una señal que únicamente SkillsApp escuchaba; al cambiar de app
            // SkillsApp se desmonta y el botón quedaba huérfano ("no hace nada").
            // Además /stop NO firma: solo pasa la sesión a 'review'. El único que
            // firma+persiste la skill es /sign. Aquí encadenamos stop → sign para
            // que "guardar" guarde de verdad (y el toast no mienta).
            readonly property string teachingApiBase: "http://127.0.0.1:7517/api/v1/training"
            function stopAndSignTeaching() {
                var sid = activeTeachingSession;
                if (!sid || teachingStopBusy) return;
                teachingStopBusy = true;
                var stopXhr = new XMLHttpRequest();
                stopXhr.onreadystatechange = function() {
                    if (stopXhr.readyState !== XMLHttpRequest.DONE) return;
                    if (stopXhr.status < 200 || stopXhr.status >= 300) {
                        root.teachingStopBusy = false;
                        root.showToast("No se pudo detener — HTTP " + stopXhr.status, "error");
                        root.teachingSignFailed();   // resetea el panel SkillsApp
                        return;
                    }
                    var signXhr = new XMLHttpRequest();
                    signXhr.onreadystatechange = function() {
                        if (signXhr.readyState !== XMLHttpRequest.DONE) return;
                        root.teachingStopBusy = false;
                        if (signXhr.status >= 200 && signXhr.status < 300) {
                            root.activeTeachingSession = "";
                            root.activeTeachingSkillName = "";
                            root.showToast("Skill capturada y firmada", "success");
                            root.teachingSignedOk();
                        } else {
                            root.showToast("Capturada pero no se pudo firmar — HTTP " + signXhr.status, "error");
                            root.teachingSignFailed();   // resetea el panel SkillsApp
                        }
                    };
                    signXhr.open("POST", root.teachingApiBase + "/" + sid + "/sign");
                    signXhr.setRequestHeader("Content-Type", "application/json");
                    signXhr.send("{}");
                };
                stopXhr.open("POST", root.teachingApiBase + "/" + sid + "/stop");
                stopXhr.setRequestHeader("Content-Type", "application/json");
                stopXhr.send("{}");
            }

            // ── Open App helper (usable from Desktop, context menu, etc.) ──
            // searchName/cmd are optional — used for native app surface matching
            function openAppWindow(appId, title, icon, searchName, cmd) {
                for (var i = 0; i < openWindows.length; i++) {
                    if (openWindows[i].appId === appId) return;
                }
                var wins = openWindows.slice();
                wins.push({ appId: appId, title: title, icon: icon, searchName: searchName || "", cmd: cmd || "" });
                openWindows = wins;
            }

            // ── Launch a native app directly (no loader window) ──
            function launchNative(cmd, searchName) {
                if (!cmd || cmd.length === 0) return;
                sysManager.launchNativeApp(cmd);
            }

            // Diagnóstico: si el binario no existe o crashea, mostramos el motivo
            // real en un toast en vez del "Launching..." infinito que enmascaraba
            // qué pasaba. También cerramos las AppWindows en estado launching.
            Connections {
                target: sysManager
                function onAppLaunchFailed(cmd, reason) {
                    root.showToast(reason + " — " + cmd, "error");
                    var wins = root.openWindows.slice();
                    var filtered = [];
                    for (var i = 0; i < wins.length; i++) {
                        var w = wins[i];
                        // Cierra las AppWindow nativas que estaban esperando surface
                        // y cuyo cmd coincide con el que falló.
                        if (w.cmd && w.cmd === cmd) continue;
                        filtered.push(w);
                    }
                    root.openWindows = filtered;
                }
            }


            // ── Wayland Surface Tracking ──
            property var pendingSurfaces: []   // Surfaces waiting to be assigned to AppWindows
            property int autoSurfaceCounter: 0

            // ── API ──
            property string apiBase: "http://127.0.0.1:7777/dashboard/api"

            // ── Theme shims — delegate to Tokens singleton (design system source of truth).
            // Kept as root aliases so all existing child components that reference
            // root.bgSurface / root.accentGreen / etc. continue to resolve without edits.
            readonly property color bgVoid:        Tokens.bgVoid
            readonly property color bgSurface:     Tokens.bgSurface
            readonly property color bgElevated:    Tokens.bgElevated
            readonly property color bgCard:        Tokens.bgCard
            readonly property color borderColor:   Tokens.borderDefault
            readonly property color borderLight:   Tokens.borderStrong
            readonly property color textPrimary:   Tokens.textPrimary
            readonly property color textSecondary: Tokens.textSecondary
            readonly property color textMuted:     Tokens.textMuted
            // Semantic colour aliases — map WhaleOS names → Sereno equivalents
            readonly property color accentBlue:    Tokens.accentBase     // amber
            readonly property color accentGreen:   Tokens.successBase
            readonly property color accentRed:     Tokens.dangerBase
            readonly property color accentOrange:  Tokens.accentBase
            readonly property color accentPurple:  Tokens.accentHover
            readonly property color accentPink:    Tokens.dangerBase
            readonly property color accentCyan:    Tokens.accentBase
            // Radius shims — scaled in Tokens; expose as root-level ints for legacy sites
            readonly property int radiusSm: Math.round(Tokens.radiusSm * sf)
            readonly property int radiusMd: Math.round(Tokens.radiusMd * sf)
            readonly property int radiusLg: Math.round(Tokens.radiusLg * sf)

            // ── Icon Fonts ──
            // Registered via QFontDatabase in main.cpp before QML loads.
            // Use exact family names from fc-query: "Font Awesome 6 Free" (solid/regular)
            // and "Font Awesome 6 Brands" (brands). Solid style needs weight: Font.Black (900).
            property string iconFont: "Font Awesome 6 Free"
            property string iconFontBrands: "Font Awesome 6 Brands"
            property string iconFontRegular: "Font Awesome 6 Free"

            // FontLoader as secondary registration path (file:// absolute path)
            FontLoader { id: faLoader;       source: "file:///usr/share/fontawesome/webfonts/fa-solid-900.ttf";   onStatusChanged: if(status===FontLoader.Ready) console.log("FA Solid OK:", name) }
            FontLoader { id: faBrandsLoader; source: "file:///usr/share/fontawesome/webfonts/fa-brands-400.ttf";  onStatusChanged: if(status===FontLoader.Ready) console.log("FA Brands OK:", name) }
            FontLoader { id: faRegLoader;    source: "file:///usr/share/fontawesome/webfonts/fa-regular-400.ttf"; onStatusChanged: if(status===FontLoader.Ready) console.log("FA Regular OK:", name) }
            FontLoader { id: systemFont;     source: "" }



            // ── Window Management ──
            property int nextZ: 100

            function bringToFront(win) {
                nextZ++;
                win.z = nextZ;
            }

            // ── Assign a Wayland surface to an AppWindow ──
            function assignSurface(toplevel, xdgSurface) {
                // Try immediate match first
                if (tryMatchSurface(toplevel, xdgSurface)) return;

                // Title/appId may not be set yet — queue for deferred matching
                var pending = pendingSurfaces.slice();
                pending.push({ toplevel: toplevel, xdgSurface: xdgSurface, attempts: 0 });
                pendingSurfaces = pending;
                surfaceMatchTimer.start();
            }

            function tryMatchSurface(toplevel, xdgSurface) {
                var appTitle = toplevel.title || "";
                var appId = toplevel.appId || "";

                // First: try to match an existing AppWindow waiting for a surface
                for (var i = 0; i < openWindows.length; i++) {
                    var win = openWindows[i];
                    if (win.appId && win.appId.indexOf("native-") === 0 && !win.surface) {
                        var searchName = win.searchName || "";
                        if (searchName.length > 0 && (appTitle.length > 0 || appId.length > 0) &&
                            (appTitle.toLowerCase().indexOf(searchName.toLowerCase()) >= 0 ||
                             appId.toLowerCase().indexOf(searchName.toLowerCase()) >= 0)) {
                            var wins = openWindows.slice();
                            wins[i] = {
                                appId: win.appId,
                                title: win.title,
                                icon: win.icon,
                                cmd: win.cmd || "",
                                searchName: win.searchName || "",
                                surface: xdgSurface,
                                toplevel: toplevel
                            };
                            openWindows = wins;
                            return true;
                        }
                    }
                }

                // Second: auto-create an AppWindow for this surface.
                // BUG CRÍTICO (causa de "ninguna app nativa mapea"): el toplevel
                // se crea ANTES de que el cliente fije title/app_id (xdg-shell:
                // get_toplevel → [después] set_title/set_app_id). En ese primer
                // instante appTitle y appId están vacíos. Antes devolvíamos `true`
                // (consumir + descartar) → el surface NUNCA se encolaba para
                // reintento, así que cuando el title llegaba 1 frame después ya
                // estaba tirado y la ventana no aparecía jamás (foot/kgx/qterminal/
                // chromium). Ahora devolvemos `false` → assignSurface lo mete en
                // pendingSurfaces y surfaceMatchTimer reintenta cada 500ms hasta
                // que el title/app_id llegan (o 20 intentos = surface realmente
                // vacío de clipboard/sistema → se descarta de forma natural).
                if (!appTitle && !appId) {
                    return false; // aún sin title — reintentar, NO descartar
                }
                root.autoSurfaceCounter++;
                var windowTitle = appTitle || appId || "App";
                var windowAppId = "native-auto-" + root.autoSurfaceCounter;

                var autoWins = openWindows.slice();
                autoWins.push({
                    appId: windowAppId,
                    title: windowTitle,
                    icon: "generic",
                    cmd: "",
                    searchName: appId || appTitle || "",
                    surface: xdgSurface,
                    toplevel: toplevel
                });
                openWindows = autoWins;
                console.log("LumenSO: Auto-created window for surface: " + windowTitle + " (id: " + windowAppId + ")");
                return true;
            }

            // Timer to retry matching pending surfaces
            Timer {
                id: surfaceMatchTimer
                interval: 500; repeat: true; running: false
                onTriggered: {
                    var remaining = [];
                    for (var i = 0; i < root.pendingSurfaces.length; i++) {
                        var s = root.pendingSurfaces[i];
                        if (root.tryMatchSurface(s.toplevel, s.xdgSurface)) {
                            continue; // matched!
                        }
                        s.attempts++;
                        if (s.attempts < 20) {
                            remaining.push(s);
                        }
                    }
                    root.pendingSurfaces = remaining;
                    if (remaining.length === 0) surfaceMatchTimer.stop();
                }
            }

            // ── First-Boot Wizard (sin cuenta aún) ──
            Loader {
                id: wizardLoader
                anchors.fill: parent
                active: !root.onboardingDone
                source: "FirstBootWizard.qml"
            }

            // ── Login Screen (cuenta creada, falta autenticar) ──
            Loader {
                id: loginLoader
                anchors.fill: parent
                active: root.onboardingDone && !root.loggedIn
                source: "LoginScreen.qml"
            }

            // ── Desktop (autenticado) ──
            Loader {
                id: desktopLoader
                anchors.fill: parent
                active: root.loggedIn
                source: "Desktop.qml"
            }

            function onLoginSuccess(user, session) {
                currentUser = user;
                sessionId = session;
                API.setSession(session);
                loggedIn = true;
            }

            // Lee el sentinel de cuenta (/var/lib/hermes/account-applied) para decidir
            // si hay que mostrar el onboarding. Lo llama el wizard al terminar.
            function refreshOnboardingState() {
                if (typeof sysManager !== "undefined" && sysManager.accountConfigured)
                    root.onboardingDone = sysManager.accountConfigured();
            }
            Component.onCompleted: root.refreshOnboardingState()

            function doLogout() {
                loggedIn = false;
                currentUser = "";
                sessionId = "";
                openWindows = [];
                // Descartar cualquier instalación armada sin confirmar (defensa
                // en profundidad: que no sobreviva a un logout/login).
                if (installReview) { installReview.pendingInstall = null; installReview.visible = false; }
            }

            // ── Toast Notification System ──
            function showToast(message, type) {
                toastText.text = message;
                toastText.color = Tokens.textPrimary;
                if (type === "success") {
                    toastBg.color = Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.92);
                    toastIcon.text = "✓";
                    toastIcon.color = Tokens.bgVoid;
                } else if (type === "error") {
                    toastBg.color = Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.92);
                    toastIcon.text = "✕";
                    toastIcon.color = Tokens.textPrimary;
                } else {
                    toastBg.color = Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.92);
                    toastIcon.text = "ℹ";
                    toastIcon.color = Tokens.bgVoid;
                }
                toastContainer.opacity = 1.0;
                toastContainer.y = 20;
                toastTimer.restart();
            }

            // Emitido cuando un flujo de instalación gated termina SIN instalar
            // (cancelar/bloqueado/error de scan). Las apps lo escuchan para limpiar
            // su estado busy. reqId = el id de la instalación ("mcp-add-…"/"pkg-install:…").
            signal installResolved(string reqId)

            // ── Gated install: Centro de Seguridad → score → usuario decide ──
            // Toda app (MCP, Apps) llama AQUÍ en vez de instalar directo. El scan
            // se CORRELA por reqId: scan_install_draft DEVUELVE el score a
            // "scangate-"+installReqId (no por la señal global), y el modal se abre
            // desde ESA respuesta. Así un scan bajo demanda del Centro de Seguridad
            // NO puede disparar una instalación pendiente ajena (bypass CRÍTICO).
            //   scanDraft : { kind, identifier, argv?, source_url? }
            //   installVerb: "add_mcp_server" | "install_package" | "install_hub_skill"
            //   installReqId: id de la llamada real (su onResult ya existe en la app)
            //   installArgs: objeto de args del verbo real
            function beginGatedInstall(scanDraft, installVerb, installReqId, installArgs) {
                // Guard: no machacar una instalación ya en curso (las apps además
                // bloquean re-entrada por busy; esto es defensa en profundidad).
                if (installReview.pendingInstall) return;
                var scanReqId = "scangate-" + installReqId;
                installReview.pendingInstall = {
                    verb: installVerb, reqId: installReqId, args: installArgs,
                    scanReqId: scanReqId
                };
                hermes.call(scanReqId, "scan_install_draft",
                    JSON.stringify({ draft_json: JSON.stringify(scanDraft) }));
                scanGateWatchdog.restart();   // si el daemon no responde, no colgar
            }

            // Watchdog: si el pre-scan no responde (daemon colgado), descartar la
            // instalación armada y liberar el busy de la app — nunca dejarla pegada.
            Timer {
                id: scanGateWatchdog
                interval: 15000; repeat: false
                onTriggered: {
                    var pi = installReview.pendingInstall;
                    if (!pi || installReview.visible) return;   // ya resuelto o en modal
                    installReview.pendingInstall = null;
                    installReview.notifyResolved(pi.reqId);
                    root.showToast("El análisis de seguridad no respondió; instalación cancelada", "error");
                }
            }

            // Respuesta del pre-scan gated, correlada por reqId. Conduce el modal
            // SOLO si corresponde al pendingInstall vigente.
            Connections {
                target: hermes
                function onResult(reqId, ok, jsonStr) {
                    var pi = installReview.pendingInstall;
                    if (!pi || reqId !== pi.scanReqId) return;
                    scanGateWatchdog.stop();   // llegó la respuesta
                    if (!ok) {
                        // El daemon falló el scan → no instalar; avisar y limpiar.
                        installReview.pendingInstall = null;
                        installReview.notifyResolved(pi.reqId);
                        showToast("No se pudo analizar la seguridad; instalación cancelada", "error");
                        return;
                    }
                    var data;
                    try { data = JSON.parse(jsonStr || "{}"); }
                    catch (e) { data = { verdict: "FAIL", identifier: "", risks: [] }; }
                    if (data.error) {
                        installReview.pendingInstall = null;
                        installReview.notifyResolved(pi.reqId);
                        showToast("Análisis de seguridad: " + data.error, "error");
                        return;
                    }
                    installReview.openGated(data);
                }
            }

            Item {
                id: toastContainer
                anchors.horizontalCenter: parent.horizontalCenter
                y: Math.round(-60 * root.sf); z: 99999
                width: toastRow.width + Math.round(32 * root.sf); height: Math.round(44 * root.sf)
                opacity: 0.0

                Behavior on opacity { NumberAnimation { duration: 300 } }
                Behavior on y { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }

                Rectangle {
                    id: toastBg
                    anchors.fill: parent; radius: Math.round(Tokens.radiusMd * root.sf)
                    color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.92)

                    Row {
                        id: toastRow
                        anchors.centerIn: parent; spacing: Math.round(8 * root.sf)
                        Text { id: toastIcon; text: "✓"; font.pixelSize: Math.round(16 * root.sf); font.weight: Font.Bold; font.family: Tokens.fontBody; color: Tokens.bgVoid; anchors.verticalCenter: parent.verticalCenter }
                        Text { id: toastText; text: ""; font.pixelSize: Math.round(13 * root.sf); font.weight: Font.DemiBold; font.family: Tokens.fontBody; color: Tokens.bgVoid; anchors.verticalCenter: parent.verticalCenter }
                    }
                }

                Timer {
                    id: toastTimer; interval: 3000
                    onTriggered: { toastContainer.opacity = 0.0; toastContainer.y = Math.round(-60 * root.sf); }
                }
            }

            // ── Teaching Overlay — floats above every window while a skill is being recorded ──
            // Visible from any app so the user never has to switch back to SkillsApp to stop.
            Rectangle {
                id: teachingOverlay
                visible: root.activeTeachingSession.length > 0
                z: 100000
                anchors.top: parent.top
                anchors.topMargin: Math.round(54 * root.sf)   // clears the TopBar (~48px)
                anchors.right: parent.right
                anchors.rightMargin: Math.round(14 * root.sf)
                width: overlayRow.implicitWidth + Math.round(22 * root.sf)
                height: Math.round(40 * root.sf)
                radius: Math.round(Tokens.radiusMd * root.sf)
                color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.95)
                border.width: 1
                border.color: Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.55)

                // Pulsing red dot
                SequentialAnimation on opacity {
                    loops: Animation.Infinite
                    running: teachingOverlay.visible
                    NumberAnimation { to: 0.75; duration: 700; easing.type: Easing.InOutSine }
                    NumberAnimation { to: 1.0;  duration: 700; easing.type: Easing.InOutSine }
                }

                Row {
                    id: overlayRow
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: parent.left
                    anchors.leftMargin: Math.round(11 * root.sf)
                    spacing: Math.round(8 * root.sf)

                    Rectangle {
                        width: Math.round(8 * root.sf); height: width; radius: width / 2
                        color: Tokens.dangerBase
                        anchors.verticalCenter: parent.verticalCenter
                        SequentialAnimation on opacity {
                            loops: Animation.Infinite
                            running: teachingOverlay.visible
                            NumberAnimation { to: 0.4; duration: 500; easing.type: Easing.InOutSine }
                            NumberAnimation { to: 1.0; duration: 500; easing.type: Easing.InOutSine }
                        }
                    }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: "Grabando — " + (root.activeTeachingSkillName || "skill")
                        color: Tokens.textPrimary
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(12 * root.sf)
                        font.weight: Font.DemiBold
                    }

                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: stopLabel.implicitWidth + Math.round(16 * root.sf)
                        height: Math.round(26 * root.sf)
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color: stopArea.containsMouse ? Tokens.dangerSubtle : Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.10)
                        border.width: 1
                        border.color: Tokens.dangerBase

                        Text {
                            id: stopLabel
                            anchors.centerIn: parent
                            text: root.teachingStopBusy ? "⏳ Guardando…" : "⏹ Detener y guardar"
                            color: Tokens.dangerBase
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * root.sf)
                            font.weight: Font.DemiBold
                        }

                        MouseArea {
                            id: stopArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            enabled: !root.teachingStopBusy
                            onClicked: root.stopAndSignTeaching()
                        }
                    }
                }
            }

            // ── InstallReview modal — global overlay for security scan verdicts ──
            InstallReview {
                id: installReview
                anchors.fill: parent
            }

            // ── ApprovalCard — tarjeta ámbar para acciones HIGH-risk del agente ──
            // Sondea list_pending cada 2 s mientras loggedIn sea true.
            // z: 100001 para flotar por encima del overlay de grabación (100000).
            ApprovalCard {
                id: approvalCardOverlay
                anchors.fill: parent
                z: 100001
            }

            // ── SecurityApprovalCard — modal de Modo Guardado ──────────────
            // Driven por la señal ApprovalRequested del daemon (no por sondeo).
            // z: 200100 por encima de todo — el propietario debe decidir antes
            // de que el Cerebro continúe.
            SecurityApprovalCard {
                id: securityApprovalCard
                anchors.fill: parent
                z: 200100
            }

            // ── Security signals from daemon ──
            Connections {
                target: hermes

                function onInstallReviewRequested(scanId, scanDataJson) {
                    // Señal global (scans bajo demanda del Centro de Seguridad,
                    // scan de skills, reviews empujados por el daemon): INFORMATIVO.
                    // NUNCA instala — el camino de instalación es el reply-driven
                    // openGated() correlado por reqId. Así esta señal no puede
                    // disparar un pendingInstall ajeno (bypass CRÍTICO).
                    try {
                        var data = JSON.parse(scanDataJson);
                        installReview.openInfo(scanId, data);
                    } catch(e) {
                        console.log("LumenSO: InstallReviewRequested parse error: " + e);
                    }
                }

                function onScanCompleted(scanId, verdict) {
                    // Update shield icon state: track worst unseen verdict
                    var current = desktopLoader.item
                        ? desktopLoader.item.topBarRef
                        : null;
                    // Walk to topBar via desktopLoader
                    if (verdict === "FAIL") {
                        securityShieldState = "fail";
                    } else if (verdict === "WARN" && securityShieldState !== "fail") {
                        securityShieldState = "warn";
                    }
                }

                function onThreatDetected(threatJson) {
                    securityShieldState = "fail";
                    root.showToast("Security threat detected — open Security Center", "error");
                }

                // Puente activate_app: el daemon (sin sesión) pide lanzar una app y
                // el compositor (en la sesión) la abre de verdad. Esto es lo que hace
                // que "Hermes, abre la calculadora" funcione.
                function onAppLaunchRequested(cmd) {
                    root.showToast("Abriendo " + cmd + "…", "info");
                    root.launchNative(cmd, cmd);
                }

                // Modo Guardado: el daemon emite ApprovalRequested cuando el Cerebro
                // intenta un comando peligroso. Lo encolamos en SecurityApprovalCard.
                function onApprovalRequested(payloadJson) {
                    securityApprovalCard.enqueueRequest(payloadJson);
                }

                // Mantiene root.autoModeOn sincronizado con el daemon para que TopBar
                // pueda mostrar el badge AUTO sin depender del Loader de SettingsApp.
                function onResult(reqId, ok, jsonStr) {
                    if (reqId === "auto-get" || reqId === "auto-set-on" || reqId === "auto-set-off") {
                        if (!ok) return;
                        try {
                            var am = JSON.parse(jsonStr || "{}");
                            if (am.auto_mode !== undefined) root.autoModeOn = am.auto_mode === true;
                        } catch (e) {}
                    }
                }
            }

            // ── Shield state, read by TopBar via Desktop ──
            property string securityShieldState: "idle"

            // ── Modo AUTO state — shared between SettingsApp (writes) and TopBar (reads) ──
            property bool autoModeOn: false

            // ── Gate de proveedor (fundamental) ──
            // Cableado REAL al daemon: si no hay proveedor activo, exige configurarlo
            // (sin proveedor Hermes no piensa). Solo visible cuando hace falta.
            ProviderGate {
                id: providerGate
                anchors.fill: parent
                ui: root
            }
        }
    }

    // ── XDG Shell — handles native app window surfaces ──
    XdgShell {
        onToplevelCreated: function(toplevel, xdgSurface) {
            console.log("LumenSO Compositor: XDG surface — title:" + toplevel.title + " appId:" + toplevel.appId);
            root.assignSurface(toplevel, xdgSurface);
        }
    }

    // ── XDG Decoration — tells clients that the compositor draws window buttons ──
    // LumenSO provides its own title bar with maximize/close for ALL windows.
    // ServerSideDecoration tells apps NOT to draw their own buttons.
    XdgDecorationManagerV1 {
        preferredMode: XdgToplevel.ServerSideDecoration
    }

    // ── WlShell — fallback for older/simpler clients ──
    WlShell {
        onWlShellSurfaceCreated: function(shellSurface) {
            console.log("LumenSO Compositor: WlShell surface — title:" + shellSurface.title + " className:" + shellSurface.className);
            if (root && typeof root.assignSurface === "function") {
                root.assignSurface(shellSurface, shellSurface);
            } else {
                console.log("LumenSO: root not ready, ignoring surface");
            }
        }
    }

}
