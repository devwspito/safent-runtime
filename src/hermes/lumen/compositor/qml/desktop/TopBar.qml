import QtQuick
import QtQuick.Layouts
import "api.js" as API
import "." // Tokens singleton — required for Tokens.X references

Rectangle {
    id: topBar
    height: Math.round(44 * root.sf)
    // Glass base: slightly transparent so the wallpaper/desktop shows through.
    // LumenGlass is not used here because the TopBar is parented inside a
    // WaylandOutput window (not a regular QML Item), so the backdropCapture
    // ShaderEffectSource would have nothing to sample. Use a simple translucent
    // fill instead, which matches the macOS-calm intent at this z-level.
    color: Qt.rgba(Tokens.bgSurface.r, Tokens.bgSurface.g, Tokens.bgSurface.b, 0.88)

    // Single amber hairline bottom border — no tri-color neon
    Rectangle {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: 1
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0;  color: "transparent" }
            GradientStop { position: 0.2;  color: Qt.rgba(240/255, 168/255, 90/255, 0.30) }
            GradientStop { position: 0.5;  color: Qt.rgba(240/255, 168/255, 90/255, 0.45) }
            GradientStop { position: 0.8;  color: Qt.rgba(240/255, 168/255, 90/255, 0.30) }
            GradientStop { position: 1.0;  color: "transparent" }
        }
    }

    // ── Security Center state ──
    // "idle" | "warn" | "fail"
    property string securityState: "idle"

    // ── Modo AUTO indicator — set from Desktop via SettingsApp/hermes ──
    // When true, a persistent amber "AUTO" badge appears so the owner always
    // knows autonomous execution is active.
    property bool autoModeOn: false

    // ── Hermes state ──
    property bool owOnline: false
    property bool owPanelVisible: false
    property string owLogs: ""
    property bool owLogsFetching: false
    property bool owRestarting: false
    property string owUptime: ""

    // ── Time settings state ──
    property bool timePanelVisible: false
    property string currentTimezone: ""
    property bool ntpSynced: false
    property bool ntpActive: false
    property string localTimeStr: ""
    property string utcTimeStr: ""
    property var timezoneList: []
    property string tzSearchFilter: ""
    property bool timeLoading: false
    property bool timeOpPending: false

    // ── Network/WiFi state ──
    property bool netPanelVisible: false
    property bool wifiEnabled: true
    property bool wifiScanning: false
    property bool wifiConnecting: false
    property string wifiConnectingSSID: ""
    property string currentSSID: ""
    property int currentSignal: 0
    property string currentIP: ""
    property string connectionType: "none"  // "wifi", "ethernet", "none"
    property var wifiNetworks: []  // [{ssid, signal, security, connected}]
    property string wifiError: ""
    property bool showPasswordDialog: false
    property string passwordSSID: ""
    property string passwordInput: ""

    Component.onCompleted: { checkOwHealth(); sysManager.getTimeInfoAsync(); refreshNetworkStatus(); }

    // ── Network Functions — ejecución LOCAL via sysManager (el :7778 de
    // WhaleOS no existe). Mantiene la firma (status, body-json {stdout}) para
    // no tocar los 5 call-sites (red/wifi). nmcli corre como el usuario de
    // sesión (polkit de NetworkManager permite gestionar wifi al usuario seated).
    function helperExec(cmd, callback, timeout) {
        var out = sysManager.runCommandQuick(cmd);
        if (callback) callback(200, JSON.stringify({ stdout: out || "" }));
    }

    function refreshNetworkStatus() {
        helperExec(
            "nmcli -t -f TYPE,NAME,DEVICE connection show --active 2>/dev/null; echo '---IP---'; " +
            "ip -4 addr show | grep 'inet ' | grep -v '127.0.0.1' | head -1 | awk '{print $2}' 2>/dev/null; echo '---WIFI---'; " +
            "nmcli -t -f WIFI general 2>/dev/null",
            function(status, body) {
                if (status === 200) {
                    try {
                        var d = JSON.parse(body);
                        var out = d.stdout || "";
                        var parts = out.split("---IP---");
                        var connPart = parts[0] || "";
                        var rest = (parts[1] || "").split("---WIFI---");
                        var ipPart = (rest[0] || "").trim();
                        var wifiPart = (rest[1] || "").trim();

                        currentIP = ipPart.split("/")[0] || "";
                        wifiEnabled = (wifiPart.toLowerCase() === "enabled");

                        // Parse active connection
                        var lines = connPart.trim().split("\n");
                        connectionType = "none";
                        currentSSID = "";
                        for (var i = 0; i < lines.length; i++) {
                            var cols = lines[i].split(":");
                            if (cols.length >= 2) {
                                if (cols[0] === "802-11-wireless" || cols[0] === "wifi") {
                                    connectionType = "wifi";
                                    currentSSID = cols[1];
                                } else if (cols[0] === "802-3-ethernet" || cols[0] === "ethernet") {
                                    connectionType = "ethernet";
                                }
                            }
                        }
                    } catch(e) {}
                }
            }, 5000
        );
    }

    function scanWifi() {
        wifiScanning = true;
        wifiError = "";
        helperExec(
            "nmcli -t -f SSID,SIGNAL,SECURITY,ACTIVE device wifi list --rescan yes 2>/dev/null",
            function(status, body) {
                wifiScanning = false;
                if (status === 200) {
                    try {
                        var d = JSON.parse(body);
                        var out = d.stdout || "";
                        var lines = out.trim().split("\n");
                        var nets = [];
                        var seen = {};
                        for (var i = 0; i < lines.length; i++) {
                            var cols = lines[i].split(":");
                            if (cols.length >= 3 && cols[0].trim() !== "" && cols[0] !== "--") {
                                var ssid = cols[0].trim();
                                if (seen[ssid]) continue;
                                seen[ssid] = true;
                                nets.push({
                                    ssid: ssid,
                                    signal: parseInt(cols[1]) || 0,
                                    security: cols[2] || "Open",
                                    connected: (cols.length >= 4 && cols[3].trim() === "yes")
                                });
                            }
                        }
                        nets.sort(function(a, b) {
                            if (a.connected !== b.connected) return a.connected ? -1 : 1;
                            return b.signal - a.signal;
                        });
                        wifiNetworks = nets;
                        if (nets.length === 0 && out.trim() === "") {
                            wifiError = "No WiFi adapter found (VM has ethernet only)";
                        }
                    } catch(e) {
                        wifiError = "Failed to parse WiFi scan results";
                    }
                } else {
                    wifiError = "WiFi scan failed. Is NetworkManager running?";
                }
            }, 15000
        );
    }

    function connectWifi(ssid, password) {
        wifiConnecting = true;
        wifiConnectingSSID = ssid;
        wifiError = "";
        var cmd = password
            ? "nmcli device wifi connect '" + ssid.replace(/'/g, "'\\''") + "' password '" + password.replace(/'/g, "'\\''") + "' 2>&1"
            : "nmcli device wifi connect '" + ssid.replace(/'/g, "'\\''") + "' 2>&1";
        helperExec(cmd, function(status, body) {
            wifiConnecting = false;
            wifiConnectingSSID = "";
            if (status === 200) {
                try {
                    var d = JSON.parse(body);
                    var out = (d.stdout || "").toLowerCase();
                    if (out.indexOf("error") !== -1 || out.indexOf("failed") !== -1) {
                        wifiError = d.stdout || "Connection failed";
                    } else {
                        showPasswordDialog = false;
                        passwordInput = "";
                        refreshNetworkStatus();
                        scanWifi();
                    }
                } catch(e) {
                    wifiError = "Connection error";
                }
            }
        }, 30000);
    }

    function disconnectWifi() {
        helperExec(
            "nmcli device disconnect $(nmcli -t -f DEVICE,TYPE device | grep wifi | cut -d: -f1 | head -1) 2>/dev/null",
            function() { refreshNetworkStatus(); scanWifi(); }
        );
    }

    function toggleWifi(enable) {
        helperExec(
            "nmcli radio wifi " + (enable ? "on" : "off") + " 2>/dev/null",
            function() {
                wifiEnabled = enable;
                if (enable) { scanWifi(); }
                refreshNetworkStatus();
            }
        );
    }

    // Refresh network status periodically
    Timer {
        interval: 15000; running: true; repeat: true
        onTriggered: refreshNetworkStatus()
    }

    function fetchTimeInfo() {
        timeLoading = true;
        sysManager.getTimeInfoAsync();
    }

    function fetchTimezones() {
        sysManager.getTimezonesAsync();
    }

    function setTimezone(tz) {
        sysManager.setTimezoneAsync(tz);
    }

    function toggleNtp(enable) {
        timeOpPending = true;
        sysManager.toggleNtpAsync(enable);
    }

    function setManualTime(timeStr) {
        sysManager.setManualTimeAsync(timeStr);
    }

    // ── Signal handlers for C++ time management ──
    Connections {
        target: sysManager

        function onTimeInfoReady(timezone, ntpSync, ntpActive, localTime, utcTime) {
            timeLoading = false;
            currentTimezone = timezone || "Unknown";
            localTimeStr = localTime || "";
            utcTimeStr = utcTime || "";
            // Don't override toggle state while an operation is pending
            if (!topBar.timeOpPending) {
                topBar.ntpSynced = ntpSync || false;
                topBar.ntpActive = ntpActive || false;
            }
        }

        function onTimezonesReady(timezones) {
            timezoneList = timezones || [];
        }

        function onTimeOpResult(operation, success, detail) {
            topBar.timeOpPending = false;
            if (operation === "toggleNtp" && success) {
                topBar.ntpActive = (detail === "enabled");
                if (!topBar.ntpActive) topBar.ntpSynced = false;
            } else if (operation === "setTimezone" && success) {
                currentTimezone = detail;
            } else if (operation === "setTime" && success) {
                clockText.text = Qt.formatTime(new Date(), "h:mm AP");
            }
            // Delayed refresh to confirm state after system settles
            delayedRefreshTimer.restart();
        }
    }

    Timer {
        id: delayedRefreshTimer
        interval: 2000; repeat: false
        onTriggered: sysManager.getTimeInfoAsync()
    }

    Timer {
        interval: 10000; running: true; repeat: true
        onTriggered: checkOwHealth()
    }

    // Refresh time info periodically when time panel is open
    Timer {
        interval: 15000; running: timePanelVisible; repeat: true
        onTriggered: sysManager.getTimeInfoAsync()
    }
    onTimePanelVisibleChanged: {
        if (timePanelVisible) {
            sysManager.getTimeInfoAsync();
            if (timezoneList.length === 0) sysManager.getTimezonesAsync();
        }
    }

    // Vida del SO = vida del daemon Hermes (D-Bus), no un helper HTTP de WhaleOS.
    // get_queue_status es read-only (sin authZ); si responde ok, el cerebro está vivo.
    function checkOwHealth() {
        hermes.call("topbar-live", "get_queue_status", "{}");
    }
    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "topbar-live") owOnline = ok;
        }
    }

    function fetchLogs() {
        // Journal REAL del daemon (hermes-user ∈ systemd-journal). Síncrono
        // pero acotado (-n 60 --no-pager); sin HTTP de WhaleOS.
        owLogsFetching = true;
        owLogs = sysManager.runCommandQuick("journalctl -u hermes-runtime -n 60 --no-pager -o short-iso") || "(journal vacío)";
        owLogsFetching = false;
    }

    // Energía REAL. polkit (49-lumenso-power.rules) autoriza a hermes-user
    // (seated) estas acciones exactas SIN contraseña; nada de `sudo` (FR-058
    // prohíbe NOPASSWD en sudoers). kind ∈ {restart-daemon, reboot, poweroff}.
    function powerAction(kind) {
        if (kind === "restart-daemon") {
            owRestarting = true;
            owLogs = "Reiniciando Hermes…";
            var out = sysManager.runCommandQuick("systemctl restart hermes-runtime 2>&1");
            if (out && out.trim().length > 0) {
                owLogs = "No se pudo reiniciar:\n" + out;
                owRestarting = false;
            } else {
                owLogs = "Hermes reiniciado. Comprobando estado…";
                restartTimer.start();
            }
            return;
        }
        // reboot/poweroff: la sesión termina de inmediato → lanzar sin bloquear.
        var cmd = kind === "reboot" ? "systemctl reboot" : "systemctl poweroff";
        sysManager.runCommandAsync(cmd, "");
    }

    function restartOw() { powerAction("restart-daemon"); }

    Timer {
        id: restartTimer; interval: 5000; repeat: false
        onTriggered: { owRestarting = false; checkOwHealth(); }
    }

    // ── Responsive helpers ──
    // Compact when the screen is narrower than bpCompact (portrait / small VM)
    readonly property bool isCompact: root.width < Tokens.bpCompact * root.sf

    // ── Widget Layout ──
    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Math.round(10 * root.sf)
        anchors.rightMargin: Math.round(10 * root.sf)
        anchors.topMargin: Math.round(6 * root.sf)
        anchors.bottomMargin: Math.round(4 * root.sf)
        spacing: Math.round(8 * root.sf)

        // ═══════════════════════════════════
        // LEFT WIDGET — Brand + Status Pill
        // ═══════════════════════════════════
        Rectangle {
            id: brandPill
            Layout.alignment: Qt.AlignVCenter
            width: owLeftRow.width + Math.round(28 * root.sf)
            height: Math.round(32 * root.sf)
            radius: Math.round(16 * root.sf)
            color: Qt.rgba(27/255, 29/255, 36/255, 0.75)
            border.width: 1

            border.color: owAreaMouse.containsMouse
                ? Qt.rgba(240/255, 168/255, 90/255, 0.50)
                : Qt.rgba(240/255, 168/255, 90/255, 0.12)

            scale: owAreaMouse.containsMouse ? 1.03 : 1.0
            Behavior on scale { NumberAnimation { duration: 250; easing.type: Easing.OutBack; easing.overshoot: 1.2 } }
            Behavior on border.color { ColorAnimation { duration: 200 } }

            Row {
                id: owLeftRow
                anchors.centerIn: parent
                spacing: Math.round(7 * root.sf)

                // Hermes orb mark — rotates on hover, no whale_logo dependency
                Text {
                    id: whaleLogo
                    width: Math.round(16 * root.sf); height: Math.round(16 * root.sf)
                    anchors.verticalCenter: parent.verticalCenter
                    text: "◉"
                    font.pixelSize: Math.round(13 * root.sf)
                    color: Tokens.accentBase
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    rotation: owAreaMouse.containsMouse ? 8 : 0
                    Behavior on rotation { enabled: !Tokens.reduceMotion; NumberAnimation { duration: 400; easing.type: Easing.OutBack } }
                }

                // ── Status indicator — static dot (PERF: removed ripple animations
                // that ran infinite scale+opacity loops and dirtied scene every frame)
                Item {
                    width: Math.round(18 * root.sf); height: Math.round(18 * root.sf)
                    anchors.verticalCenter: parent.verticalCenter

                    // Outer glow ring
                    Rectangle {
                        anchors.centerIn: parent
                        width: Math.round(13 * root.sf); height: width; radius: width / 2
                        color: "transparent"
                        border.color: owOnline ? Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.25) : Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.25)
                        border.width: 1
                    }

                    // Core status dot
                    Rectangle {
                        anchors.centerIn: parent
                        width: Math.round(7 * root.sf); height: width; radius: width / 2
                        color: owOnline ? Tokens.successBase : Tokens.dangerBase
                    }
                }

                Text {
                    text: "LumenSO"
                    font.family: Tokens.fontDisplay
                    font.pixelSize: Math.round(12 * root.sf)
                    font.weight: Font.Medium
                    font.letterSpacing: 0.3
                    color: owAreaMouse.containsMouse ? Tokens.accentBase : Tokens.textPrimary
                    anchors.verticalCenter: parent.verticalCenter
                    Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad } }
                }
            }

            MouseArea {
                id: owAreaMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    owPanelVisible = !owPanelVisible;
                    userMenu.visible = false; netPanelVisible = false; timePanelVisible = false;
                    if (owPanelVisible) { checkOwHealth(); }
                }
            }
        }

        // Flexible spacer — pushes clock pill to the true centre regardless of screen width
        Item { width: Math.round(24 * root.sf); height: 1 }  // Row plano: Layout.fillWidth NO aplica (daría Non-existent attached object)

        // ═══════════════════════════════════
        // CENTER WIDGET — Clock Pill
        // ═══════════════════════════════════
        Rectangle {
            id: clockPill
            Layout.alignment: Qt.AlignVCenter
            width: clockRow.width + Math.round(30 * root.sf)
            height: Math.round(32 * root.sf)
            radius: Math.round(16 * root.sf)
            color: Qt.rgba(27/255, 29/255, 36/255, 0.75)
            border.width: 1

            border.color: clockMa.containsMouse
                ? Qt.rgba(240/255, 168/255, 90/255, 0.50)
                : Qt.rgba(240/255, 168/255, 90/255, 0.12)

            scale: clockMa.containsMouse ? 1.03 : 1.0
            Behavior on scale { NumberAnimation { duration: 250; easing.type: Easing.OutBack; easing.overshoot: 1.2 } }
            Behavior on border.color { ColorAnimation { duration: 200 } }

            Row {
                id: clockRow; anchors.centerIn: parent; spacing: Math.round(6 * root.sf)

                Text {
                    id: clockText
                    text: Qt.formatTime(new Date(), "h:mm AP")
                    font.family: Tokens.fontDisplay
                    font.pixelSize: Math.round(13 * root.sf)
                    font.weight: Font.Medium
                    color: clockMa.containsMouse ? Tokens.accentBase : Tokens.textPrimary
                    anchors.verticalCenter: parent.verticalCenter
                    Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad } }

                    Timer {
                        interval: 30000; running: true; repeat: true
                        onTriggered: clockText.text = Qt.formatTime(new Date(), "h:mm AP")
                    }
                }

                // Separator dot + timezone label — hidden on compact screens to prevent
                // the clock pill from overflowing into the controls pill.
                Rectangle {
                    visible: currentTimezone !== "" && !topBar.isCompact
                    width: Math.round(3 * root.sf); height: Math.round(3 * root.sf)
                    radius: width / 2; anchors.verticalCenter: parent.verticalCenter
                    color: Qt.rgba(1, 1, 1, 0.25)
                    opacity: 0.4
                }

                Text {
                    visible: currentTimezone !== "" && !topBar.isCompact
                    text: {
                        var parts = currentTimezone.split("/");
                        return parts.length > 1 ? parts[parts.length - 1].replace(/_/g, " ") : currentTimezone;
                    }
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(10 * root.sf)
                    font.weight: Font.Medium
                    color: clockMa.containsMouse ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.80) : Qt.rgba(Tokens.textSecondary.r, Tokens.textSecondary.g, Tokens.textSecondary.b, 0.60)
                    anchors.verticalCenter: parent.verticalCenter
                    Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                }
            }

            MouseArea {
                id: clockMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                onClicked: {
                    timePanelVisible = !timePanelVisible;
                    owPanelVisible = false; netPanelVisible = false;
                    userMenu.visible = false;
                    if (timePanelVisible) {
                        fetchTimeInfo();
                        if (timezoneList.length === 0) fetchTimezones();
                    }
                }
            }
        }

        // Flexible spacer — balances right side so clock pill stays centred
        Item { width: Math.round(24 * root.sf); height: 1 }  // Row plano: Layout.fillWidth NO aplica (daría Non-existent attached object)

        // ═══════════════════════════════════
        // RIGHT WIDGET — Controls Pill
        // ═══════════════════════════════════
        Rectangle {
            id: controlsPill
            Layout.alignment: Qt.AlignVCenter
            width: rightRow.width + Math.round(22 * root.sf)
            height: Math.round(32 * root.sf)
            radius: Math.round(16 * root.sf)
            color: Qt.rgba(27/255, 29/255, 36/255, 0.75)
            border.color: Qt.rgba(240/255, 168/255, 90/255, 0.10)
            border.width: 1

            Row {
                id: rightRow
                anchors.centerIn: parent
                spacing: Math.round(6 * root.sf)

                // Security shield icon
                Rectangle {
                    width: Math.round(26 * root.sf)
                    height: Math.round(26 * root.sf)
                    radius: Math.round(13 * root.sf)
                    color: shieldMouse.containsMouse
                        ? Qt.rgba(1, 1, 1, 0.10)
                        : "transparent"
                    anchors.verticalCenter: parent.verticalCenter
                    Behavior on color { ColorAnimation { duration: 200 } }
                    scale: shieldMouse.containsMouse ? 1.12 : 1.0
                    Behavior on scale { NumberAnimation { duration: 300; easing.type: Easing.OutBack } }

                    Canvas {
                        id: shieldCanvas
                        anchors.centerIn: parent
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                        property real s: root.sf
                        property string state: securityState

                        onPaint: {
                            var ctx = getContext("2d");
                            ctx.clearRect(0, 0, width, height);
                            ctx.save(); ctx.scale(s, s);
                            var c = state === "fail" ? "#F0768A"
                                  : state === "warn" ? "#EFC05C"
                                  : "#5FD1A8";
                            ctx.strokeStyle = c;
                            ctx.fillStyle   = Qt.rgba(
                                state === "fail" ? 0.94 : state === "warn" ? 0.937 : 0.37,
                                state === "fail" ? 0.46 : state === "warn" ? 0.75  : 0.82,
                                state === "fail" ? 0.54 : state === "warn" ? 0.36  : 0.67,
                                0.18
                            );
                            ctx.lineWidth = 1.3;
                            ctx.lineCap = "round";
                            ctx.lineJoin = "round";
                            // Shield path: top arc + angled sides + pointed bottom
                            ctx.beginPath();
                            ctx.moveTo(7, 1);
                            ctx.lineTo(13, 3.5);
                            ctx.lineTo(13, 7.5);
                            ctx.quadraticCurveTo(13, 12, 7, 14);
                            ctx.quadraticCurveTo(1, 12, 1, 7.5);
                            ctx.lineTo(1, 3.5);
                            ctx.closePath();
                            ctx.fill();
                            ctx.stroke();
                            // Check mark for idle, exclamation for warn/fail
                            ctx.strokeStyle = c;
                            ctx.lineWidth = 1.4;
                            if (state === "idle") {
                                ctx.beginPath();
                                ctx.moveTo(4.5, 7.5); ctx.lineTo(6.2, 9.5); ctx.lineTo(9.5, 5.5);
                                ctx.stroke();
                            } else if (state === "warn") {
                                ctx.beginPath(); ctx.moveTo(7, 4.5); ctx.lineTo(7, 8.5); ctx.stroke();
                                ctx.beginPath(); ctx.arc(7, 10.5, 0.8, 0, Math.PI * 2); ctx.fillStyle = c; ctx.fill();
                            } else {
                                ctx.lineWidth = 1.6;
                                ctx.beginPath(); ctx.moveTo(5, 5); ctx.lineTo(9, 9); ctx.stroke();
                                ctx.beginPath(); ctx.moveTo(9, 5); ctx.lineTo(5, 9); ctx.stroke();
                            }
                            ctx.restore();
                        }
                        onSChanged: requestPaint()
                        onStateChanged: requestPaint()
                        property bool hov: shieldMouse.containsMouse
                        onHovChanged: requestPaint()
                    }

                    MouseArea {
                        id: shieldMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            owPanelVisible = false; netPanelVisible = false;
                            timePanelVisible = false; userMenu.visible = false;
                            openApp("security", "Security Center", "security");
                        }
                    }
                }

                // Settings gear with rotation
                Rectangle {
                    width: Math.round(26 * root.sf)
                    height: Math.round(26 * root.sf)
                    radius: Math.round(13 * root.sf)
                    color: settingsMouse.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.15) : "transparent"
                    anchors.verticalCenter: parent.verticalCenter

                    Behavior on color { ColorAnimation { duration: 200 } }
                    scale: settingsMouse.containsMouse ? 1.12 : 1.0
                    Behavior on scale { NumberAnimation { duration: 300; easing.type: Easing.OutBack } }

                    Canvas {
                        id: gearCanvas
                        anchors.centerIn: parent
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                        property real s: root.sf

                        // Slow rotation on hover
                        rotation: 0
                        RotationAnimation on rotation {
                            running: settingsMouse.containsMouse
                            from: gearCanvas.rotation; to: gearCanvas.rotation + 360
                            duration: 3000; loops: Animation.Infinite
                        }

                        onPaint: {
                            var ctx = getContext("2d");
                            ctx.clearRect(0, 0, width, height);
                            ctx.save(); ctx.scale(s, s);
                            ctx.strokeStyle = settingsMouse.containsMouse ? "#EFA45C" : "#94a3b8";
                            ctx.lineWidth = 1.2;
                            ctx.beginPath();
                            var cx = 7, cy = 7;
                            for (var i = 0; i < 8; i++) {
                                var a1 = (i * Math.PI / 4) - 0.18;
                                var a2 = (i * Math.PI / 4) + 0.18;
                                ctx.lineTo(cx + 6 * Math.cos(a1), cy + 6 * Math.sin(a1));
                                ctx.lineTo(cx + 6 * Math.cos(a2), cy + 6 * Math.sin(a2));
                                var a3 = ((i + 0.5) * Math.PI / 4) - 0.12;
                                var a4 = ((i + 0.5) * Math.PI / 4) + 0.12;
                                ctx.lineTo(cx + 4.5 * Math.cos(a3), cy + 4.5 * Math.sin(a3));
                                ctx.lineTo(cx + 4.5 * Math.cos(a4), cy + 4.5 * Math.sin(a4));
                            }
                            ctx.closePath();
                            ctx.stroke();
                            ctx.beginPath();
                            ctx.arc(cx, cy, 2.2, 0, Math.PI * 2);
                            ctx.stroke();
                            ctx.restore();
                        }
                        onSChanged: requestPaint()
                        property bool hov: settingsMouse.containsMouse
                        onHovChanged: requestPaint()
                    }

                    MouseArea {
                        id: settingsMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: openApp("settings", "Settings", "settings")
                    }
                }

                // WiFi/Network indicator
                Rectangle {
                    width: Math.round(26 * root.sf)
                    height: Math.round(26 * root.sf)
                    radius: Math.round(13 * root.sf)
                    color: wifiMouse.containsMouse ? Qt.rgba(0.0, 0.90, 0.46, 0.15) : "transparent"
                    anchors.verticalCenter: parent.verticalCenter
                    Behavior on color { ColorAnimation { duration: 200 } }
                    scale: wifiMouse.containsMouse ? 1.12 : 1.0
                    Behavior on scale { NumberAnimation { duration: 300; easing.type: Easing.OutBack } }

                    Canvas {
                        id: wifiCanvas
                        anchors.centerIn: parent
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                        property real s: root.sf
                        property string connType: connectionType
                        property bool enabled: wifiEnabled

                        onPaint: {
                            var ctx = getContext("2d");
                            ctx.clearRect(0, 0, width, height);
                            ctx.save(); ctx.scale(s, s);

                            if (connType === "ethernet") {
                                // Ethernet icon — monitor with cable
                                ctx.strokeStyle = "#5BBF8A"; ctx.lineWidth = 1.3;
                                ctx.strokeRect(2, 1, 10, 7);
                                ctx.beginPath(); ctx.moveTo(7, 8); ctx.lineTo(7, 11); ctx.stroke();
                                ctx.beginPath(); ctx.moveTo(4, 11); ctx.lineTo(10, 11); ctx.stroke();
                            } else if (connType === "wifi" && enabled) {
                                // WiFi connected — signal arcs
                                var cx = 7, cy = 12;
                                ctx.strokeStyle = "#5BBF8A"; ctx.lineWidth = 1.3; ctx.lineCap = "round";
                                // Three arcs
                                ctx.beginPath(); ctx.arc(cx, cy, 9, -Math.PI * 0.85, -Math.PI * 0.15); ctx.stroke();
                                ctx.beginPath(); ctx.arc(cx, cy, 6, -Math.PI * 0.80, -Math.PI * 0.20); ctx.stroke();
                                ctx.beginPath(); ctx.arc(cx, cy, 3, -Math.PI * 0.75, -Math.PI * 0.25); ctx.stroke();
                                // Center dot
                                ctx.fillStyle = "#5BBF8A";
                                ctx.beginPath(); ctx.arc(cx, cy, 1.2, 0, Math.PI * 2); ctx.fill();
                            } else if (!enabled) {
                                // WiFi disabled — crossed out
                                var cx2 = 7, cy2 = 12;
                                ctx.strokeStyle = "#6b7280"; ctx.lineWidth = 1.3; ctx.lineCap = "round";
                                ctx.beginPath(); ctx.arc(cx2, cy2, 9, -Math.PI * 0.85, -Math.PI * 0.15); ctx.stroke();
                                ctx.beginPath(); ctx.arc(cx2, cy2, 6, -Math.PI * 0.80, -Math.PI * 0.20); ctx.stroke();
                                // Slash through
                                ctx.strokeStyle = "#ef4444"; ctx.lineWidth = 1.5;
                                ctx.beginPath(); ctx.moveTo(2, 2); ctx.lineTo(12, 12); ctx.stroke();
                            } else {
                                // No connection — gray wifi
                                var cx3 = 7, cy3 = 12;
                                ctx.strokeStyle = "#6b7280"; ctx.lineWidth = 1.3; ctx.lineCap = "round";
                                ctx.beginPath(); ctx.arc(cx3, cy3, 9, -Math.PI * 0.85, -Math.PI * 0.15); ctx.stroke();
                                ctx.beginPath(); ctx.arc(cx3, cy3, 6, -Math.PI * 0.80, -Math.PI * 0.20); ctx.stroke();
                                ctx.beginPath(); ctx.arc(cx3, cy3, 3, -Math.PI * 0.75, -Math.PI * 0.25); ctx.stroke();
                                ctx.fillStyle = "#6b7280";
                                ctx.beginPath(); ctx.arc(cx3, cy3, 1.2, 0, Math.PI * 2); ctx.fill();
                            }
                            ctx.restore();
                        }
                        onConnTypeChanged: requestPaint()
                        onEnabledChanged: requestPaint()
                        onSChanged: requestPaint()
                        property bool hov: wifiMouse.containsMouse
                        onHovChanged: requestPaint()
                    }

                    MouseArea {
                        id: wifiMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            netPanelVisible = !netPanelVisible;
                            owPanelVisible = false; timePanelVisible = false; userMenu.visible = false;
                            if (netPanelVisible) { refreshNetworkStatus(); scanWifi(); }
                        }
                    }
                }

                // ── Modo AUTO badge — visible solo cuando autonomía completa está activa ──
                Rectangle {
                    visible: topBar.autoModeOn
                    height: Math.round(20 * root.sf)
                    width: autoBadgeRow.implicitWidth + Math.round(10 * root.sf)
                    radius: Math.round(5 * root.sf)
                    color: Qt.rgba(0.94, 0.75, 0.36, 0.18)
                    border.width: 1
                    border.color: Qt.rgba(0.94, 0.75, 0.36, 0.55)
                    anchors.verticalCenter: parent.verticalCenter

                    // Gentle pulse to draw attention while AUTO is active.
                    // Gated on !reduceMotion: si no, repinta la TopBar mientras AUTO
                    // esté activo (horas) — caro en reposo para decks/batería.
                    SequentialAnimation on opacity {
                        running: topBar.autoModeOn && !Tokens.reduceMotion
                        loops: Animation.Infinite
                        NumberAnimation { to: 0.60; duration: 1400; easing.type: Easing.InOutSine }
                        NumberAnimation { to: 1.0;  duration: 1400; easing.type: Easing.InOutSine }
                    }

                    Row {
                        id: autoBadgeRow
                        anchors.centerIn: parent
                        spacing: Math.round(4 * root.sf)

                        Rectangle {
                            width: Math.round(5 * root.sf); height: Math.round(5 * root.sf)
                            radius: width / 2; color: Tokens.accentBase
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        Text {
                            text: "AUTO"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(9 * root.sf)
                            font.weight: Font.Bold
                            color: Tokens.accentBase
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        // Clicking the badge opens Settings → permisos for quick disable.
                        onClicked: {
                            owPanelVisible = false; netPanelVisible = false;
                            timePanelVisible = false; userMenu.visible = false;
                            openApp("settings", "Settings", "settings");
                        }
                    }
                }

                // Animated separator
                Rectangle {
                    width: 1; height: Math.round(16 * root.sf)
                    color: Qt.rgba(1, 1, 1, 0.10)
                    anchors.verticalCenter: parent.verticalCenter
                }

                // User avatar with animated ring
                Item {
                    width: Math.round(28 * root.sf)
                    height: Math.round(28 * root.sf)
                    anchors.verticalCenter: parent.verticalCenter

                    // Static amber ring — no rotating gradient, no pink/purple
                    Rectangle {
                        id: avatarRing
                        anchors.centerIn: parent
                        width: parent.width; height: parent.height
                        radius: width / 2
                        color: "transparent"
                        border.width: 1.5
                        border.color: Qt.rgba(240/255, 168/255, 90/255, 0.55)
                    }

                    Rectangle {
                        anchors.centerIn: parent
                        width: Math.round(22 * root.sf)
                        height: width; radius: width / 2

                        gradient: Gradient {
                            GradientStop { position: 0.0; color: Tokens.accentBase }
                            GradientStop { position: 1.0; color: Tokens.accentHover }
                        }

                        scale: avatarMa.containsMouse ? 1.1 : 1.0
                        Behavior on scale { enabled: !Tokens.reduceMotion; NumberAnimation { duration: 200; easing.type: Easing.OutBack } }

                        Text {
                            anchors.centerIn: parent
                            text: root.currentUser.charAt(0).toUpperCase()
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(10 * root.sf)
                            font.weight: Font.Bold
                            color: Tokens.textOnAccent
                        }

                        MouseArea {
                            id: avatarMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: { userMenu.visible = !userMenu.visible; owPanelVisible = false; netPanelVisible = false; timePanelVisible = false; }
                        }
                    }
                }
            }
        }
    }

    // ════════════════════════════════════════════
    // ── Hermes Status Panel (Dropdown) ──
    // ════════════════════════════════════════════
    Rectangle {
        id: owPanel
        visible: owPanelVisible
        parent: topBar.parent
        x: Math.round(10 * root.sf)
        y: topBar.height + Math.round(6 * root.sf)
        width: Math.round(360 * root.sf)
        height: owPanelCol.height + Math.round(20 * root.sf)
        radius: Math.round(Tokens.radiusMd * root.sf)
        color: Tokens.bgElevated
        border.color: Tokens.borderDefault
        border.width: 1
        z: 1001

        Column {
            id: owPanelCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Math.round(10 * root.sf)
            spacing: Math.round(10 * root.sf)

            // ── Header ──
            RowLayout {
                width: parent.width
                spacing: Math.round(8 * root.sf)

                // Hermes orb mark — no whale_logo dependency
                Rectangle {
                    Layout.preferredWidth: Math.round(22 * root.sf)
                    Layout.preferredHeight: Math.round(22 * root.sf)
                    Layout.alignment: Qt.AlignVCenter
                    radius: Math.round(6 * root.sf)
                    color: Tokens.accentSubtle
                    Text {
                        anchors.centerIn: parent
                        text: "◉"
                        font.pixelSize: Math.round(12 * root.sf)
                        color: Tokens.accentBase
                    }
                }

                Column {
                    Layout.fillWidth: true
                    spacing: 1
                    Text {
                        text: "LumenSO"
                        font.family: Tokens.fontDisplay
                        font.pixelSize: Math.round(14 * root.sf)
                        font.weight: Font.DemiBold
                        color: Tokens.textPrimary
                    }
                    Text {
                        text: owUptime || "Powered by Hermes Engine"
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(10 * root.sf)
                        color: Tokens.textMuted
                    }
                }
            }

            Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

            // ── Status Row ──
            Rectangle {
                width: parent.width
                height: Math.round(44 * root.sf)
                radius: root.radiusSm
                color: owOnline ? Qt.rgba(0.0, 0.90, 0.46, 0.06) : Qt.rgba(1.0, 0.09, 0.27, 0.06)
                border.color: owOnline ? Qt.rgba(0.0, 0.90, 0.46, 0.18) : Qt.rgba(1.0, 0.09, 0.27, 0.18)
                border.width: 1

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: Math.round(10 * root.sf)
                    spacing: Math.round(8 * root.sf)

                    Rectangle {
                        width: Math.round(9 * root.sf); height: Math.round(9 * root.sf); radius: width / 2
                        color: owRestarting ? Tokens.warnBase : owOnline ? Tokens.successBase : Tokens.dangerBase
                    }

                    Text {
                        text: owRestarting ? "Restarting..." : owOnline ? "Online" : "Offline"
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(13 * root.sf)
                        font.weight: Font.Medium
                        color: owRestarting ? Tokens.warnBase : owOnline ? Tokens.successBase : Tokens.dangerBase
                        Layout.fillWidth: true
                    }

                    Text {
                        text: "Port 7777"
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(10 * root.sf)
                        color: Tokens.textMuted
                    }
                }
            }

            // ── Action Buttons ──
            RowLayout {
                width: parent.width
                spacing: Math.round(8 * root.sf)

                Rectangle {
                    Layout.fillWidth: true
                    height: Math.round(32 * root.sf)
                    radius: root.radiusSm
                    color: restartMa.containsMouse ? Qt.rgba(0.98, 0.45, 0.09, 0.15) : Qt.rgba(1, 1, 1, 0.04)
                    border.color: Qt.rgba(1, 1, 1, 0.08); border.width: 1

                    Row {
                        anchors.centerIn: parent
                        spacing: Math.round(6 * root.sf)
                        Canvas {
                            width: Math.round(12 * root.sf); height: Math.round(12 * root.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            property real s: root.sf
                            onPaint: {
                                var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                                ctx.save(); ctx.scale(s, s);
                                ctx.strokeStyle = owRestarting ? "#f97316" : "#999"; ctx.lineWidth = 1.5;
                                ctx.beginPath(); ctx.arc(6, 6, 4, -0.5, Math.PI * 1.5); ctx.stroke();
                                ctx.beginPath(); ctx.moveTo(6, 1); ctx.lineTo(9, 2.5); ctx.lineTo(6, 4); ctx.stroke();
                                ctx.restore();
                            }
                            onSChanged: requestPaint()
                        }
                        Text {
                            text: owRestarting ? "Reiniciando…" : "Reiniciar Hermes"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium
                            color: owRestarting ? Tokens.warnBase : Tokens.textSecondary
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                    MouseArea { id: restartMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; enabled: !owRestarting; onClicked: restartOw() }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: Math.round(32 * root.sf)
                    radius: root.radiusSm
                    color: logsMa.containsMouse ? Qt.rgba(0.23, 0.51, 0.96, 0.15) : Qt.rgba(1, 1, 1, 0.04)
                    border.color: Qt.rgba(1, 1, 1, 0.08); border.width: 1

                    Row {
                        anchors.centerIn: parent
                        spacing: Math.round(6 * root.sf)
                        Canvas {
                            width: Math.round(12 * root.sf); height: Math.round(12 * root.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            property real s: root.sf
                            onPaint: {
                                var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                                ctx.save(); ctx.scale(s, s);
                                ctx.strokeStyle = owLogsFetching ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 1.0) : "#999"; ctx.lineWidth = 1.2;
                                ctx.strokeRect(1, 0, 10, 12);
                                ctx.beginPath(); ctx.moveTo(3.5, 3); ctx.lineTo(8.5, 3); ctx.stroke();
                                ctx.beginPath(); ctx.moveTo(3.5, 6); ctx.lineTo(8.5, 6); ctx.stroke();
                                ctx.beginPath(); ctx.moveTo(3.5, 9); ctx.lineTo(6.5, 9); ctx.stroke();
                                ctx.restore();
                            }
                            onSChanged: requestPaint()
                        }
                        Text {
                            text: owLogsFetching ? "Fetching..." : "View Logs"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium
                            color: owLogsFetching ? Tokens.infoBase : Tokens.textSecondary
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                    MouseArea { id: logsMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; enabled: !owLogsFetching; onClicked: fetchLogs() }
                }

                Rectangle {
                    width: Math.round(32 * root.sf); height: Math.round(32 * root.sf)
                    radius: root.radiusSm
                    color: refreshMa.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04)
                    border.color: Qt.rgba(1, 1, 1, 0.08); border.width: 1

                    Canvas {
                        anchors.centerIn: parent
                        width: Math.round(12 * root.sf); height: Math.round(12 * root.sf)
                        property real s: root.sf
                        onPaint: {
                            var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                            ctx.save(); ctx.scale(s, s);
                            ctx.strokeStyle = "#999"; ctx.lineWidth = 1.5;
                            ctx.beginPath(); ctx.arc(6, 6, 4, -0.5, Math.PI * 1.5); ctx.stroke();
                            ctx.beginPath(); ctx.moveTo(6, 1); ctx.lineTo(9, 2.5); ctx.lineTo(6, 4); ctx.stroke();
                            ctx.restore();
                        }
                        onSChanged: requestPaint()
                    }
                    MouseArea { id: refreshMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: checkOwHealth() }
                }
            }

            // ── Logs Viewer ──
            Rectangle {
                visible: owLogs !== ""
                width: parent.width
                height: Math.round(200 * root.sf)
                radius: root.radiusSm
                color: Qt.rgba(0, 0, 0, 0.4)
                border.color: Qt.rgba(1, 1, 1, 0.06); border.width: 1
                clip: true

                Rectangle {
                    id: logsHeader
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    height: Math.round(24 * root.sf)
                    color: Qt.rgba(1, 1, 1, 0.03)

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: Math.round(8 * root.sf); anchors.rightMargin: Math.round(8 * root.sf)

                        Text {
                            text: "LOGS — hermes-runtime"
                            font.family: Tokens.fontMono
                            font.pixelSize: Math.round(9 * root.sf)
                            color: Tokens.textMuted; Layout.fillWidth: true
                        }

                        Text {
                            text: "x"
                            font.pixelSize: Math.round(10 * root.sf); font.weight: Font.Bold
                            color: clearLogsMa.containsMouse ? Tokens.dangerBase : Tokens.textMuted
                            MouseArea { id: clearLogsMa; anchors.fill: parent; anchors.margins: -4; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: owLogs = "" }
                        }
                    }

                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Qt.rgba(1, 1, 1, 0.04) }
                }

                Flickable {
                    anchors.top: logsHeader.bottom; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                    anchors.margins: Math.round(6 * root.sf)
                    contentHeight: logsText.height; clip: true; boundsBehavior: Flickable.StopAtBounds

                    Text {
                        id: logsText
                        width: parent.width
                        text: owLogs
                        font.pixelSize: Math.round(10 * root.sf); font.family: Tokens.fontMono
                        color: Tokens.textMuted; wrapMode: Text.WrapAnywhere; lineHeight: 1.4
                    }
                }
            }

            // ── System Power Options ──
            Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

            RowLayout {
                width: parent.width
                spacing: Math.round(8 * root.sf)

                // Restart System button
                Rectangle {
                    Layout.fillWidth: true
                    height: Math.round(34 * root.sf)
                    radius: root.radiusSm
                    color: restartSysMa.containsMouse ? Qt.rgba(0.98, 0.62, 0.04, 0.15) : Qt.rgba(1, 1, 1, 0.04)
                    border.color: Qt.rgba(1, 1, 1, 0.08); border.width: 1

                    Row {
                        anchors.centerIn: parent
                        spacing: Math.round(6 * root.sf)
                        Canvas {
                            width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            property real s: root.sf
                            onPaint: {
                                var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                                ctx.save(); ctx.scale(s, s);
                                ctx.strokeStyle = "#f59e0b"; ctx.lineWidth = 1.5;
                                ctx.beginPath(); ctx.arc(7, 7, 5, -0.5, Math.PI * 1.5); ctx.stroke();
                                ctx.beginPath(); ctx.moveTo(7, 1); ctx.lineTo(10, 3); ctx.lineTo(7, 5); ctx.fill();
                                ctx.restore();
                            }
                            onSChanged: requestPaint()
                        }
                        Text {
                            text: "Reiniciar equipo"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium
                            color: restartSysMa.containsMouse ? Tokens.warnBase : Tokens.textSecondary
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                    MouseArea {
                        id: restartSysMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: powerAction("reboot")
                    }
                }

                // Shut Down button
                Rectangle {
                    Layout.fillWidth: true
                    height: Math.round(34 * root.sf)
                    radius: root.radiusSm
                    color: shutdownMa.containsMouse ? Qt.rgba(0.94, 0.27, 0.27, 0.15) : Qt.rgba(1, 1, 1, 0.04)
                    border.color: Qt.rgba(1, 1, 1, 0.08); border.width: 1

                    Row {
                        anchors.centerIn: parent
                        spacing: Math.round(6 * root.sf)
                        Canvas {
                            width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            property real s: root.sf
                            onPaint: {
                                var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                                ctx.save(); ctx.scale(s, s);
                                ctx.strokeStyle = "#ef4444"; ctx.lineWidth = 1.8;
                                ctx.beginPath(); ctx.moveTo(7, 1); ctx.lineTo(7, 6); ctx.stroke();
                                ctx.beginPath(); ctx.arc(7, 7, 5, -1.2, Math.PI + 1.2); ctx.stroke();
                                ctx.restore();
                            }
                            onSChanged: requestPaint()
                        }
                        Text {
                            text: "Apagar equipo"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium
                            color: shutdownMa.containsMouse ? Tokens.dangerBase : Tokens.textSecondary
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                    MouseArea {
                        id: shutdownMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: powerAction("poweroff")
                    }
                }
            }
        }
    }

    // ════════════════════════════════════════════
    // ── Network / WiFi Panel (Dropdown) ──
    // ════════════════════════════════════════════
    Rectangle {
        id: netPanel
        visible: netPanelVisible
        parent: topBar.parent
        x: topBar.width - width - Math.round(10 * root.sf)
        y: topBar.height + Math.round(6 * root.sf)
        width: Math.round(340 * root.sf)
        height: netPanelCol.height + Math.round(20 * root.sf)
        radius: Math.round(Tokens.radiusMd * root.sf)
        color: Tokens.bgElevated
        border.color: Tokens.borderDefault; border.width: 1
        z: 1001

        Column {
            id: netPanelCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Math.round(10 * root.sf)
            spacing: Math.round(10 * root.sf)

            // ── Header ──
            RowLayout {
                width: parent.width
                spacing: Math.round(8 * root.sf)

                Canvas {
                    Layout.preferredWidth: Math.round(20 * root.sf)
                    Layout.preferredHeight: Math.round(20 * root.sf)
                    property real s: root.sf
                    onPaint: {
                        var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                        ctx.save(); ctx.scale(s * 1.4, s * 1.4);
                        var cx = 7, cy = 12;
                        ctx.strokeStyle = connectionType !== "none" ? "#5BBF8A" : "#94a3b8";
                        ctx.lineWidth = 1.4; ctx.lineCap = "round";
                        ctx.beginPath(); ctx.arc(cx, cy, 9, -Math.PI * 0.85, -Math.PI * 0.15); ctx.stroke();
                        ctx.beginPath(); ctx.arc(cx, cy, 6, -Math.PI * 0.80, -Math.PI * 0.20); ctx.stroke();
                        ctx.beginPath(); ctx.arc(cx, cy, 3, -Math.PI * 0.75, -Math.PI * 0.25); ctx.stroke();
                        ctx.fillStyle = ctx.strokeStyle;
                        ctx.beginPath(); ctx.arc(cx, cy, 1.2, 0, Math.PI * 2); ctx.fill();
                        ctx.restore();
                    }
                    onSChanged: requestPaint()
                }

                Column {
                    Layout.fillWidth: true
                    spacing: 1
                    Text {
                        text: "Network"
                        font.family: Tokens.fontDisplay
                        font.pixelSize: Math.round(14 * root.sf)
                        font.weight: Font.DemiBold
                        color: Tokens.textPrimary
                    }
                    Text {
                        text: connectionType === "wifi" ? "Connected to " + currentSSID
                            : connectionType === "ethernet" ? "Wired Connection"
                            : "Not Connected"
                        font.pixelSize: Math.round(10 * root.sf)
                        color: root.textMuted
                        elide: Text.ElideRight
                        width: parent.width
                    }
                }

                // Close button
                Rectangle {
                    Layout.preferredWidth: Math.round(24 * root.sf)
                    Layout.preferredHeight: Math.round(24 * root.sf)
                    radius: Math.round(12 * root.sf)
                    color: netCloseMa.containsMouse ? Qt.rgba(1,1,1,0.1) : "transparent"
                    Text {
                        anchors.centerIn: parent; text: "✕"
                        font.pixelSize: Math.round(11 * root.sf); color: root.textMuted
                    }
                    MouseArea { id: netCloseMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: netPanelVisible = false }
                }
            }

            Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

            // ── Connection Status ──
            Rectangle {
                width: parent.width
                height: Math.round(44 * root.sf)
                radius: root.radiusSm
                color: connectionType !== "none"
                    ? Qt.rgba(0.13, 0.77, 0.37, 0.06)
                    : Qt.rgba(0.42, 0.46, 0.50, 0.06)
                border.color: connectionType !== "none"
                    ? Qt.rgba(0.13, 0.77, 0.37, 0.15)
                    : Qt.rgba(0.42, 0.46, 0.50, 0.15)
                border.width: 1

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: Math.round(10 * root.sf)
                    spacing: Math.round(8 * root.sf)

                    Rectangle {
                        width: Math.round(9 * root.sf); height: width; radius: width/2
                        color: connectionType !== "none" ? Tokens.successBase : Tokens.textDisabled
                    }

                    Column {
                        Layout.fillWidth: true
                        spacing: 1
                        Text {
                            text: connectionType === "wifi" ? currentSSID
                                : connectionType === "ethernet" ? "Ethernet"
                                : "Disconnected"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(12 * root.sf)
                            font.weight: Font.Medium
                            color: connectionType !== "none" ? Tokens.successBase : Tokens.textMuted
                        }
                        Text {
                            visible: currentIP !== ""
                            text: currentIP
                            font.family: Tokens.fontMono
                            font.pixelSize: Math.round(9 * root.sf)
                            color: Tokens.textMuted
                        }
                    }

                    // Disconnect button (for WiFi)
                    Rectangle {
                        visible: connectionType === "wifi"
                        width: Math.round(70 * root.sf); height: Math.round(24 * root.sf)
                        radius: Math.round(12 * root.sf)
                        color: disconnMa.containsMouse ? Qt.rgba(0.94, 0.27, 0.27, 0.15) : Qt.rgba(1,1,1,0.05)
                        border.color: Qt.rgba(1,1,1,0.08); border.width: 1
                        Text {
                            anchors.centerIn: parent; text: "Disconnect"
                            font.pixelSize: Math.round(9 * root.sf); font.weight: Font.Medium
                            color: disconnMa.containsMouse ? "#ef4444" : root.textMuted
                        }
                        MouseArea { id: disconnMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: disconnectWifi() }
                    }
                }
            }

            // ── WiFi Toggle ──
            Rectangle {
                width: parent.width
                height: Math.round(40 * root.sf)
                radius: root.radiusSm
                color: Qt.rgba(1, 1, 1, 0.03)
                border.color: Qt.rgba(1,1,1,0.06); border.width: 1

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: Math.round(10 * root.sf)
                    spacing: Math.round(8 * root.sf)

                    Text {
                        text: "Wi-Fi"
                        font.pixelSize: Math.round(12 * root.sf); font.weight: Font.Medium
                        color: Tokens.textPrimary; Layout.fillWidth: true
                    }

                    // Toggle switch
                    Rectangle {
                        width: Math.round(40 * root.sf); height: Math.round(22 * root.sf)
                        radius: Math.round(11 * root.sf)
                        color: wifiEnabled ? Tokens.successBase : Tokens.borderDefault
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: 200 } }

                        Rectangle {
                            width: Math.round(18 * root.sf); height: Math.round(18 * root.sf)
                            radius: Math.round(9 * root.sf)
                            color: Tokens.bgVoid
                            x: wifiEnabled ? parent.width - width - Math.round(2 * root.sf) : Math.round(2 * root.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            Behavior on x { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
                        }

                        MouseArea {
                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            onClicked: toggleWifi(!wifiEnabled)
                        }
                    }
                }
            }

            // ── WiFi Networks Header ──
            RowLayout {
                visible: wifiEnabled
                width: parent.width
                spacing: Math.round(6 * root.sf)

                Text {
                    text: "Available Networks"
                    font.pixelSize: Math.round(10 * root.sf)
                    font.weight: Font.DemiBold
                    color: root.textMuted
                    Layout.fillWidth: true
                    font.letterSpacing: 0.5
                }

                // Scan/refresh
                Rectangle {
                    Layout.preferredWidth: Math.round(24 * root.sf)
                    Layout.preferredHeight: Math.round(24 * root.sf)
                    radius: Math.round(12 * root.sf)
                    color: scanMa.containsMouse ? Qt.rgba(1,1,1,0.1) : "transparent"

                    Canvas {
                        anchors.centerIn: parent
                        width: Math.round(12 * root.sf); height: Math.round(12 * root.sf)
                        property real s: root.sf
                        rotation: wifiScanning ? 360 : 0
                        Behavior on rotation { RotationAnimation { duration: 1000; loops: Animation.Infinite } }
                        onPaint: {
                            var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                            ctx.save(); ctx.scale(s, s);
                            ctx.strokeStyle = wifiScanning ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 1.0) : "#999"; ctx.lineWidth = 1.5;
                            ctx.beginPath(); ctx.arc(6, 6, 4, -0.5, Math.PI * 1.5); ctx.stroke();
                            ctx.beginPath(); ctx.moveTo(6, 1); ctx.lineTo(9, 2.5); ctx.lineTo(6, 4); ctx.stroke();
                            ctx.restore();
                        }
                        onSChanged: requestPaint()
                    }
                    MouseArea { id: scanMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: scanWifi() }
                }
            }

            // ── Error message ──
            Text {
                visible: wifiError !== ""
                width: parent.width
                text: wifiError
                font.pixelSize: Math.round(10 * root.sf)
                color: Tokens.dangerBase
                wrapMode: Text.WordWrap
            }

            // ── Scanning indicator ──
            Text {
                visible: wifiScanning && wifiNetworks.length === 0
                width: parent.width
                text: "Scanning for networks..."
                font.pixelSize: Math.round(11 * root.sf)
                color: root.textMuted
                horizontalAlignment: Text.AlignHCenter
            }

            // ── Network List ──
            Rectangle {
                visible: wifiEnabled && wifiNetworks.length > 0
                width: parent.width
                height: Math.min(Math.round(240 * root.sf), netListCol.height + Math.round(4 * root.sf))
                radius: root.radiusSm
                color: Qt.rgba(0, 0, 0, 0.2)
                border.color: Qt.rgba(1,1,1,0.06); border.width: 1
                clip: true

                Flickable {
                    anchors.fill: parent
                    anchors.margins: Math.round(2 * root.sf)
                    contentHeight: netListCol.height
                    clip: true; boundsBehavior: Flickable.StopAtBounds

                    Column {
                        id: netListCol
                        width: parent.width
                        spacing: Math.round(2 * root.sf)

                        Repeater {
                            model: wifiNetworks

                            Rectangle {
                                width: netListCol.width
                                height: Math.round(42 * root.sf)
                                radius: root.radiusSm
                                color: netItemMa.containsMouse
                                    ? Qt.rgba(1, 1, 1, 0.08)
                                    : modelData.connected ? Qt.rgba(0.13, 0.77, 0.37, 0.05) : "transparent"
                                Behavior on color { ColorAnimation { duration: 150 } }

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: Math.round(10 * root.sf)
                                    anchors.rightMargin: Math.round(10 * root.sf)
                                    spacing: Math.round(8 * root.sf)

                                    // Signal strength bars
                                    Row {
                                        spacing: Math.round(1.5 * root.sf)
                                        Layout.alignment: Qt.AlignVCenter

                                        Repeater {
                                            model: 4
                                            Rectangle {
                                                width: Math.round(3 * root.sf)
                                                height: Math.round((4 + index * 3) * root.sf)
                                                radius: Math.round(1 * root.sf)
                                                anchors.bottom: parent.bottom
                                                color: {
                                                    var sig = modelData.signal;
                                                    var threshold = [0, 25, 50, 75][index];
                                                    if (sig > threshold) {
                                                        return modelData.connected ? "#34d399" : "#94a3b8";
                                                    }
                                                    return Qt.rgba(1,1,1,0.1);
                                                }
                                            }
                                        }
                                    }

                                    // SSID + security
                                    Column {
                                        Layout.fillWidth: true
                                        spacing: 1

                                        Text {
                                            text: modelData.ssid
                                            font.pixelSize: Math.round(11.5 * root.sf)
                                            font.weight: modelData.connected ? Font.DemiBold : Font.Normal
                                            color: modelData.connected ? Tokens.successBase : Tokens.textPrimary
                                            elide: Text.ElideRight
                                            width: parent.width
                                        }

                                        Text {
                                            text: modelData.connected ? "Connected"
                                                : modelData.security !== "" && modelData.security !== "--" ? "🔒 " + modelData.security
                                                : "Open"
                                            font.pixelSize: Math.round(9 * root.sf)
                                            color: modelData.connected ? Qt.rgba(0.2, 0.83, 0.6, 0.6) : root.textMuted
                                        }
                                    }

                                    // Connect button
                                    Rectangle {
                                        visible: !modelData.connected && !(wifiConnecting && wifiConnectingSSID === modelData.ssid)
                                        width: Math.round(60 * root.sf); height: Math.round(24 * root.sf)
                                        radius: Math.round(12 * root.sf)
                                        color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, netConnMa.containsMouse ? 0.25 : 0.10)
                                        border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.35); border.width: 1
                                        Text {
                                            anchors.centerIn: parent; text: "Connect"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(9 * root.sf); font.weight: Font.Medium
                                            color: Tokens.accentBase
                                        }
                                        MouseArea {
                                            id: netConnMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                var sec = modelData.security || "";
                                                if (sec !== "" && sec !== "--" && sec.toLowerCase() !== "open") {
                                                    passwordSSID = modelData.ssid;
                                                    passwordInput = "";
                                                    showPasswordDialog = true;
                                                } else {
                                                    connectWifi(modelData.ssid, "");
                                                }
                                            }
                                        }
                                    }

                                    // Connecting spinner text
                                    Text {
                                        visible: wifiConnecting && wifiConnectingSSID === modelData.ssid
                                        text: "Connecting..."
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(9 * root.sf)
                                        color: Tokens.accentBase
                                    }
                                }

                                MouseArea {
                                    id: netItemMa; anchors.fill: parent; hoverEnabled: true
                                    // Just hover effect, clicking is via Connect button
                                    acceptedButtons: Qt.NoButton
                                }
                            }
                        }
                    }
                }
            }

            // ── Password Dialog ──
            Rectangle {
                visible: showPasswordDialog
                width: parent.width
                height: pwdCol.height + Math.round(16 * root.sf)
                radius: root.radiusSm
                color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.06)
                border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.22); border.width: 1

                Column {
                    id: pwdCol
                    anchors.left: parent.left; anchors.right: parent.right
                    anchors.top: parent.top; anchors.margins: Math.round(8 * root.sf)
                    spacing: Math.round(8 * root.sf)

                    Text {
                        text: "Enter password for \"" + passwordSSID + "\""
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium
                        color: Tokens.textPrimary; wrapMode: Text.WordWrap; width: parent.width
                    }

                    Rectangle {
                        width: parent.width; height: Math.round(34 * root.sf)
                        radius: root.radiusSm
                        color: Qt.rgba(Tokens.bgSunken.r, Tokens.bgSunken.g, Tokens.bgSunken.b, 0.9)
                        border.color: pwdInput.activeFocus ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40) : Tokens.borderSubtle
                        border.width: 1

                        TextInput {
                            id: pwdInput
                            anchors.fill: parent; anchors.margins: Math.round(8 * root.sf)
                            font.family: Tokens.fontBody; font.pixelSize: Math.round(12 * root.sf)
                            color: Tokens.textPrimary; echoMode: TextInput.Password
                            clip: true
                            onTextChanged: passwordInput = text
                            onAccepted: { if (passwordInput.length > 0) connectWifi(passwordSSID, passwordInput); }

                            Text {
                                visible: parent.text === ""
                                text: "Password"
                                font.family: Tokens.fontBody; font.pixelSize: Math.round(12 * root.sf)
                                color: Tokens.textDisabled
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }
                    }

                    RowLayout {
                        width: parent.width
                        spacing: Math.round(6 * root.sf)

                        Rectangle {
                            Layout.fillWidth: true; height: Math.round(30 * root.sf)
                            radius: root.radiusSm
                            color: cancelPwdMa.containsMouse ? Qt.rgba(1,1,1,0.08) : Qt.rgba(1,1,1,0.04)
                            border.color: Qt.rgba(1,1,1,0.08); border.width: 1
                            Text { anchors.centerIn: parent; text: "Cancel"; font.pixelSize: Math.round(11 * root.sf); color: root.textMuted }
                            MouseArea { id: cancelPwdMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: { showPasswordDialog = false; passwordInput = ""; }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true; height: Math.round(30 * root.sf)
                            radius: root.radiusSm
                            color: connectPwdMa.containsMouse ? Tokens.accentSubtle : Tokens.accentGhost
                            border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40); border.width: 1
                            Text { anchors.centerIn: parent; text: wifiConnecting ? "Connecting..." : "Connect"; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium; color: Tokens.accentBase }
                            MouseArea { id: connectPwdMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                enabled: passwordInput.length > 0 && !wifiConnecting
                                onClicked: connectWifi(passwordSSID, passwordInput)
                            }
                        }
                    }
                }
            }

            // ── No WiFi networks found ──
            Text {
                visible: wifiEnabled && !wifiScanning && wifiNetworks.length === 0 && wifiError === ""
                width: parent.width
                text: "No WiFi networks found"
                font.pixelSize: Math.round(11 * root.sf)
                color: root.textMuted
                horizontalAlignment: Text.AlignHCenter
            }
        }
    }

    // ════════════════════════════════════════════
    // ── Time Settings Panel (Dropdown) ──
    // ════════════════════════════════════════════
    Rectangle {
        id: timePanel
        visible: timePanelVisible
        parent: topBar.parent
        x: (topBar.width - width) / 2 // centered below clock
        y: topBar.height + Math.round(6 * root.sf)
        width: Math.round(340 * root.sf)
        height: timePanelCol.height + Math.round(24 * root.sf)
        radius: Math.round(Tokens.radiusMd * root.sf)
        color: Tokens.bgElevated
        border.color: Tokens.borderDefault; border.width: 1
        z: 1001

        Column {
            id: timePanelCol
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: Math.round(14 * root.sf)
            spacing: Math.round(12 * root.sf)

            // ── Header: Date & Time ──
            RowLayout {
                width: parent.width; spacing: Math.round(10 * root.sf)

                Canvas {
                    width: Math.round(20 * root.sf); height: Math.round(20 * root.sf)
                    Layout.alignment: Qt.AlignVCenter
                    property real s: root.sf
                    onPaint: {
                        var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                        ctx.save(); ctx.scale(s, s);
                        ctx.strokeStyle = Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 1.0); ctx.lineWidth = 1.5;
                        ctx.beginPath(); ctx.arc(10, 10, 8, 0, Math.PI * 2); ctx.stroke();
                        ctx.beginPath(); ctx.moveTo(10, 4); ctx.lineTo(10, 10); ctx.lineTo(14, 12); ctx.stroke();
                        ctx.restore();
                    }
                    onSChanged: requestPaint()
                }

                Column {
                    Layout.fillWidth: true; spacing: Math.round(2 * root.sf)
                    Text {
                        text: "Date & Time"
                        font.family: Tokens.fontDisplay
                        font.pixelSize: Math.round(14 * root.sf); font.weight: Font.DemiBold; color: Tokens.textPrimary
                    }
                    Text {
                        text: Qt.formatDate(new Date(), "dddd, MMMM d, yyyy")
                        font.pixelSize: Math.round(11 * root.sf); color: root.textMuted
                    }
                }

                Rectangle {
                    width: Math.round(24 * root.sf); height: Math.round(24 * root.sf)
                    radius: Math.round(6 * root.sf)
                    color: closeTimeMa.containsMouse ? Qt.rgba(1,1,1,0.1) : "transparent"

                    Text {
                        anchors.centerIn: parent; text: "✕"
                        font.pixelSize: Math.round(12 * root.sf); color: root.textMuted
                    }
                    MouseArea {
                        id: closeTimeMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: timePanelVisible = false
                    }
                }
            }

            Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

            // ── Current Time Display ──
            Rectangle {
                width: parent.width; height: Math.round(54 * root.sf); radius: Math.round(8 * root.sf)
                color: Qt.rgba(Tokens.bgSunken.r, Tokens.bgSunken.g, Tokens.bgSunken.b, 0.8)
                border.color: Tokens.borderDefault; border.width: 1

                Row {
                    anchors.centerIn: parent; spacing: Math.round(12 * root.sf)

                    Text {
                        text: Qt.formatTime(new Date(), "h:mm:ss AP")
                        font.pixelSize: Math.round(22 * root.sf); font.weight: Font.Bold
                        font.family: Tokens.fontMono; color: Tokens.textPrimary
                        anchors.verticalCenter: parent.verticalCenter

                        Timer {
                            interval: 1000; running: timePanelVisible; repeat: true
                            onTriggered: parent.text = Qt.formatTime(new Date(), "h:mm:ss AP")
                        }
                    }

                    // NTP badge
                    Rectangle {
                        visible: ntpActive
                        width: ntpBadgeText.width + Math.round(12 * root.sf)
                        height: Math.round(20 * root.sf); radius: 10
                        anchors.verticalCenter: parent.verticalCenter
                        color: ntpSynced ? Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.15) : Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.15)

                        Text {
                            id: ntpBadgeText; anchors.centerIn: parent
                            text: ntpSynced ? "NTP ✓" : "NTP ⋯"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(9 * root.sf); font.weight: Font.Bold
                            color: ntpSynced ? Tokens.successBase : Tokens.warnBase
                        }
                    }
                }
            }

            // ── Timezone Section ──
            Column {
                width: parent.width; spacing: Math.round(8 * root.sf)

                RowLayout {
                    width: parent.width; spacing: Math.round(8 * root.sf)

                    Text {
                        text: "Timezone"
                        font.pixelSize: Math.round(12 * root.sf); font.weight: Font.DemiBold; color: root.textSecondary
                        Layout.fillWidth: true
                    }

                    Text {
                        text: currentTimezone || "Loading..."
                        font.family: Tokens.fontMono
                        font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium
                        color: Tokens.accentBase
                    }
                }

                // Timezone search
                Rectangle {
                    width: parent.width; height: Math.round(34 * root.sf); radius: Math.round(8 * root.sf)
                    color: Qt.rgba(Tokens.bgSunken.r, Tokens.bgSunken.g, Tokens.bgSunken.b, 0.9); border.color: Tokens.borderSubtle; border.width: 1

                    TextInput {
                        id: tzSearch; anchors.fill: parent
                        anchors.leftMargin: Math.round(10 * root.sf); anchors.rightMargin: Math.round(10 * root.sf)
                        color: Tokens.textPrimary; font.family: Tokens.fontBody; font.pixelSize: Math.round(12 * root.sf)
                        clip: true; verticalAlignment: TextInput.AlignVCenter
                        onTextChanged: tzSearchFilter = text.toLowerCase()

                        Text {
                            anchors.fill: parent; verticalAlignment: Text.AlignVCenter
                            text: "🔍 Search timezones..."
                            color: Qt.rgba(1,1,1,0.2); font.pixelSize: Math.round(11 * root.sf)
                            visible: !parent.text
                        }
                    }
                }

                // Timezone list (scrollable, filtered)
                Rectangle {
                    visible: tzSearchFilter.length >= 2
                    width: parent.width; height: Math.min(Math.round(180 * root.sf), tzListView.contentHeight + 4)
                    radius: Math.round(8 * root.sf)
                    color: Qt.rgba(0, 0, 0, 0.25); border.color: Qt.rgba(1,1,1,0.06); border.width: 1
                    clip: true

                    ListView {
                        id: tzListView; anchors.fill: parent; anchors.margins: 2
                        clip: true; spacing: 1
                        model: {
                            if (tzSearchFilter.length < 2) return [];
                            var filtered = [];
                            for (var i = 0; i < timezoneList.length && filtered.length < 50; i++) {
                                if (timezoneList[i].toLowerCase().indexOf(tzSearchFilter) >= 0) {
                                    filtered.push(timezoneList[i]);
                                }
                            }
                            return filtered;
                        }

                        delegate: Rectangle {
                            width: tzListView.width; height: Math.round(30 * root.sf); radius: Math.round(4 * root.sf)
                            color: tzItemMa.containsMouse ? Tokens.accentSubtle :
                                   modelData === currentTimezone ? Tokens.accentGhost : "transparent"

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.left: parent.left; anchors.leftMargin: Math.round(10 * root.sf)
                                text: (modelData === currentTimezone ? "✓ " : "") + modelData
                                font.family: Tokens.fontMono
                                font.pixelSize: Math.round(11 * root.sf)
                                color: modelData === currentTimezone ? Tokens.accentBase : Tokens.textPrimary
                                font.weight: modelData === currentTimezone ? Font.DemiBold : Font.Normal
                            }

                            MouseArea {
                                id: tzItemMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: { setTimezone(modelData); tzSearch.text = ""; tzSearchFilter = ""; }
                            }
                        }
                    }
                }
            }

            Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

            // ── NTP Sync Toggle ──
            Rectangle {
                width: parent.width; height: Math.round(44 * root.sf); radius: Math.round(8 * root.sf)
                color: Qt.rgba(0,0,0,0.15); border.color: Qt.rgba(1,1,1,0.06); border.width: 1

                RowLayout {
                    anchors.fill: parent; anchors.margins: Math.round(10 * root.sf); spacing: Math.round(10 * root.sf)

                    Column {
                        Layout.fillWidth: true; spacing: Math.round(2 * root.sf)

                        Text {
                            text: "Automatic Time Sync (NTP)"
                            font.pixelSize: Math.round(12 * root.sf); font.weight: Font.Medium; color: root.textPrimary
                        }
                        Text {
                            text: ntpActive ? (ntpSynced ? "Synchronized with time server" : "Waiting for sync...") : "Manual time mode"
                            font.pixelSize: Math.round(10 * root.sf); color: root.textMuted
                        }
                    }

                    // Toggle button
                    Rectangle {
                        width: Math.round(46 * root.sf); height: Math.round(24 * root.sf)
                        radius: Math.round(12 * root.sf)
                        color: ntpActive ? Qt.rgba(0.13, 0.77, 0.37, 0.3) : Qt.rgba(1,1,1,0.1)
                        border.color: ntpActive ? Qt.rgba(0.13, 0.77, 0.37, 0.5) : Qt.rgba(1,1,1,0.15)
                        border.width: 1

                        Rectangle {
                            width: Math.round(18 * root.sf); height: Math.round(18 * root.sf)
                            radius: Math.round(9 * root.sf)
                            x: ntpActive ? parent.width - width - Math.round(3 * root.sf) : Math.round(3 * root.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            color: ntpActive ? "#22c55e" : "#888"
                            Behavior on x { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
                        }

                        MouseArea {
                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                var newState = !ntpActive;
                                ntpActive = newState;
                                if (!newState) ntpSynced = false;
                                toggleNtp(newState);
                            }
                        }
                    }
                }
            }

            // ── Manual Time (visible when NTP is off) ──
            Rectangle {
                visible: !ntpActive
                width: parent.width; height: manualTimeCol.height + Math.round(16 * root.sf)
                radius: Math.round(8 * root.sf)
                color: Qt.rgba(0,0,0,0.15); border.color: Qt.rgba(1,1,1,0.06); border.width: 1

                Column {
                    id: manualTimeCol; anchors.left: parent.left; anchors.right: parent.right
                    anchors.top: parent.top; anchors.margins: Math.round(10 * root.sf)
                    spacing: Math.round(8 * root.sf)

                    Text {
                        text: "Set Time Manually"
                        font.pixelSize: Math.round(11 * root.sf); font.weight: Font.DemiBold; color: root.textSecondary
                    }

                    Row {
                        spacing: Math.round(8 * root.sf)

                        Rectangle {
                            width: Math.round(190 * root.sf); height: Math.round(32 * root.sf); radius: Math.round(6 * root.sf)
                            color: Qt.rgba(Tokens.bgSunken.r, Tokens.bgSunken.g, Tokens.bgSunken.b, 0.9); border.color: Tokens.borderSubtle; border.width: 1

                            TextInput {
                                id: manualTimeInput; anchors.fill: parent
                                anchors.leftMargin: Math.round(8 * root.sf); anchors.rightMargin: Math.round(8 * root.sf)
                                color: Tokens.textPrimary; font.pixelSize: Math.round(12 * root.sf); font.family: Tokens.fontMono
                                clip: true; verticalAlignment: TextInput.AlignVCenter
                                Keys.onReturnPressed: setManualTime(text.trim())

                                Text {
                                    anchors.fill: parent; verticalAlignment: Text.AlignVCenter
                                    text: Qt.formatDateTime(new Date(), "yyyy-MM-dd HH:mm:ss")
                                    color: Qt.rgba(1,1,1,0.2); font.pixelSize: Math.round(11 * root.sf); font.family: "monospace"
                                    visible: !parent.text
                                }
                            }
                        }

                        Rectangle {
                            width: Math.round(60 * root.sf); height: Math.round(32 * root.sf); radius: Math.round(6 * root.sf)
                            color: setTimeMa.containsMouse ? Tokens.accentHover : Tokens.accentBase

                            Text {
                                anchors.centerIn: parent; text: "Set"
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * root.sf); font.weight: Font.DemiBold; color: Tokens.textOnAccent
                            }
                            MouseArea {
                                id: setTimeMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: { if (manualTimeInput.text.trim()) setManualTime(manualTimeInput.text.trim()); }
                            }
                        }
                    }

                    Text {
                        text: "Format: YYYY-MM-DD HH:MM:SS"
                        font.pixelSize: Math.round(9 * root.sf); color: root.textMuted
                    }
                }
            }

            // ── UTC time ──
            Row {
                visible: utcTimeStr !== ""; spacing: Math.round(6 * root.sf)
                Text { text: "UTC:"; font.pixelSize: Math.round(10 * root.sf); font.weight: Font.DemiBold; color: root.textMuted }
                Text { text: utcTimeStr; font.pixelSize: Math.round(10 * root.sf); color: root.textMuted; font.family: "monospace" }
            }
        }
    }

    // ── Click-outside to close panels ──
    MouseArea {
        visible: owPanelVisible || timePanelVisible
        parent: topBar.parent
        anchors.fill: parent
        anchors.topMargin: topBar.height
        z: 999
        onClicked: { owPanelVisible = false; timePanelVisible = false; }
    }

    // ── User Dropdown Menu ──
    Rectangle {
        id: userMenu
        visible: false
        anchors.right: parent.right
        anchors.top: parent.bottom
        anchors.rightMargin: Math.round(14 * root.sf)
        anchors.topMargin: Math.round(6 * root.sf)
        width: Math.round(160 * root.sf)
        height: menuCol.height + Math.round(12 * root.sf)
        radius: Math.round(Tokens.radiusMd * root.sf)
        color: Tokens.bgElevated
        border.color: Tokens.borderDefault; border.width: 1
        z: 1000

        Column {
            id: menuCol
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: Math.round(6 * root.sf)
            spacing: 2

            Rectangle {
                width: parent.width; height: Math.round(36 * root.sf)
                radius: root.radiusSm; color: "transparent"

                Row {
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: parent.left; anchors.leftMargin: Math.round(8 * root.sf)
                    spacing: Math.round(8 * root.sf)

                    Canvas {
                        width: Math.round(13 * root.sf); height: Math.round(13 * root.sf)
                        anchors.verticalCenter: parent.verticalCenter
                        property real s: root.sf
                        onPaint: {
                            var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                            ctx.save(); ctx.scale(s, s);
                            ctx.strokeStyle = "#999"; ctx.lineWidth = 1.2;
                            ctx.beginPath(); ctx.arc(6.5, 4.5, 3, 0, Math.PI * 2); ctx.stroke();
                            ctx.beginPath(); ctx.arc(6.5, 15, 6, Math.PI * 1.2, Math.PI * 1.8); ctx.stroke();
                            ctx.restore();
                        }
                        onSChanged: requestPaint()
                    }
                    Text {
                        text: root.currentUser
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(12 * root.sf)
                        color: Tokens.textPrimary; font.weight: Font.Medium
                    }
                }
            }

            Rectangle { width: parent.width; height: 1; color: Tokens.borderSubtle }

            Rectangle {
                width: parent.width; height: Math.round(34 * root.sf)
                radius: root.radiusSm
                color: logoutMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.06) : "transparent"

                Row {
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: parent.left; anchors.leftMargin: Math.round(8 * root.sf)
                    spacing: Math.round(8 * root.sf)

                    Canvas {
                        width: Math.round(13 * root.sf); height: Math.round(13 * root.sf)
                        anchors.verticalCenter: parent.verticalCenter
                        property real s: root.sf
                        onPaint: {
                            var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                            ctx.save(); ctx.scale(s, s);
                            ctx.strokeStyle = "#999"; ctx.lineWidth = 1.2;
                            ctx.beginPath();
                            ctx.moveTo(5, 1); ctx.lineTo(1, 1); ctx.lineTo(1, 12);
                            ctx.lineTo(5, 12); ctx.stroke();
                            ctx.beginPath(); ctx.moveTo(5, 6.5); ctx.lineTo(12, 6.5); ctx.stroke();
                            ctx.beginPath(); ctx.moveTo(9, 3.5); ctx.lineTo(12, 6.5); ctx.lineTo(9, 9.5); ctx.stroke();
                            ctx.restore();
                        }
                        onSChanged: requestPaint()
                    }
                    Text {
                        text: "Sign Out"
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(12 * root.sf)
                        color: Tokens.textSecondary
                    }
                }

                MouseArea { id: logoutMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { userMenu.visible = false; root.doLogout(); } }
            }
        }
    }

    function openApp(appId, title, icon) {
        userMenu.visible = false;
        owPanelVisible = false;
        for (var i = 0; i < root.openWindows.length; i++) {
            if (root.openWindows[i].appId === appId) return;
        }
        var wins = root.openWindows.slice();
        wins.push({ appId: appId, title: title, icon: icon });
        root.openWindows = wins;
    }
}
