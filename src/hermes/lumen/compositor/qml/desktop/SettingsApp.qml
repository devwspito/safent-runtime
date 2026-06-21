import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

Rectangle {
    id: settingsApp; anchors.fill: parent; color: "transparent"
    // Responsive: collapse sidebar into a top tab strip when narrow
    readonly property bool _compact: settingsApp.width < Tokens.bpCompact * root.sf
    readonly property var _navItems: [
        { id: "profile",  label: "Perfil" },
        { id: "permisos", label: "Permisos" },
        { id: "remote",   label: "Acceso remoto" },
        { id: "users",    label: "Usuarios" },
        { id: "storage",  label: "Almacenamiento" },
        { id: "display",  label: "Pantalla" }
    ]
    property string activeTab: "profile"
    property var userList: []
    property bool showAddUser: false
    property string newUsername: ""
    property string newPassword: ""
    property bool waConnecting: false
    property string waQrCode: ""
    property bool waConnected: false
    property string tgToken: ""
    property string dcToken: ""
    // Display settings
    property var displayModes: []
    property var currentRes: ({"width": 1920, "height": 1080, "refresh": 60.0})
    property var gpuInfo: ({"name": "Detecting...", "driver": "-", "renderer": "-"})
    property var gfxInfo: ({"modules": "", "compositor": ""})
    property string selectedRes: ""
    property bool resApplied: false
    property int revertCountdown: 0
    // Storage settings
    property string workspaceDir: "/home/ainux/Works"
    property bool workspaceDirSaving: false
    // ── Modo AUTO ────────────────────────────────────────────────────────────
    property bool autoModeOn: false
    property bool autoModeLoading: false
    property bool showAutoWarning: false   // controla el diálogo de advertencia
    function loadAutoMode() {
        hermes.call("auto-get", "get_auto_mode", "{}");
    }
    function confirmEnableAutoMode() {
        showAutoWarning = false;
        autoModeLoading = true;
        hermes.call("auto-set-on", "set_auto_mode", JSON.stringify({ enabled: true }));
    }
    function disableAutoMode() {
        autoModeLoading = true;
        hermes.call("auto-set-off", "set_auto_mode", JSON.stringify({ enabled: false }));
    }

    Component.onCompleted: { loadUsers(); checkChannelStatus(); loadDisplayInfo(); loadWorkspaceDir(); loadConsents(); loadRemoteStatus(); loadAutoMode(); }

    // Watch for settingsOpenTab — context menu sets this to jump to Display tab
    Connections {
        target: root
        function onSettingsOpenTabChanged() {
            if (root.settingsOpenTab === "display") {
                activeTab = "display";
                root.settingsOpenTab = ""; // reset
                loadDisplayInfo();
            }
        }
    }

    // ── Async signal handlers for SystemManager ──
    Connections {
        target: sysManager

        function onUserOpResult(operation, success, detail) {
            if (operation === "addUser") {
                if (success) {
                    root.showToast("User '" + detail + "' created successfully", "success");
                } else {
                    root.showToast("Failed to create user: " + detail, "error");
                }
                showAddUser = false; newUsername = ""; newPassword = ""; loadUsers();
            } else if (operation === "deleteUser") {
                if (success) {
                    root.showToast("User '" + detail + "' deleted", "success");
                } else {
                    root.showToast("Failed to delete user: " + detail, "error");
                }
                loadUsers();
            } else if (operation === "changePassword") {
                if (success) {
                    root.showToast("Password updated successfully", "success");
                } else {
                    root.showToast("Failed to update password: " + detail, "error");
                }
            }
        }

        function onDisplayInfoReady(xrandrText) {
            if (xrandrText && xrandrText.length > 10) {
                var parsed = parseXrandrModes(xrandrText);
                if (parsed.modes.length === 0) {
                    displayModes = defaultDisplayModes();
                    currentRes = {"width": 1280, "height": 800, "refresh": 60.0};
                } else {
                    displayModes = parsed.modes;
                    currentRes = parsed.current;
                    if (currentRes.width === 0) currentRes = {"width": 1280, "height": 800, "refresh": 60.0};
                }
            } else {
                displayModes = defaultDisplayModes();
                currentRes = {"width": 1280, "height": 800, "refresh": 60.0};
            }
            selectedRes = currentRes.width + "x" + currentRes.height;
            // Now fetch GPU info (async)
            sysManager.getGpuInfoAsync();
        }

        function onGpuInfoReady(gpuLine) {
            gpuInfo = {
                "name": gpuLine.length > 5 ? gpuLine.replace(/.*VGA.*:\s*/,"").split("[")[0].trim() : "VirtIO GPU (QEMU)",
                "driver": "virtio_gpu / pixman",
                "renderer": "wlroots (Cage compositor)"
            };
            gfxInfo = {"modules": "virtio_gpu, drm, kms", "compositor": "Cage (wlroots)"};
        }
    }

    function checkChannelStatus() {
        // Los canales de mensajería (WhatsApp/Telegram/Discord) no tienen backend
        // D-Bus en esta versión — se muestran como "Próximamente" en la UI.
        waConnected = false;
    }
    function loadUsers() {
        try {
            var result = sysManager.listUsers();
            userList = JSON.parse(result);
        } catch(e) {
            userList = [{ username: root.currentUser, role: "admin" }];
        }
    }
    function addUser() {
        if (!newUsername || !newPassword) { root.showToast("Username and password required", "error"); return; }
        if (typeof sysManager.addUserAsync === "function") {
            sysManager.addUserAsync(newUsername, newPassword);
        } else {
            var ok = sysManager.addUser(newUsername, newPassword);
            if (ok) { root.showToast("User '" + newUsername + "' created successfully", "success"); }
            else { root.showToast("Failed to create user '" + newUsername + "'", "error"); }
            showAddUser = false; newUsername = ""; newPassword = ""; loadUsers();
        }
    }
    function deleteUser(u) {
        if (u === root.currentUser) { root.showToast("Cannot delete your own account", "error"); return; }
        if (typeof sysManager.deleteUserAsync === "function") {
            sysManager.deleteUserAsync(u);
        } else {
            var ok = sysManager.deleteUser(u);
            if (ok) { root.showToast("User '" + u + "' deleted", "success"); }
            else { root.showToast("Failed to delete user '" + u + "'", "error"); }
            loadUsers();
        }
    }
    function changePassword(currentPass, newPass) {
        if (!currentPass) { root.showToast("Escribe la contraseña actual", "error"); return; }
        if (!newPass) { root.showToast("Escribe la nueva contraseña", "error"); return; }
        if (typeof sysManager.changePasswordAsync === "function") {
            sysManager.changePasswordAsync(currentPass, newPass);
        } else {
            var ok = sysManager.changePassword(currentPass, newPass);
            if (ok) { root.showToast("Password updated successfully", "success"); }
            else { root.showToast("Failed to update password", "error"); }
        }
    }
    // Los canales de mensajería (WhatsApp/Telegram/Discord) no tienen verbo
    // D-Bus en esta versión. Las funciones se mantienen como no-op honestos para
    // que la UI no llame al :7777 muerto. La sección se muestra como "Próximamente".
    property int qrPollCount: 0
    function connectWhatsApp() {
        root.showToast("Canales de mensajería: próximamente", "info");
    }
    function pollWAQR() { /* no-op */ }
    Timer { id: qrTimer; interval: 3000; repeat: false; onTriggered: { /* no-op */ } }
    function connectCh(t, tok) {
        root.showToast("Canales de mensajería: próximamente", "info");
    }
    // Parse xrandr output to extract display modes
    function parseXrandrModes(xrandrText) {
        var modes = [];
        var lines = xrandrText.split("\n");
        var inScreen = false;
        var curW = 0; var curH = 0; var curR = 0;

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            // Screen resolution line: "   1280x800..."
            if (line.match(/^\s+\d+x\d+/)) {
                var parts = line.trim().split(/\s+/);
                var resDims = parts[0].split("x");
                var mW = parseInt(resDims[0]); var mH = parseInt(resDims[1]);
                var mR = 60.0;
                var isCurrent = line.indexOf("*") >= 0;
                var isPreferred = line.indexOf("+") >= 0;
                // Parse refresh rates
                for (var j = 1; j < parts.length; j++) {
                    var rStr = parts[j].replace("*","").replace("+","");
                    var r = parseFloat(rStr);
                    if (!isNaN(r) && r > 0) { mR = r; break; }
                }
                if (isCurrent) { curW = mW; curH = mH; curR = mR; }
                modes.push({"width": mW, "height": mH, "refresh": mR, "preferred": isPreferred, "current": isCurrent});
            }
        }
        return { modes: modes, current: {"width": curW || 1280, "height": curH || 800, "refresh": curR || 60.0} };
    }

    function defaultDisplayModes() {
        return [
            {"width":1920,"height":1080,"refresh":60.0,"preferred":true,"current":false},
            {"width":1680,"height":1050,"refresh":60.0,"preferred":false,"current":false},
            {"width":1600,"height":900,"refresh":60.0,"preferred":false,"current":false},
            {"width":1440,"height":900,"refresh":60.0,"preferred":false,"current":false},
            {"width":1366,"height":768,"refresh":60.0,"preferred":false,"current":false},
            {"width":1280,"height":1024,"refresh":60.0,"preferred":false,"current":false},
            {"width":1280,"height":800,"refresh":60.0,"preferred":false,"current":true},
            {"width":1280,"height":720,"refresh":60.0,"preferred":false,"current":false},
            {"width":1024,"height":768,"refresh":60.0,"preferred":false,"current":false}
        ];
    }

    function loadDisplayInfo() {
        if (typeof sysManager.getDisplayInfoAsync === "function") {
            // ASYNC: non-blocking display info fetch — result comes via onDisplayInfoReady
            sysManager.getDisplayInfoAsync();
        } else {
            // Sync fallback for current binary
            try {
                var xrandrText = sysManager.getDisplayInfo();
                if (xrandrText && xrandrText.length > 10) {
                    var parsed = parseXrandrModes(xrandrText);
                    if (parsed.modes.length === 0) {
                        displayModes = defaultDisplayModes();
                        currentRes = {"width": 1280, "height": 800, "refresh": 60.0};
                    } else {
                        displayModes = parsed.modes;
                        currentRes = parsed.current;
                        if (currentRes.width === 0) currentRes = {"width": 1280, "height": 800, "refresh": 60.0};
                    }
                } else {
                    throw new Error("no xrandr");
                }
            } catch(e) {
                displayModes = defaultDisplayModes();
                currentRes = {"width": 1280, "height": 800, "refresh": 60.0};
            }
            // GPU info (sync)
            try {
                var gpuRaw = JSON.parse(sysManager.runCommand("lspci 2>/dev/null | grep -i vga || echo 'VirtIO GPU'", "/"));
                var gpuLine = (gpuRaw.stdout || "").trim();
                gpuInfo = {
                    "name": gpuLine.length > 5 ? gpuLine.replace(/.*VGA.*:\s*/,"").split("[")[0].trim() : "VirtIO GPU (QEMU)",
                    "driver": "virtio_gpu / pixman",
                    "renderer": "wlroots (Cage compositor)"
                };
            } catch(e) {
                gpuInfo = {"name": "VirtIO GPU (QEMU)", "driver": "virtio_gpu", "renderer": "wlroots/pixman"};
            }
            gfxInfo = {"modules": "virtio_gpu, drm, kms", "compositor": "Cage (wlroots)"};
            selectedRes = currentRes.width + "x" + currentRes.height;
        }
    }

    function applyResolution(res) {
        // HONESTO: en el compositor Qt/Wayland (eglfs) la resolución la fija el
        // WaylandOutput al arrancar — no hay xrandr/XWAYLAND. Cambiarla en caliente
        // no está soportado todavía (requiere reconfigurar el output del compositor).
        // No fingimos que funciona (regla: nunca maquillar estado).
        root.showToast("La resolución la fija la pantalla del SO; cambiarla en caliente aún no está disponible.", "info");
    }

    function confirmResolution() {
        resApplied = false;
        revertTimer.stop();
        var parts = selectedRes.split("x");
        currentRes = {"width": parseInt(parts[0]), "height": parseInt(parts[1]), "refresh": 60.0};
        root.showToast("Resolution confirmed: " + selectedRes, "success");
    }
    function revertResolution() {
        resApplied = false;
        revertTimer.stop();
        var oldRes = currentRes.width + "x" + currentRes.height;
        selectedRes = oldRes;
        // Apply the old resolution directly (not through applyResolution to avoid countdown)
        var parts2 = oldRes.split("x");
        sysManager.setDisplayResolution(parseInt(parts2[0]), parseInt(parts2[1]));
        root.showToast("Resolution reverted to " + oldRes, "info");
    }

    Timer {
        id: revertTimer; interval: 1000; repeat: true
        onTriggered: {
            revertCountdown--;
            if (revertCountdown <= 0) { revertResolution(); }
        }
    }

    function loadWorkspaceDir() {
        // No hay verbo D-Bus de os-config en esta versión. El directorio por
        // defecto se muestra desde la propiedad local (no persiste entre reinicios).
    }

    function saveWorkspaceDir(path) {
        if (!path || path.trim() === "") {
            root.showToast("La ruta del workspace no puede estar vacía", "error");
            return;
        }
        var trimmed = path.trim();
        // Crea el directorio vía sysManager (operación SO real).
        sysManager.createDir(trimmed);
        workspaceDir = trimmed;
        root.showToast("Workspace: " + trimmed + " (se aplica en esta sesión)", "success");
    }

    // ── Permisos (FR-013): panel de políticas de capacidades del agente ──
    // El broker LEE estos consents antes de cada acción. Concedido = el agente
    // actúa solo (p.ej. leer documentos); sin consent = la acción se bloquea
    // fail-closed y el agente lo explica en el chat.
    property var capCatalog: [
        { id: "documents",      label: "Documentos",            desc: "Leer y trabajar con tus documentos",                                              alwaysAsk: false },
        { id: "downloads",      label: "Descargas",             desc: "Acceder a la carpeta de descargas",                                               alwaysAsk: false },
        { id: "desktop_files",  label: "Escritorio",            desc: "Ficheros del escritorio",                                                         alwaysAsk: false },
        { id: "browser",        label: "Navegador",             desc: "Navegar por la web (confinado)",                                                  alwaysAsk: false },
        { id: "screen",         label: "Captura de pantalla",   desc: "Ver la pantalla para ayudarte",                                                   alwaysAsk: false },
        { id: "network_local",  label: "Red local",             desc: "Hablar con dispositivos de tu red",                                               alwaysAsk: false },
        { id: "system_info",    label: "Información del sistema", desc: "Leer estado del sistema (solo lectura)",                                        alwaysAsk: false },
        { id: "system_services",label: "Servicios del sistema", desc: "Observar/operar servicios",                                                       alwaysAsk: false },
        { id: "audio_devices",  label: "Audio",                 desc: "Detectar micrófonos y altavoces",                                                 alwaysAsk: false },
        { id: "udev_devices",   label: "Dispositivos",          desc: "Enumerar dispositivos conectados",                                                alwaysAsk: false },
        { id: "scheduler",      label: "Tareas programadas",    desc: "Crear tareas recurrentes",                                                        alwaysAsk: false },
        { id: "terminal",       label: "Terminal",              desc: "Ejecutar comandos de consola",                                                    alwaysAsk: true  },
        { id: "filesystem_full",label: "Disco completo",        desc: "Acceso amplio al sistema de ficheros",                                            alwaysAsk: true  },
        { id: "package_manager",label: "Instalar software",     desc: "Instalar/quitar aplicaciones",                                                   alwaysAsk: true  },
        { id: "system_settings",label: "Ajustes del SO",        desc: "Cambiar configuración del sistema operativo",                                     alwaysAsk: true  },
        { id: "input_control",  label: "Control de pantalla",   desc: "Mover ratón / teclear / operar apps abiertas — se aprueba por sesión",            alwaysAsk: true  },
        { id: "camera",         label: "Cámara",                desc: "Usar la cámara",                                                                  alwaysAsk: true  },
        { id: "microphone",     label: "Micrófono",             desc: "Escucharte (dictado/enseñanza)",                                                  alwaysAsk: true  }
    ]
    property var activeConsents: ({})
    function loadConsents() { hermes.call("perm-list", "list_consents", "{}"); }

    // ── Acceso remoto (espejo noVNC + URL pública individual) ──
    property bool remoteActive: false
    property string remoteUrl: ""
    property bool remoteAskPassword: false
    property string remoteNote: ""
    function loadRemoteStatus() { hermes.call("remote-status", "get_remote_access_status", "{}"); }
    function toggleRemoteAccess(pw) {
        if (!pw || pw.trim().length === 0) { remoteNote = "Escribe la contraseña del dispositivo."; return; }
        remoteNote = remoteActive ? "Desactivando…" : "Activando… (la pantalla local se reiniciará en modo espejo)";
        // El staging lo hace el COMPOSITOR (hermes-user), NO el daemon (que está
        // bloqueado de /run/hermes/remote-control por seguridad). El root helper
        // PAM-verifica la contraseña del dispositivo antes de activar.
        var ok = remoteActive ? sysManager.disableRemoteAccess(pw) : sysManager.enableRemoteAccess(pw);
        if (ok) {
            remoteNote = "Petición aceptada — verificando contraseña…";
            remoteAskPassword = false;
        } else {
            remoteNote = "✕ no se pudo registrar la petición";
        }
    }
    // Mientras el panel esté activo, sondea estado/URL (el túnel tarda unos s).
    Timer {
        id: remotePollTimer
        interval: 3000; repeat: true
        running: activeTab === "remote"
        onTriggered: loadRemoteStatus()
    }
    function setConsent(capId, enable) {
        if (enable) hermes.call("perm-grant-" + capId, "grant_consent", JSON.stringify({ capability: capId, scope: "persistent" }));
        else hermes.call("perm-revoke-" + capId, "revoke_consent", JSON.stringify({ capability: capId }));
    }
    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "remote-status") {
                try {
                    var rs = JSON.parse(jsonStr || "{}");
                    remoteActive = rs.active === true;
                    remoteUrl = rs.url || "";
                } catch (e) {}
                return;
            }
            if (reqId === "remote-toggle") {
                var rt = {}; try { rt = JSON.parse(jsonStr || "{}"); } catch (e) {}
                if (ok && rt.ok) {
                    remoteNote = "Petición aceptada — verificando contraseña…";
                    remoteAskPassword = false;
                    // El root helper tarda ~2s en PAM + systemctl; el poll refresca.
                } else {
                    remoteNote = "✕ " + (rt.error || jsonStr || "no se pudo");
                }
                loadRemoteStatus();
                return;
            }
            if (reqId === "perm-list") {
                var m = ({});
                try {
                    var arr = JSON.parse(jsonStr || "[]");
                    for (var i = 0; i < arr.length; i++) {
                        var c = arr[i].capability || arr[i].cap || "";
                        if (c) m[c] = true;
                    }
                } catch (e) {}
                activeConsents = m;
            } else if (reqId.indexOf("perm-grant-") === 0 || reqId.indexOf("perm-revoke-") === 0) {
                loadConsents();
                if (!ok) root.showToast("No se pudo cambiar el permiso", "error");
            } else if (reqId === "auto-get") {
                autoModeLoading = false;
                try {
                    var am = JSON.parse(jsonStr || "{}");
                    autoModeOn = am.auto_mode === true;
                } catch (e) { autoModeOn = false; }
            } else if (reqId === "auto-set-on" || reqId === "auto-set-off") {
                autoModeLoading = false;
                try {
                    var amr = JSON.parse(jsonStr || "{}");
                    if (amr.ok === true) {
                        autoModeOn = amr.auto_mode === true;
                        root.showToast(
                            autoModeOn ? "Modo AUTO activado" : "Modo Guardado activado",
                            autoModeOn ? "info" : "success"
                        );
                    } else {
                        root.showToast("No se pudo cambiar el modo de autonomía", "error");
                        loadAutoMode();
                    }
                } catch (e) {
                    root.showToast("No se pudo cambiar el modo de autonomía", "error");
                    loadAutoMode();
                }
            }
        }
    }

    // ── Layout: sidebar + scrollable content ──────────────────────────────
    // Compact mode (< bpCompact): top tab strip replaces sidebar.
    // _navItems defined on settingsApp above.

    // ── Compact: horizontal scrollable tab strip ──────────────────────
    Rectangle {
        id: compactTabBar
        visible: settingsApp._compact
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: Math.round(44 * root.sf)
        color: Qt.rgba(Tokens.bgVoid.r, Tokens.bgVoid.g, Tokens.bgVoid.b, 0.55)

        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: Tokens.borderSubtle
        }

        Flickable {
            anchors.fill: parent
            contentWidth: ctsRow.implicitWidth + Math.round(Tokens.spMd * root.sf)
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            Row {
                id: ctsRow
                anchors.verticalCenter: parent.verticalCenter
                x: Math.round(Tokens.spSm * root.sf)
                spacing: Math.round(Tokens.spXs * root.sf)

                Repeater {
                    model: settingsApp._navItems
                    delegate: Item {
                        width: ctsLabel.implicitWidth + Math.round(Tokens.spLg * root.sf)
                        height: Math.round(32 * root.sf)

                        readonly property bool _active: activeTab === modelData.id
                        readonly property bool _hov: ctsMa.containsMouse && !_active

                        Rectangle {
                            anchors.fill: parent
                            radius: Math.round(Tokens.radiusSm * root.sf)
                            color: _active ? Tokens.accentSubtle : _hov ? Tokens.bgElevated : "transparent"
                            border.width: _active ? 1 : 0
                            border.color: Tokens.accentBase
                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }
                        }

                        Text {
                            id: ctsLabel
                            anchors.centerIn: parent
                            text: modelData.label
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * root.sf)
                            font.weight: _active ? Font.Medium : Font.Normal
                            color: _active ? Tokens.accentBase : Tokens.textSecondary
                        }

                        MouseArea {
                            id: ctsMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: activeTab = modelData.id
                        }
                    }
                }
            }
        }
    }

    RowLayout {
        anchors.top: settingsApp._compact ? compactTabBar.bottom : parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        spacing: 0

        // ── Sidebar nav (normal width only) ──────────────────────────────
        Rectangle {
            visible: !settingsApp._compact
            Layout.fillHeight: true
            Layout.preferredWidth: visible ? Math.round(172 * root.sf) : 0
            color: Qt.rgba(Tokens.bgVoid.r, Tokens.bgVoid.g, Tokens.bgVoid.b, 0.55)

            // Right separator
            Rectangle {
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 1
                color: Tokens.borderSubtle
            }

            Column {
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spSm * root.sf)
                anchors.topMargin: Math.round(Tokens.spMd * root.sf)
                spacing: Math.round(2 * root.sf)

                Repeater {
                    model: [
                        { id: "profile",  icon: "", label: "Perfil" },
                        { id: "permisos", icon: "", label: "Permisos" },
                        { id: "remote",   icon: "", label: "Acceso remoto" },
                        { id: "users",    icon: "", label: "Usuarios" },
                        { id: "storage",  icon: "", label: "Almacenamiento" },
                        { id: "display",  icon: "", label: "Pantalla" }
                    ]
                    delegate: Item {
                        width: parent ? parent.width : Math.round(156 * root.sf)
                        height: Math.round(36 * root.sf)

                        readonly property bool _active: activeTab === modelData.id
                        readonly property bool _hovered: navMa.containsMouse && !_active

                        // Selection background
                        Rectangle {
                            anchors.fill: parent
                            radius: root.radiusSm
                            color: _active  ? Tokens.accentSubtle
                                 : _hovered ? Tokens.bgElevated
                                 : "transparent"

                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }
                        }

                        // Amber accent bar (active only)
                        Rectangle {
                            visible: _active
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            width: Math.round(3 * root.sf)
                            height: Math.round(18 * root.sf)
                            radius: Math.round(2 * root.sf)
                            color: Tokens.accentBase
                        }

                        Row {
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.left: parent.left
                            anchors.leftMargin: Math.round(Tokens.spMd * root.sf)
                            spacing: Math.round(Tokens.spSm * root.sf)

                            Text {
                                text: modelData.icon
                                font.family: root.iconFont
                                font.weight: Font.Black
                                font.pixelSize: Math.round(13 * root.sf)
                                color: _active ? Tokens.accentBase : Tokens.textMuted
                            }
                            Text {
                                text: modelData.label
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(13 * root.sf)
                                font.weight: _active ? Font.Medium : Font.Normal
                                color: _active ? Tokens.textPrimary : Tokens.textSecondary
                            }
                        }

                        MouseArea {
                            id: navMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: activeTab = modelData.id
                        }
                    }
                }
            }
        }

        // ── Scrollable content area ───────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            Flickable {
                id: settingsFlick
                anchors.fill: parent
                contentHeight: cCol.implicitHeight + Math.round(Tokens.spXl * root.sf)
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: LumenScrollBar { sf: root.sf }

                WheelHandler {
                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                    onWheel: (event) => {
                        var f = settingsFlick;
                        f.contentY = Math.max(0, Math.min(Math.max(0, f.contentHeight - f.height), f.contentY - event.angleDelta.y));
                    }
                }

                Column {
                    id: cCol
                    width: settingsFlick.width - Math.round(Tokens.spXl * root.sf)
                    x: Math.round(Tokens.spLg * root.sf)
                    y: Math.round(Tokens.spLg * root.sf)
                    spacing: Math.round(Tokens.spMd * root.sf)

                    // ── PROFILE ─────────────────────────────────────────────
                    Column {
                        width: parent.width
                        spacing: Math.round(Tokens.spMd * root.sf)
                        visible: activeTab === "profile"

                        // Section heading
                        Text {
                            text: "Perfil"
                            font.family: Tokens.fontDisplay
                            font.pixelSize: Math.round(20 * root.sf)
                            font.weight: Font.Medium
                            color: Tokens.textPrimary
                        }

                        // Avatar + user info card
                        Rectangle {
                            width: parent.width
                            height: profileInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.width: 1
                            border.color: Tokens.borderSubtle

                            Column {
                                id: profileInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spLg * root.sf)
                                }
                                spacing: Math.round(Tokens.spMd * root.sf)

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spMd * root.sf)

                                    // Avatar circle
                                    Rectangle {
                                        width: Math.round(48 * root.sf)
                                        height: Math.round(48 * root.sf)
                                        radius: width / 2
                                        color: Tokens.accentBase
                                        Text {
                                            anchors.centerIn: parent
                                            text: root.currentUser.charAt(0).toUpperCase()
                                            font.family: Tokens.fontDisplay
                                            font.pixelSize: Math.round(20 * root.sf)
                                            font.weight: Font.Bold
                                            color: Tokens.textOnAccent
                                        }
                                    }

                                    Column {
                                        Layout.fillWidth: true
                                        spacing: Math.round(Tokens.spXs * root.sf)
                                        Text {
                                            text: root.currentUser
                                            font.family: Tokens.fontDisplay
                                            font.pixelSize: Math.round(16 * root.sf)
                                            font.weight: Font.Medium
                                            color: Tokens.textPrimary
                                        }
                                        Text {
                                            text: "Administrator"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * root.sf)
                                            color: Tokens.textMuted
                                        }
                                    }
                                }

                                Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

                                Text {
                                    text: "Cambiar contraseña"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(12 * root.sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textMuted
                                }

                                LumenInput {
                                    id: currentPwField
                                    width: parent.width
                                    sf: root.sf
                                    placeholder: "Contraseña actual…"
                                    password: true
                                }

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)

                                    LumenInput {
                                        id: pwField
                                        Layout.fillWidth: true
                                        sf: root.sf
                                        placeholder: "Nueva contraseña…"
                                        password: true
                                    }

                                    LumenButton {
                                        sf: root.sf
                                        label: "Actualizar"
                                        variant: "primary"
                                        implicitWidth: Math.round(90 * root.sf)
                                        implicitHeight: Math.round(38 * root.sf)
                                        onClicked: changePassword(currentPwField.text, pwField.text)
                                    }
                                }
                            }
                        }
                    }

                    // ── PERMISOS ─────────────────────────────────────────────
                    Column {
                        width: parent.width
                        spacing: Math.round(Tokens.spMd * root.sf)
                        visible: activeTab === "permisos"

                        Text {
                            text: "Permisos del agente"
                            font.family: Tokens.fontDisplay
                            font.pixelSize: Math.round(20 * root.sf)
                            font.weight: Font.Medium
                            color: Tokens.textPrimary
                        }

                        Text {
                            width: parent.width
                            text: "Activado: el agente puede usar esa capacidad. Las marcadas «siempre pide confirmación» te mostrarán una tarjeta de aprobación cada vez antes de actuar. Desactivado: la capacidad se bloquea y Hermes te lo explicará en el chat."
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(12 * root.sf)
                            color: Tokens.textMuted
                            wrapMode: Text.WordWrap
                        }

                        // ── Modo AUTO / Modo Guardado ─────────────────────────
                        Rectangle {
                            width: parent.width
                            height: autoModeInner.implicitHeight + Math.round(Tokens.spLg * root.sf)
                            radius: root.radiusMd
                            color: autoModeOn ? Tokens.warnSubtle : Tokens.bgCard
                            border.width: autoModeOn ? 2 : 1
                            border.color: autoModeOn ? Tokens.warnBase : Tokens.borderSubtle

                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durBase; easing.type: Easing.InOutCubic }
                            }
                            Behavior on border.color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durBase; easing.type: Easing.InOutCubic }
                            }

                            Column {
                                id: autoModeInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spLg * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                // Header row: label + LumenSwitch
                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spMd * root.sf)

                                    Column {
                                        Layout.fillWidth: true
                                        spacing: Math.round(Tokens.spXs * root.sf)

                                        Text {
                                            text: autoModeOn ? "Modo AUTO — autonomía completa" : "Modo Guardado — pide permiso"
                                            font.family: Tokens.fontDisplay
                                            font.pixelSize: Math.round(14 * root.sf)
                                            font.weight: Font.Medium
                                            color: autoModeOn ? Tokens.warnBase : Tokens.textPrimary

                                            Behavior on color {
                                                enabled: !Tokens.reduceMotion
                                                ColorAnimation { duration: Tokens.durBase; easing.type: Easing.InOutCubic }
                                            }
                                        }

                                        Text {
                                            width: parent.width
                                            text: autoModeOn
                                                  ? "El asistente actúa sin pedirte permiso. Ideal para tareas desatendidas largas."
                                                  : "El asistente te pregunta antes de cada acción delicada. Recomendado."
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * root.sf)
                                            color: Tokens.textMuted
                                            wrapMode: Text.WordWrap
                                        }

                                        // "AUTO ACTIVO" badge
                                        Rectangle {
                                            visible: autoModeOn
                                            width: autoBadgeText.implicitWidth + Math.round(Tokens.spMd * root.sf)
                                            height: Math.round(18 * root.sf)
                                            radius: Math.round(Tokens.radiusSm * root.sf)
                                            color: Tokens.warnSubtle
                                            border.width: 1
                                            border.color: Tokens.warnBase

                                            Text {
                                                id: autoBadgeText
                                                anchors.centerIn: parent
                                                text: "AUTO ACTIVO"
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(9 * root.sf)
                                                font.weight: Font.Bold
                                                color: Tokens.warnBase
                                            }
                                        }
                                    }

                                    // LumenSwitch wired to auto mode.
                                    // onToggled fires AFTER the internal checked flip, so we
                                    // re-sync to autoModeOn after the decision (revert or confirm).
                                    LumenSwitch {
                                        id: autoModeSwitch
                                        sf: root.sf
                                        checked: autoModeOn
                                        opacity: autoModeLoading ? 0.45 : 1.0
                                        onToggled: function(v) {
                                            if (v) {
                                                // Activar: gate behind warning dialog.
                                                // Revert the visual flip until confirmed.
                                                autoModeSwitch.checked = autoModeOn;
                                                showAutoWarning = true;
                                            } else {
                                                disableAutoMode();
                                            }
                                        }
                                        // Re-establish checked whenever autoModeOn changes
                                        // (covers both the revert above and the server response).
                                        Connections {
                                            target: settingsApp
                                            function onAutoModeOnChanged() {
                                                autoModeSwitch.checked = settingsApp.autoModeOn;
                                            }
                                        }

                                        Behavior on opacity {
                                            enabled: !Tokens.reduceMotion
                                            NumberAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                        }
                                    }
                                }
                            }
                        }

                        // ── Diálogo de advertencia AUTO ───────────────────────
                        Rectangle {
                            visible: showAutoWarning
                            width: parent.width
                            height: autoWarnInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.dangerSubtle
                            border.width: 1
                            border.color: Tokens.dangerBase

                            Behavior on opacity {
                                enabled: !Tokens.reduceMotion
                                NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutCubic }
                            }

                            Column {
                                id: autoWarnInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spLg * root.sf)
                                }
                                spacing: Math.round(Tokens.spMd * root.sf)

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)

                                    Text {
                                        text: "⚠"
                                        font.pixelSize: Math.round(18 * root.sf)
                                        color: Tokens.dangerBase
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: "¿Activar modo AUTO?"
                                        font.family: Tokens.fontDisplay
                                        font.pixelSize: Math.round(14 * root.sf)
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                    }
                                }

                                Text {
                                    width: parent.width
                                    text: "El asistente ejecutará TODO sin pedirte permiso, incluidas acciones delicadas como borrar ficheros, instalar software o enviar mensajes. Actívalo solo si confías plenamente en lo que hace en este momento."
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(12 * root.sf)
                                    color: Tokens.textSecondary
                                    wrapMode: Text.WordWrap
                                }

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)

                                    Item { Layout.fillWidth: true }

                                    LumenButton {
                                        sf: root.sf
                                        label: "Cancelar"
                                        variant: "ghost"
                                        implicitWidth: Math.round(88 * root.sf)
                                        implicitHeight: Math.round(34 * root.sf)
                                        onClicked: showAutoWarning = false
                                    }

                                    LumenButton {
                                        sf: root.sf
                                        label: "Sí, activar AUTO"
                                        variant: "danger"
                                        implicitWidth: Math.round(144 * root.sf)
                                        implicitHeight: Math.round(34 * root.sf)
                                        onClicked: confirmEnableAutoMode()
                                    }
                                }
                            }
                        }

                        // ── Capacidades individuales ───────────────────────────
                        Repeater {
                            model: capCatalog

                            Rectangle {
                                width: cCol.width
                                height: capInner.implicitHeight + Math.round(Tokens.spMd * root.sf)
                                radius: root.radiusMd
                                color: Tokens.bgCard
                                border.width: 1
                                border.color: Tokens.borderSubtle

                                readonly property bool _granted: activeConsents[modelData.id] === true

                                RowLayout {
                                    id: capInner
                                    anchors {
                                        left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter
                                        leftMargin: Math.round(Tokens.spMd * root.sf)
                                        rightMargin: Math.round(Tokens.spMd * root.sf)
                                    }
                                    spacing: Math.round(Tokens.spMd * root.sf)

                                    Column {
                                        Layout.fillWidth: true
                                        spacing: Math.round(Tokens.spXs * root.sf)

                                        Text {
                                            text: modelData.label
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(13 * root.sf)
                                            font.weight: Font.Medium
                                            color: Tokens.textPrimary
                                        }
                                        Text {
                                            width: parent.width
                                            text: modelData.desc
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * root.sf)
                                            color: Tokens.textMuted
                                            wrapMode: Text.WordWrap
                                            elide: Text.ElideRight
                                            maximumLineCount: 2
                                        }
                                        // "siempre pide confirmación" badge (alwaysAsk)
                                        Rectangle {
                                            visible: modelData.alwaysAsk === true
                                            width: askBadgeText.implicitWidth + Math.round(Tokens.spMd * root.sf)
                                            height: Math.round(17 * root.sf)
                                            radius: Math.round(Tokens.radiusSm * root.sf)
                                            color: Tokens.warnSubtle
                                            border.color: Tokens.warnBase
                                            border.width: 1

                                            Text {
                                                id: askBadgeText
                                                anchors.centerIn: parent
                                                text: "siempre pide confirmación"
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(9 * root.sf)
                                                font.weight: Font.Medium
                                                color: Tokens.warnBase
                                            }
                                        }
                                    }

                                    LumenSwitch {
                                        sf: root.sf
                                        checked: _granted
                                        onToggled: function(v) { setConsent(modelData.id, v) }
                                    }
                                }
                            }
                        }
                    }

                    // ── ACCESO REMOTO ────────────────────────────────────────
                    Column {
                        width: parent.width
                        spacing: Math.round(Tokens.spMd * root.sf)
                        visible: activeTab === "remote"

                        Text {
                            text: "Acceso remoto"
                            font.family: Tokens.fontDisplay
                            font.pixelSize: Math.round(20 * root.sf)
                            font.weight: Font.Medium
                            color: Tokens.textPrimary
                        }

                        Text {
                            width: parent.width
                            text: "Genera un enlace noVNC único para esta instalación: abre el escritorio desde cualquier navegador, con teclado, ratón y portapapeles. Mientras está activo, la pantalla local se reinicia en modo espejo."
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(12 * root.sf)
                            color: Tokens.textMuted
                            wrapMode: Text.WordWrap
                        }

                        // Estado + toggle
                        Rectangle {
                            width: parent.width
                            height: remoteRowInner.implicitHeight + Math.round(Tokens.spMd * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.width: 1
                            border.color: remoteActive ? Tokens.successBase : Tokens.borderSubtle

                            Behavior on border.color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durBase; easing.type: Easing.InOutCubic }
                            }

                            RowLayout {
                                id: remoteRowInner
                                anchors {
                                    left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter
                                    leftMargin: Math.round(Tokens.spMd * root.sf)
                                    rightMargin: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spMd * root.sf)

                                Column {
                                    Layout.fillWidth: true
                                    spacing: Math.round(Tokens.spXs * root.sf)
                                    Text {
                                        text: remoteActive ? "Activado" : "Desactivado"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(13 * root.sf)
                                        font.weight: Font.Medium
                                        color: remoteActive ? Tokens.successBase : Tokens.textPrimary
                                    }
                                    Text {
                                        text: remoteActive
                                              ? "El escritorio es accesible por el enlace de abajo."
                                              : "Actívalo con la contraseña del dispositivo."
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(11 * root.sf)
                                        color: Tokens.textMuted
                                    }
                                }

                                LumenSwitch {
                                    sf: root.sf
                                    checked: remoteActive
                                    onToggled: function(v) { remoteAskPassword = true }
                                }
                            }
                        }

                        // Diálogo de contraseña (consentimiento PAM en el root helper)
                        Rectangle {
                            visible: remoteAskPassword
                            width: parent.width
                            height: rapInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.warnSubtle
                            border.color: Tokens.warnBase
                            border.width: 1

                            Column {
                                id: rapInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                Text {
                                    text: (remoteActive ? "Desactivar" : "Activar") + " acceso remoto — contraseña del dispositivo"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(12 * root.sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textPrimary
                                }

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)

                                    LumenInput {
                                        id: remotePwField
                                        Layout.fillWidth: true
                                        sf: root.sf
                                        placeholder: "Contraseña…"
                                        password: true
                                    }

                                    LumenButton {
                                        sf: root.sf
                                        label: remoteActive ? "Desactivar" : "Activar"
                                        variant: "primary"
                                        implicitWidth: Math.round(96 * root.sf)
                                        implicitHeight: Math.round(38 * root.sf)
                                        onClicked: toggleRemoteAccess(remotePwField.text)
                                    }

                                    LumenButton {
                                        sf: root.sf
                                        label: "Cancelar"
                                        variant: "ghost"
                                        implicitWidth: Math.round(80 * root.sf)
                                        implicitHeight: Math.round(38 * root.sf)
                                        onClicked: { remoteAskPassword = false; remotePwField.text = ""; }
                                    }
                                }

                                Text {
                                    visible: remoteNote.length > 0
                                    width: parent.width
                                    text: remoteNote
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf)
                                    color: Tokens.textMuted
                                    wrapMode: Text.WordWrap
                                }
                            }
                        }

                        // URL del enlace (cuando el túnel ya la publicó)
                        Rectangle {
                            visible: remoteActive && remoteUrl.length > 0
                            width: parent.width
                            height: rURLInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.width: 1
                            border.color: Tokens.successBase

                            Column {
                                id: rURLInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                Text {
                                    text: "Tu enlace noVNC (único de esta instalación):"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf)
                                    color: Tokens.textMuted
                                }
                                Text {
                                    width: parent.width
                                    text: remoteUrl
                                    font.family: Tokens.fontMono
                                    font.pixelSize: Math.round(12 * root.sf)
                                    color: Tokens.successBase
                                    wrapMode: Text.WrapAnywhere
                                }

                                LumenButton {
                                    sf: root.sf
                                    label: "Copiar enlace"
                                    variant: "secondary"
                                    implicitWidth: Math.round(130 * root.sf)
                                    implicitHeight: Math.round(32 * root.sf)
                                    onClicked: { sysManager.copyToClipboard(remoteUrl); root.showToast("Enlace copiado", "success"); }
                                }
                            }
                        }

                        Text {
                            visible: remoteActive && remoteUrl.length === 0
                            text: "Generando el enlace público… (unos segundos)"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * root.sf)
                            color: Tokens.textMuted
                        }
                    }

                    // ── USERS ────────────────────────────────────────────────
                    Column {
                        width: parent.width
                        spacing: Math.round(Tokens.spMd * root.sf)
                        visible: activeTab === "users"

                        RowLayout {
                            width: parent.width

                            Text {
                                Layout.fillWidth: true
                                text: "Usuarios"
                                font.family: Tokens.fontDisplay
                                font.pixelSize: Math.round(20 * root.sf)
                                font.weight: Font.Medium
                                color: Tokens.textPrimary
                            }

                            LumenButton {
                                sf: root.sf
                                label: "+ Añadir usuario"
                                variant: "primary"
                                implicitWidth: Math.round(130 * root.sf)
                                implicitHeight: Math.round(32 * root.sf)
                                onClicked: showAddUser = true
                            }
                        }

                        // Add user form
                        Rectangle {
                            visible: showAddUser
                            width: parent.width
                            height: addUserInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.successSubtle
                            border.color: Tokens.successBase
                            border.width: 1

                            Column {
                                id: addUserInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                Text {
                                    text: "Nuevo usuario"
                                    font.family: Tokens.fontDisplay
                                    font.pixelSize: Math.round(14 * root.sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textPrimary
                                }

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)

                                    Column {
                                        Layout.fillWidth: true
                                        spacing: Math.round(Tokens.spXs * root.sf)
                                        Text {
                                            text: "Usuario"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * root.sf)
                                            color: Tokens.textMuted
                                        }
                                        LumenInput {
                                            width: parent.width
                                            sf: root.sf
                                            placeholder: "nombre_usuario"
                                            onAccepted: newUsername = text
                                            Component.onCompleted: {
                                                // Bind text changes since LumenInput exposes alias
                                            }
                                        }
                                    }

                                    Column {
                                        Layout.fillWidth: true
                                        spacing: Math.round(Tokens.spXs * root.sf)
                                        Text {
                                            text: "Contraseña"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * root.sf)
                                            color: Tokens.textMuted
                                        }
                                        LumenInput {
                                            width: parent.width
                                            sf: root.sf
                                            placeholder: "Contraseña"
                                            password: true
                                            onAccepted: newPassword = text
                                        }
                                    }
                                }

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)
                                    Item { Layout.fillWidth: true }

                                    LumenButton {
                                        sf: root.sf
                                        label: "Cancelar"
                                        variant: "ghost"
                                        implicitWidth: Math.round(72 * root.sf)
                                        implicitHeight: Math.round(32 * root.sf)
                                        onClicked: showAddUser = false
                                    }
                                    LumenButton {
                                        sf: root.sf
                                        label: "Crear"
                                        variant: "primary"
                                        implicitWidth: Math.round(72 * root.sf)
                                        implicitHeight: Math.round(32 * root.sf)
                                        onClicked: addUser()
                                    }
                                }
                            }
                        }

                        // User list
                        Repeater {
                            model: userList

                            Rectangle {
                                width: cCol.width
                                height: Math.round(52 * root.sf)
                                radius: root.radiusMd
                                color: Tokens.bgCard
                                border.color: Tokens.borderSubtle
                                border.width: 1

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: Math.round(Tokens.spMd * root.sf)
                                    spacing: Math.round(Tokens.spMd * root.sf)

                                    // Avatar
                                    Rectangle {
                                        width: Math.round(32 * root.sf)
                                        height: Math.round(32 * root.sf)
                                        radius: width / 2
                                        color: Tokens.accentBase
                                        Text {
                                            anchors.centerIn: parent
                                            text: (modelData.username || "?").charAt(0).toUpperCase()
                                            color: Tokens.textOnAccent
                                            font.family: Tokens.fontDisplay
                                            font.pixelSize: Math.round(14 * root.sf)
                                            font.weight: Font.Medium
                                        }
                                    }

                                    Column {
                                        Layout.fillWidth: true
                                        spacing: Math.round(Tokens.spXs * root.sf)
                                        Text {
                                            text: modelData.username || ""
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(13 * root.sf)
                                            font.weight: Font.Medium
                                            color: Tokens.textPrimary
                                        }
                                        Text {
                                            text: modelData.role || "user"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(10 * root.sf)
                                            color: Tokens.textMuted
                                        }
                                    }

                                    // Active badge
                                    Rectangle {
                                        width: activeUserLabel.implicitWidth + Math.round(Tokens.spMd * root.sf)
                                        height: Math.round(20 * root.sf)
                                        radius: Math.round(Tokens.radiusSm * root.sf)
                                        color: Tokens.successSubtle

                                        Text {
                                            id: activeUserLabel
                                            anchors.centerIn: parent
                                            text: "Activo"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(10 * root.sf)
                                            color: Tokens.successBase
                                        }
                                    }

                                    // Delete button (hidden for own account)
                                    LumenButton {
                                        visible: (modelData.username || "") !== root.currentUser
                                        sf: root.sf
                                        label: "Eliminar"
                                        variant: "danger"
                                        implicitWidth: Math.round(72 * root.sf)
                                        implicitHeight: Math.round(28 * root.sf)
                                        onClicked: deleteUser(modelData.username)
                                    }
                                }
                            }
                        }
                    }

                    // ── CHANNELS (Próximamente) ─────────────────────────────
                    Column {
                        width: parent.width
                        spacing: Math.round(Tokens.spMd * root.sf)
                        visible: activeTab === "channels"

                        Text {
                            text: "Canales de mensajería"
                            font.family: Tokens.fontDisplay
                            font.pixelSize: Math.round(20 * root.sf)
                            font.weight: Font.Medium
                            color: Tokens.textPrimary
                        }

                        // WhatsApp
                        Rectangle {
                            width: parent.width
                            height: waInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: waConnected ? Tokens.successBase : Tokens.borderSubtle
                            border.width: waConnected ? 2 : 1

                            Column {
                                id: waInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spMd * root.sf)

                                    Rectangle {
                                        width: Math.round(36 * root.sf)
                                        height: Math.round(36 * root.sf)
                                        radius: root.radiusSm
                                        color: Qt.rgba(0.14, 0.69, 0.40, waConnected ? 0.20 : 0.12)
                                        Text {
                                            anchors.centerIn: parent
                                            text: ""
                                            font.family: root.iconFont
                                            font.weight: Font.Black
                                            font.pixelSize: Math.round(16 * root.sf)
                                            color: "#25D366"
                                        }
                                    }

                                    Column {
                                        Layout.fillWidth: true
                                        spacing: Math.round(Tokens.spXs * root.sf)
                                        Text {
                                            text: "WhatsApp"
                                            font.family: Tokens.fontDisplay
                                            font.pixelSize: Math.round(14 * root.sf)
                                            font.weight: Font.Medium
                                            color: Tokens.textPrimary
                                        }
                                        Row {
                                            spacing: Math.round(Tokens.spSm * root.sf)
                                            Rectangle {
                                                visible: waConnected
                                                width: Math.round(7 * root.sf); height: Math.round(7 * root.sf)
                                                radius: width / 2
                                                color: Tokens.successBase
                                                anchors.verticalCenter: parent.verticalCenter
                                                SequentialAnimation on opacity {
                                                    running: waConnected; loops: Animation.Infinite
                                                    NumberAnimation { to: 0.4; duration: 1500 }
                                                    NumberAnimation { to: 1.0; duration: 1500 }
                                                }
                                            }
                                            Text {
                                                text: waConnected ? "Conectado y activo" : "Conectar mediante código QR"
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(11 * root.sf)
                                                color: waConnected ? Tokens.successBase : Tokens.textMuted
                                            }
                                        }
                                    }

                                    LumenButton {
                                        visible: !waConnected
                                        sf: root.sf
                                        label: waConnecting ? "Esperando…" : "Conectar"
                                        variant: "primary"
                                        loading: waConnecting
                                        implicitWidth: Math.round(88 * root.sf)
                                        implicitHeight: Math.round(32 * root.sf)
                                        onClicked: if (!waConnected && !waConnecting) connectWhatsApp()
                                    }
                                }

                                Rectangle {
                                    visible: waQrCode !== "" && !waConnected
                                    width: Math.round(200 * root.sf); height: Math.round(200 * root.sf)
                                    radius: root.radiusMd; color: Tokens.bgVoid
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    Image {
                                        anchors.fill: parent; anchors.margins: Math.round(Tokens.spSm * root.sf)
                                        source: waQrCode; fillMode: Image.PreserveAspectFit; smooth: true
                                    }
                                }

                                Text {
                                    visible: waConnecting && waQrCode === ""
                                    text: "Iniciando WhatsApp…"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf)
                                    color: Tokens.textMuted
                                    anchors.horizontalCenter: parent.horizontalCenter
                                }
                            }
                        }

                        // Telegram
                        Rectangle {
                            width: parent.width
                            height: tgInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: Tokens.borderSubtle
                            border.width: 1

                            Column {
                                id: tgInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spMd * root.sf)

                                    Rectangle {
                                        width: Math.round(36 * root.sf); height: Math.round(36 * root.sf)
                                        radius: root.radiusSm
                                        color: Qt.rgba(0.16, 0.55, 0.85, 0.12)
                                        Text {
                                            anchors.centerIn: parent; text: ""
                                            font.family: root.iconFont; font.weight: Font.Black
                                            font.pixelSize: Math.round(16 * root.sf); color: "#0088cc"
                                        }
                                    }
                                    Column {
                                        Layout.fillWidth: true; spacing: Math.round(Tokens.spXs * root.sf)
                                        Text { text: "Telegram"; font.family: Tokens.fontDisplay; font.pixelSize: Math.round(14 * root.sf); font.weight: Font.Medium; color: Tokens.textPrimary }
                                        Text { text: "Conectar con token de bot"; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); color: Tokens.textMuted }
                                    }
                                }

                                RowLayout {
                                    width: parent.width; spacing: Math.round(Tokens.spSm * root.sf)
                                    LumenInput {
                                        Layout.fillWidth: true; sf: root.sf
                                        placeholder: "Token del bot desde @BotFather…"
                                        onAccepted: tgToken = text
                                    }
                                    LumenButton {
                                        sf: root.sf; label: "Conectar"; variant: "primary"
                                        implicitWidth: Math.round(88 * root.sf); implicitHeight: Math.round(38 * root.sf)
                                        onClicked: connectCh("telegram", tgToken)
                                    }
                                }
                            }
                        }

                        // Discord
                        Rectangle {
                            width: parent.width
                            height: dcInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: Tokens.borderSubtle
                            border.width: 1

                            Column {
                                id: dcInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                RowLayout {
                                    width: parent.width; spacing: Math.round(Tokens.spMd * root.sf)
                                    Rectangle {
                                        width: Math.round(36 * root.sf); height: Math.round(36 * root.sf)
                                        radius: root.radiusSm
                                        color: Qt.rgba(0.35, 0.40, 0.95, 0.12)
                                        Text {
                                            anchors.centerIn: parent; text: ""
                                            font.family: root.iconFont; font.weight: Font.Black
                                            font.pixelSize: Math.round(16 * root.sf); color: "#5865F2"
                                        }
                                    }
                                    Column {
                                        Layout.fillWidth: true; spacing: Math.round(Tokens.spXs * root.sf)
                                        Text { text: "Discord"; font.family: Tokens.fontDisplay; font.pixelSize: Math.round(14 * root.sf); font.weight: Font.Medium; color: Tokens.textPrimary }
                                        Text { text: "Conectar con token de bot"; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); color: Tokens.textMuted }
                                    }
                                }

                                RowLayout {
                                    width: parent.width; spacing: Math.round(Tokens.spSm * root.sf)
                                    LumenInput {
                                        Layout.fillWidth: true; sf: root.sf
                                        placeholder: "Token del bot de Discord…"
                                        onAccepted: dcToken = text
                                    }
                                    LumenButton {
                                        sf: root.sf; label: "Conectar"; variant: "primary"
                                        implicitWidth: Math.round(88 * root.sf); implicitHeight: Math.round(38 * root.sf)
                                        onClicked: connectCh("discord", dcToken)
                                    }
                                }
                            }
                        }
                    }

                    // ── DISPLAY ──────────────────────────────────────────────
                    Column {
                        width: parent.width
                        spacing: Math.round(Tokens.spMd * root.sf)
                        visible: activeTab === "display"

                        Text {
                            text: "Pantalla"
                            font.family: Tokens.fontDisplay
                            font.pixelSize: Math.round(20 * root.sf)
                            font.weight: Font.Medium
                            color: Tokens.textPrimary
                        }

                        // Current Resolution Card
                        Rectangle {
                            width: parent.width
                            height: curResInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: Tokens.borderSubtle
                            border.width: 1

                            Column {
                                id: curResInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spMd * root.sf)

                                Text {
                                    text: "Resolución actual"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(13 * root.sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textPrimary
                                }

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spLg * root.sf)

                                    Column {
                                        spacing: Math.round(Tokens.spXs * root.sf)
                                        Text {
                                            text: currentRes.width + " × " + currentRes.height
                                            font.family: Tokens.fontDisplay
                                            font.pixelSize: Math.round(22 * root.sf)
                                            font.weight: Font.Bold
                                            color: Tokens.accentBase
                                        }
                                        Text {
                                            text: currentRes.refresh + " Hz"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * root.sf)
                                            color: Tokens.textMuted
                                        }
                                    }

                                    Item { Layout.fillWidth: true }

                                    Rectangle {
                                        width: activeResLabel.implicitWidth + Math.round(Tokens.spLg * root.sf)
                                        height: activeResLabel.implicitHeight + Math.round(Tokens.spMd * root.sf)
                                        radius: root.radiusSm
                                        color: Tokens.successSubtle
                                        border.color: Tokens.successBase
                                        border.width: 1

                                        Column {
                                            id: activeResLabel
                                            anchors.centerIn: parent
                                            spacing: Math.round(1 * root.sf)
                                            Text {
                                                text: "Activo"
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(9 * root.sf)
                                                color: Tokens.successBase
                                                anchors.horizontalCenter: parent.horizontalCenter
                                            }
                                            Text {
                                                text: currentRes.refresh + " Hz"
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(12 * root.sf)
                                                font.weight: Font.Medium
                                                color: Tokens.successBase
                                                anchors.horizontalCenter: parent.horizontalCenter
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // Resolution Picker Card
                        Rectangle {
                            width: parent.width
                            height: resPickerInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: Tokens.borderSubtle
                            border.width: 1

                            Column {
                                id: resPickerInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                RowLayout {
                                    width: parent.width
                                    Text {
                                        text: "Resolución de pantalla"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(13 * root.sf)
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                    }
                                    Item { Layout.fillWidth: true }

                                    LumenButton {
                                        sf: root.sf; label: "Refrescar"; variant: "ghost"
                                        implicitWidth: Math.round(84 * root.sf); implicitHeight: Math.round(28 * root.sf)
                                        onClicked: loadDisplayInfo()
                                    }
                                }

                                Text {
                                    text: "Selecciona una resolución para tu pantalla"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf)
                                    color: Tokens.textMuted
                                }

                                Repeater {
                                    model: displayModes
                                    delegate: Item {
                                        width: resPickerInner.width
                                        height: Math.round(36 * root.sf)

                                        readonly property bool isSelected: selectedRes === (modelData.width + "x" + modelData.height)
                                        readonly property bool isCurrent: currentRes.width === modelData.width && currentRes.height === modelData.height

                                        Rectangle {
                                            anchors.fill: parent
                                            radius: root.radiusSm
                                            color: resItemMa.containsMouse
                                                   ? Tokens.bgElevated
                                                   : isSelected ? Tokens.accentSubtle : "transparent"
                                            border.color: isSelected ? Tokens.accentBase : "transparent"
                                            border.width: isSelected ? 1 : 0

                                            Behavior on color {
                                                enabled: !Tokens.reduceMotion
                                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                            }
                                        }

                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: Math.round(Tokens.spMd * root.sf)
                                            anchors.rightMargin: Math.round(Tokens.spMd * root.sf)
                                            spacing: Math.round(Tokens.spSm * root.sf)

                                            // Radio indicator
                                            Rectangle {
                                                width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                                                radius: Math.round(7 * root.sf)
                                                color: "transparent"
                                                border.color: isSelected ? Tokens.accentBase : Tokens.borderStrong
                                                border.width: 1
                                                Rectangle {
                                                    anchors.centerIn: parent
                                                    width: Math.round(8 * root.sf); height: Math.round(8 * root.sf)
                                                    radius: width / 2
                                                    color: Tokens.accentBase
                                                    visible: isSelected
                                                }
                                            }

                                            Text {
                                                text: modelData.width + " × " + modelData.height
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(12 * root.sf)
                                                font.weight: isSelected ? Font.Medium : Font.Normal
                                                color: isSelected ? Tokens.textPrimary : Tokens.textSecondary
                                            }

                                            Text {
                                                text: {
                                                    var ratio = modelData.width / modelData.height;
                                                    if (Math.abs(ratio - 16/9)  < 0.05) return "16:9";
                                                    if (Math.abs(ratio - 16/10) < 0.05) return "16:10";
                                                    if (Math.abs(ratio - 4/3)   < 0.05) return "4:3";
                                                    if (Math.abs(ratio - 5/4)   < 0.05) return "5:4";
                                                    if (Math.abs(ratio - 21/9)  < 0.05) return "21:9";
                                                    return "";
                                                }
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(10 * root.sf)
                                                color: Tokens.textMuted
                                            }

                                            Item { Layout.fillWidth: true }

                                            Text {
                                                text: modelData.refresh + " Hz"
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(10 * root.sf)
                                                color: Tokens.textMuted
                                            }

                                            Rectangle {
                                                visible: isCurrent
                                                width: curResBadge.implicitWidth + Math.round(Tokens.spSm * root.sf)
                                                height: Math.round(16 * root.sf)
                                                radius: Math.round(Tokens.radiusSm * root.sf)
                                                color: Tokens.successSubtle
                                                Text {
                                                    id: curResBadge
                                                    anchors.centerIn: parent
                                                    text: "Actual"
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(8 * root.sf)
                                                    color: Tokens.successBase
                                                }
                                            }

                                            Rectangle {
                                                visible: modelData.preferred || false
                                                width: prefResBadge.implicitWidth + Math.round(Tokens.spSm * root.sf)
                                                height: Math.round(16 * root.sf)
                                                radius: Math.round(Tokens.radiusSm * root.sf)
                                                color: Tokens.accentSubtle
                                                Text {
                                                    id: prefResBadge
                                                    anchors.centerIn: parent
                                                    text: "Recomendada"
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(8 * root.sf)
                                                    color: Tokens.accentBase
                                                }
                                            }
                                        }

                                        MouseArea {
                                            id: resItemMa
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: selectedRes = modelData.width + "x" + modelData.height
                                        }
                                    }
                                }

                                // Apply / Cancel
                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)
                                    visible: selectedRes !== (currentRes.width + "x" + currentRes.height) && !resApplied

                                    Item { Layout.fillWidth: true }

                                    LumenButton {
                                        sf: root.sf; label: "Cancelar"; variant: "ghost"
                                        implicitWidth: Math.round(80 * root.sf); implicitHeight: Math.round(32 * root.sf)
                                        onClicked: selectedRes = currentRes.width + "x" + currentRes.height
                                    }
                                    LumenButton {
                                        sf: root.sf; label: "Aplicar resolución"; variant: "primary"
                                        implicitWidth: Math.round(144 * root.sf); implicitHeight: Math.round(32 * root.sf)
                                        onClicked: applyResolution(selectedRes)
                                    }
                                }

                                // Revert countdown
                                Rectangle {
                                    width: parent.width
                                    height: revertInner.implicitHeight + Math.round(Tokens.spLg * root.sf)
                                    radius: root.radiusSm
                                    color: Tokens.warnSubtle
                                    border.color: Tokens.warnBase
                                    border.width: 1
                                    visible: resApplied

                                    Column {
                                        id: revertInner
                                        anchors.centerIn: parent
                                        spacing: Math.round(Tokens.spSm * root.sf)

                                        Text {
                                            text: "¿Conservar esta resolución?"
                                            font.family: Tokens.fontDisplay
                                            font.pixelSize: Math.round(12 * root.sf)
                                            font.weight: Font.Medium
                                            color: Tokens.warnBase
                                            anchors.horizontalCenter: parent.horizontalCenter
                                        }
                                        Text {
                                            text: "Revirtiendo en " + revertCountdown + " segundos…"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * root.sf)
                                            color: Tokens.textMuted
                                            anchors.horizontalCenter: parent.horizontalCenter
                                        }

                                        RowLayout {
                                            spacing: Math.round(Tokens.spSm * root.sf)
                                            anchors.horizontalCenter: parent.horizontalCenter

                                            LumenButton {
                                                sf: root.sf; label: "Revertir ahora"; variant: "ghost"
                                                implicitWidth: Math.round(110 * root.sf); implicitHeight: Math.round(30 * root.sf)
                                                onClicked: revertResolution()
                                            }
                                            LumenButton {
                                                sf: root.sf; label: "Conservar"; variant: "primary"
                                                implicitWidth: Math.round(110 * root.sf); implicitHeight: Math.round(30 * root.sf)
                                                onClicked: confirmResolution()
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // UI Scaling Card
                        Rectangle {
                            width: parent.width
                            height: scaleInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: Tokens.borderSubtle
                            border.width: 1

                            Column {
                                id: scaleInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spSm * root.sf)

                                Text {
                                    text: "Escala de la interfaz"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(13 * root.sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textPrimary
                                }
                                Text {
                                    text: "Ajusta el tamaño del texto, los iconos y los elementos de la interfaz"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf)
                                    color: Tokens.textMuted
                                }

                                Repeater {
                                    model: [
                                        { label: "Compacto",    scale: 0.75,  desc: "Más contenido, elementos pequeños" },
                                        { label: "Por defecto", scale: 1.0,   desc: "Equilibrado" },
                                        { label: "Cómodo",      scale: 1.15,  desc: "Elementos algo más grandes" },
                                        { label: "Grande",      scale: 1.35,  desc: "Más fácil de leer" },
                                        { label: "Extra grande", scale: 1.6,  desc: "Legibilidad máxima" }
                                    ]
                                    delegate: Item {
                                        width: scaleInner.width
                                        height: Math.round(38 * root.sf)

                                        readonly property bool isActive: Math.abs(root.userScale - modelData.scale) < 0.01

                                        Rectangle {
                                            anchors.fill: parent
                                            radius: root.radiusSm
                                            color: scaleMa.containsMouse
                                                   ? Tokens.bgElevated
                                                   : isActive ? Tokens.accentSubtle : "transparent"
                                            border.color: isActive ? Tokens.accentBase : "transparent"
                                            border.width: isActive ? 1 : 0

                                            Behavior on color {
                                                enabled: !Tokens.reduceMotion
                                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                            }
                                        }

                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: Math.round(Tokens.spMd * root.sf)
                                            anchors.rightMargin: Math.round(Tokens.spMd * root.sf)
                                            spacing: Math.round(Tokens.spSm * root.sf)

                                            // Radio indicator
                                            Rectangle {
                                                width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                                                radius: Math.round(7 * root.sf)
                                                color: "transparent"
                                                border.color: isActive ? Tokens.accentBase : Tokens.borderStrong
                                                border.width: 1
                                                Rectangle {
                                                    anchors.centerIn: parent
                                                    width: Math.round(8 * root.sf); height: Math.round(8 * root.sf)
                                                    radius: width / 2
                                                    color: Tokens.accentBase
                                                    visible: isActive
                                                }
                                            }

                                            Column {
                                                Layout.fillWidth: true
                                                spacing: Math.round(1 * root.sf)
                                                Text {
                                                    text: modelData.label
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(12 * root.sf)
                                                    font.weight: isActive ? Font.Medium : Font.Normal
                                                    color: isActive ? Tokens.textPrimary : Tokens.textSecondary
                                                }
                                                Text {
                                                    text: modelData.desc
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(9 * root.sf)
                                                    color: Tokens.textMuted
                                                }
                                            }

                                            Text {
                                                text: (modelData.scale * 100).toFixed(0) + "%"
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(10 * root.sf)
                                                color: Tokens.textMuted
                                            }
                                        }

                                        MouseArea {
                                            id: scaleMa
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: { root.userScale = modelData.scale; root.showToast("Pantalla: " + modelData.label, "success"); }
                                        }
                                    }
                                }
                            }
                        }

                        // GPU & Driver Info Card
                        Rectangle {
                            width: parent.width
                            height: gpuInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: Tokens.borderSubtle
                            border.width: 1

                            Column {
                                id: gpuInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spMd * root.sf)

                                Text {
                                    text: "Gráficos y controladores"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(13 * root.sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textPrimary
                                }

                                Repeater {
                                    model: [
                                        { label: "GPU",         value: gpuInfo.name       || "Desconocida" },
                                        { label: "Controlador", value: gpuInfo.driver      || "-" },
                                        { label: "Renderer",    value: gpuInfo.renderer    || "-" },
                                        { label: "Compositor",  value: gfxInfo.compositor  || "-" },
                                        { label: "Módulos",     value: gfxInfo.modules     || "Ninguno detectado" }
                                    ]
                                    delegate: Column {
                                        width: gpuInner.width
                                        spacing: 0

                                        Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle; visible: index > 0 }

                                        RowLayout {
                                            width: parent.width
                                            height: Math.round(32 * root.sf)
                                            spacing: Math.round(Tokens.spSm * root.sf)

                                            Text {
                                                Layout.preferredWidth: Math.round(90 * root.sf)
                                                text: modelData.label
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(11 * root.sf)
                                                color: Tokens.textMuted
                                            }
                                            Text {
                                                Layout.fillWidth: true
                                                text: modelData.value
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(11 * root.sf)
                                                color: Tokens.textPrimary
                                                wrapMode: Text.Wrap
                                                elide: Text.ElideRight
                                                maximumLineCount: 2
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // ── STORAGE ──────────────────────────────────────────────
                    Column {
                        width: parent.width
                        spacing: Math.round(Tokens.spMd * root.sf)
                        visible: activeTab === "storage"

                        Text {
                            text: "Almacenamiento"
                            font.family: Tokens.fontDisplay
                            font.pixelSize: Math.round(20 * root.sf)
                            font.weight: Font.Medium
                            color: Tokens.textPrimary
                        }

                        // Workspace Directory Card
                        Rectangle {
                            width: parent.width
                            height: wsInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: Tokens.borderSubtle
                            border.width: 1

                            Column {
                                id: wsInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spMd * root.sf)

                                Text {
                                    text: "Directorio de trabajo del agente"
                                    font.family: Tokens.fontDisplay
                                    font.pixelSize: Math.round(14 * root.sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textPrimary
                                }
                                Text {
                                    width: parent.width
                                    text: "Esta es la carpeta donde Hermes guarda todo su trabajo: ficheros, código, proyectos y artefactos creados por el agente."
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf)
                                    color: Tokens.textMuted
                                    wrapMode: Text.Wrap
                                }

                                Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

                                Text {
                                    text: "Ruta del workspace"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf)
                                    color: Tokens.textMuted
                                }

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)

                                    LumenInput {
                                        id: wsDirInput
                                        Layout.fillWidth: true
                                        sf: root.sf
                                        text: workspaceDir
                                        placeholder: "/home/ainux/Works"
                                        Component.onCompleted: {
                                            // sync to prop on text changes via alias binding
                                        }
                                    }

                                    LumenButton {
                                        sf: root.sf
                                        label: workspaceDirSaving ? "Guardando…" : "Guardar"
                                        variant: "primary"
                                        loading: workspaceDirSaving
                                        implicitWidth: Math.round(88 * root.sf)
                                        implicitHeight: Math.round(38 * root.sf)
                                        onClicked: saveWorkspaceDir(wsDirInput.text)
                                    }
                                }

                                // Bind workspace text changes back to property
                                Connections {
                                    target: wsDirInput
                                    function onTextChanged() { workspaceDir = wsDirInput.text }
                                }

                                Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * root.sf)

                                    Text {
                                        Layout.fillWidth: true
                                        text: "Predeterminado: /home/" + root.currentUser + "/Works"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(10 * root.sf)
                                        color: Tokens.textMuted
                                    }

                                    LumenButton {
                                        sf: root.sf; label: "Restablecer"; variant: "secondary"
                                        implicitWidth: Math.round(104 * root.sf); implicitHeight: Math.round(30 * root.sf)
                                        onClicked: {
                                            workspaceDir = "/home/" + root.currentUser + "/Works";
                                            saveWorkspaceDir(workspaceDir);
                                        }
                                    }
                                }
                            }
                        }

                        // Storage Info Card
                        Rectangle {
                            width: parent.width
                            height: storInner.implicitHeight + Math.round(Tokens.spXl * root.sf)
                            radius: root.radiusMd
                            color: Tokens.bgCard
                            border.color: Tokens.borderSubtle
                            border.width: 1

                            Column {
                                id: storInner
                                anchors {
                                    left: parent.left; right: parent.right; top: parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spMd * root.sf)

                                Text {
                                    text: "Información de almacenamiento"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(13 * root.sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textPrimary
                                }

                                Repeater {
                                    model: [
                                        { label: "Datos",      value: "/var/lib/hermes" },
                                        { label: "Workspace",  value: workspaceDir },
                                        { label: "Logs",       value: "/var/lib/hermes/logs/hermes.log" },
                                        { label: "Base datos", value: "/var/lib/hermes/shell-state.db" }
                                    ]
                                    delegate: Column {
                                        width: storInner.width
                                        spacing: 0

                                        Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle; visible: index > 0 }

                                        RowLayout {
                                            width: parent.width
                                            height: Math.round(32 * root.sf)
                                            spacing: Math.round(Tokens.spSm * root.sf)

                                            Text {
                                                Layout.preferredWidth: Math.round(80 * root.sf)
                                                text: modelData.label
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(11 * root.sf)
                                                color: Tokens.textMuted
                                            }
                                            Text {
                                                Layout.fillWidth: true
                                                text: modelData.value
                                                font.family: Tokens.fontMono
                                                font.pixelSize: Math.round(11 * root.sf)
                                                color: index === 1 ? Tokens.accentBase : Tokens.textPrimary
                                                elide: Text.ElideMiddle
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

            }
        }
    }
}
