import QtQuick
import QtQuick.Controls
import "." // Tokens singleton — mandatory for Tokens.X access

// LoginScreen — pantalla de acceso al SO.
// Un único usuario (single-device): campo de nombre read-only + contraseña.
// Lógica: doLogin() → sysManager.authenticateAsync / authenticate → proceedAfterAuth().
// Residuo WhaleOS eliminado: whale_logo.png, colores hex hardcoded → Tokens.
Rectangle {
    id: loginScreen
    anchors.fill: parent
    color: Tokens.bgVoid

    property bool loginBusy: false

    // ── Background: nebula sutil (ámbar sobre oscuro, marca Sereno) ──
    Rectangle {
        anchors.fill: parent
        color: Tokens.bgVoid
    }
    Rectangle {
        x: parent.width * 0.15; y: parent.height * 0.0
        width: parent.width * 0.7; height: parent.height * 0.5
        radius: width / 2; opacity: 0.10; rotation: -5
        color: Tokens.accentBase
    }
    Rectangle {
        x: parent.width * 0.2; y: parent.height * 0.1
        width: parent.width * 0.65; height: parent.height * 0.55
        radius: width / 2; opacity: 0.07; rotation: 10
        color: Tokens.accentHover
    }
    Canvas {
        anchors.fill: parent; opacity: 0.35
        onPaint: {
            var ctx = getContext("2d");
            var seed = 42;
            function rand() { seed = (seed * 16807 + 0) % 2147483647; return seed / 2147483647; }
            for (var i = 0; i < 90; i++) {
                var sx = rand() * width; var sy = rand() * height;
                var sr = rand() * 1.2 + 0.3; var so = rand() * 0.5 + 0.1;
                ctx.beginPath(); ctx.fillStyle = "rgba(255, 255, 255, " + so + ")";
                ctx.arc(sx, sy, sr, 0, Math.PI * 2); ctx.fill();
            }
        }
    }

    // ── Clock (top center) ──
    Column {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: Math.round(50 * root.sf)
        spacing: Math.round(2 * root.sf)
        opacity: 0

        Text {
            id: loginClock
            anchors.horizontalCenter: parent.horizontalCenter
            text: Qt.formatTime(new Date(), "h:mm")
            font.family:      Tokens.fontDisplay
            font.pixelSize:   Math.round(52 * root.sf)
            font.weight:      Font.Light
            font.letterSpacing: Math.round(4 * root.sf)
            color: Tokens.textPrimary
        }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: Qt.formatDate(new Date(), "dddd, MMMM d")
            font.family:    Tokens.fontBody
            font.pixelSize: Math.round(13 * root.sf)
            color: Tokens.textMuted
            font.letterSpacing: Math.round(1 * root.sf)
        }

        Timer {
            interval: 1000; running: true; repeat: true
            onTriggered: loginClock.text = Qt.formatTime(new Date(), "h:mm")
        }

        Component.onCompleted: fadeInClock.start()
        NumberAnimation on opacity {
            id: fadeInClock; to: 1.0; duration: 800
            easing.type: Easing.OutCubic
        }
    }

    // ── Center content ──
    Column {
        id: centerContent
        anchors.centerIn: parent
        spacing: Math.round(20 * root.sf)
        opacity: 0

        // Logo orb — marca Lumen (reemplaza whale_logo.png eliminado)
        Item {
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.round(120 * root.sf); height: Math.round(120 * root.sf)

            Rectangle {
                anchors.centerIn: parent
                width: Math.round(110 * root.sf); height: width; radius: width / 2
                color: "transparent"
                border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.14)
                border.width: Math.round(1 * root.sf)

                Rectangle {
                    width: Math.round(4 * root.sf); height: Math.round(4 * root.sf)
                    radius: width / 2; color: Tokens.accentBase
                    x: parent.width / 2 - width / 2
                    y: -height / 2
                    opacity: 0.8
                }
            }

            Rectangle {
                anchors.centerIn: parent
                width: Math.round(90 * root.sf); height: width; radius: width / 2
                color: "transparent"
                border.color: Qt.rgba(Tokens.infoBase.r, Tokens.infoBase.g, Tokens.infoBase.b, 0.10)
                border.width: Math.round(1 * root.sf)
            }

            // Inner glass circle with "L" wordmark
            Rectangle {
                anchors.centerIn: parent
                width: Math.round(74 * root.sf); height: width; radius: width / 2
                color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.7)
                border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.18)
                border.width: 1

                Text {
                    anchors.centerIn: parent
                    text: "L"
                    font.family:    Tokens.fontDisplay
                    font.pixelSize: Math.round(28 * root.sf)
                    font.weight:    Font.Bold
                    color: Tokens.accentBase
                }
            }
        }

        // OS name
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "LUMEN OS"
            font.family:      Tokens.fontDisplay
            font.pixelSize:   Math.round(13 * root.sf)
            font.weight:      Font.Medium
            font.letterSpacing: Math.round(3 * root.sf)
            color: Tokens.textMuted
        }

        Item { width: 1; height: Math.round(8 * root.sf) }

        // ── Username display (read-only — single-device, one account) ──
        // Not an editable input: show as a styled pill label matching the input style.
        Rectangle {
            id: userDisplayField
            // Expose .text for doLogin() to read
            property string text: (typeof sysManager !== "undefined" && sysManager.displayName)
                ? sysManager.displayName()
                : "lumen"

            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.round(280 * root.sf)
            height: Math.round(38 * root.sf)
            radius: Math.round(Tokens.radiusMd * root.sf)
            color: Tokens.bgElevated
            border.width: 1
            border.color: Tokens.borderSubtle

            Row {
                anchors.fill: parent
                anchors.leftMargin:  Math.round(Tokens.spMd * root.sf)
                anchors.rightMargin: Math.round(Tokens.spMd * root.sf)
                spacing: Math.round(Tokens.spSm * root.sf)

                // User icon (simple circle + head silhouette via text glyph)
                Text {
                    text: "◎"
                    font.pixelSize:  Math.round(14 * root.sf)
                    color: Tokens.textMuted
                    anchors.verticalCenter: parent.verticalCenter
                }

                Text {
                    text: userDisplayField.text
                    font.family:    Tokens.fontBody
                    font.pixelSize: Math.round(13 * root.sf)
                    color: Tokens.textSecondary
                    anchors.verticalCenter: parent.verticalCenter
                    elide: Text.ElideRight
                    width: parent.width - Math.round(22 * root.sf)
                }
            }
        }

        // ── Password field ──
        LumenInput {
            id: passInput
            sf: root.sf
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.round(280 * root.sf)
            placeholder: "Contraseña"
            password: true
            onAccepted: doLogin()
            Component.onCompleted: forceActiveFocus()
        }

        // ── Submit button ──
        LumenButton {
            id: loginBtn
            sf: root.sf
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.round(280 * root.sf)
            variant: "primary"
            label: "Entrar"
            loading: loginBusy
            onClicked: doLogin()
        }

        // Status / error
        Text {
            id: errorText
            anchors.horizontalCenter: parent.horizontalCenter
            text: loginBusy ? "Autenticando…" : ""
            font.family:    Tokens.fontBody
            font.pixelSize: Math.round(11 * root.sf)
            color: loginBusy ? Tokens.textMuted : Tokens.dangerBase
            visible: text !== ""
        }

        Component.onCompleted: fadeInCenter.start()
        NumberAnimation on opacity {
            id: fadeInCenter; to: 1.0; duration: 1000
            easing.type: Easing.OutCubic
        }
    }

    // ── Power buttons (bottom-right) ──
    Row {
        anchors.bottom: parent.bottom; anchors.right: parent.right
        anchors.bottomMargin: Math.round(18 * root.sf)
        anchors.rightMargin:  Math.round(18 * root.sf)
        spacing: Math.round(10 * root.sf)
        opacity: 0
        Component.onCompleted: fadeInPower.start()
        NumberAnimation on opacity {
            id: fadeInPower; to: 1.0; duration: 1200; easing.type: Easing.OutCubic
        }

        // Restart
        Rectangle {
            width: Math.round(34 * root.sf); height: Math.round(34 * root.sf)
            radius: width / 2
            color: loginRestartMa.containsMouse ? Qt.rgba(1, 1, 1, 0.10) : "transparent"

            Behavior on color {
                enabled: !Tokens.reduceMotion
                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
            }

            Canvas {
                anchors.centerIn: parent
                width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                property real s: root.sf; property bool hov: loginRestartMa.containsMouse
                onPaint: {
                    var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                    ctx.save(); ctx.scale(s, s);
                    ctx.strokeStyle = hov ? "#ff9100" : "rgba(255,255,255,0.35)";
                    ctx.lineWidth = 1.4; ctx.lineCap = "round";
                    ctx.beginPath(); ctx.arc(7, 7, 4.5, -0.5, Math.PI * 1.5); ctx.stroke();
                    ctx.fillStyle = ctx.strokeStyle;
                    ctx.beginPath(); ctx.moveTo(7, 1.5); ctx.lineTo(10, 3.5); ctx.lineTo(7, 5.5); ctx.fill();
                    ctx.restore();
                }
                onHovChanged: requestPaint(); onSChanged: requestPaint()
            }
            MouseArea {
                id: loginRestartMa; anchors.fill: parent; hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: sysManager.runCommandAsync("systemctl reboot", "")
            }
        }

        // Shut down
        Rectangle {
            width: Math.round(34 * root.sf); height: Math.round(34 * root.sf)
            radius: width / 2
            color: loginShutdownMa.containsMouse ? Qt.rgba(1, 1, 1, 0.10) : "transparent"

            Behavior on color {
                enabled: !Tokens.reduceMotion
                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
            }

            Canvas {
                anchors.centerIn: parent
                width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                property real s: root.sf; property bool hov: loginShutdownMa.containsMouse
                onPaint: {
                    var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                    ctx.save(); ctx.scale(s, s);
                    ctx.strokeStyle = hov ? "#ff1744" : "rgba(255,255,255,0.35)";
                    ctx.lineWidth = 1.6; ctx.lineCap = "round";
                    ctx.beginPath(); ctx.moveTo(7, 1.5); ctx.lineTo(7, 6); ctx.stroke();
                    ctx.beginPath(); ctx.arc(7, 7, 4.5, -1.2, Math.PI + 1.2); ctx.stroke();
                    ctx.restore();
                }
                onHovChanged: requestPaint(); onSChanged: requestPaint()
            }
            MouseArea {
                id: loginShutdownMa; anchors.fill: parent; hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: sysManager.runCommandAsync("systemctl poweroff", "")
            }
        }
    }

    // ════════════════════════════════════════
    // Auth logic — untouched
    // ════════════════════════════════════════

    Timer {
        id: xhrTimeout; interval: 5000; running: false; repeat: false
        property string pendingUser: ""
        onTriggered: { if (loginBusy) { loginBusy = false; root.onLoginSuccess(pendingUser, "system"); } }
    }

    Connections {
        target: sysManager
        function onAuthResult(success) {
            if (!loginBusy) return;
            if (!success) { loginBusy = false; errorText.text = "Contraseña incorrecta"; return; }
            proceedAfterAuth();
        }
    }

    function doLogin() {
        if (loginBusy) return;
        loginBusy = true; errorText.text = "";
        var hasAsync = (typeof sysManager.authenticateAsync === "function");
        if (hasAsync) {
            sysManager.authenticateAsync(userDisplayField.text, passInput.text);
        } else {
            var ok = sysManager.authenticate(userDisplayField.text, passInput.text);
            if (!ok) { loginBusy = false; errorText.text = "Contraseña incorrecta"; return; }
            proceedAfterAuth();
        }
    }

    function proceedAfterAuth() {
        loginBusy = false;
        var u = (typeof sysManager !== "undefined" && sysManager.loginUser)
            ? sysManager.loginUser()
            : "hermes-user";
        root.onLoginSuccess(u, "system");
    }
}
