import QtQuick
import QtQuick.Controls
import "." // Tokens singleton — mandatory for Tokens.X access

// ── FirstBootWizard ─────────────────────────────────────────────────────────
// Onboarding OBLIGATORIO de primer arranque. Sin esto el SO arrancaba
// passwordless (autologin) y en hardware físico cualquiera entraba. Dos pasos:
//   1) Crear cuenta — usuario + contraseña → stage_account (D-Bus). El root
//      oneshot hermes-account-apply hace chpasswd, fija display-name, DESACTIVA
//      el autologin y escribe el sentinel /var/lib/hermes/account-applied.
//   2) Idioma + teclado → set_locale_keymap (D-Bus) → hermes-locale-apply
//      (localectl) lo aplica al SO.
// Al terminar: el gate de main.qml pasa a Desktop (sesión recién creada).
Item {
    id: wizard
    anchors.fill: parent

    readonly property real sf: (typeof root !== "undefined" && root.sf) ? root.sf : 1.0

    property int step: 0            // 0 = cuenta, 1 = idioma/teclado
    property bool busy: false
    property string errorMsg: ""

    property var locales: [
        { name: "Español",   locale: "es_ES.UTF-8", keymap: "es" },
        { name: "English",   locale: "en_US.UTF-8", keymap: "us" },
        { name: "Français",  locale: "fr_FR.UTF-8", keymap: "fr" },
        { name: "Deutsch",   locale: "de_DE.UTF-8", keymap: "de" },
        { name: "Italiano",  locale: "it_IT.UTF-8", keymap: "it" },
        { name: "Português", locale: "pt_PT.UTF-8", keymap: "pt" }
    ]
    property int localeIndex: 0

    function validUser(u) { return /^[a-z][a-z0-9_-]{0,31}$/.test(u); }

    function submitAccount() {
        errorMsg = "";
        var u = userField.text.trim();
        var p = passField.text;
        if (!validUser(u)) { errorMsg = "Usuario: minúsculas, empieza por letra (a-z, 0-9, _ , -)."; return; }
        if (p.length < 8) { errorMsg = "La contraseña debe tener al menos 8 caracteres."; return; }
        if (p !== confirmField.text) { errorMsg = "Las contraseñas no coinciden."; return; }
        busy = true;
        hermes.call("ob-stage", "stage_account", JSON.stringify({ username: u, password: p }));
    }

    function submitLocale() {
        errorMsg = ""; busy = true;
        var l = locales[localeIndex];
        hermes.call("ob-locale", "set_locale_keymap", JSON.stringify({ locale: l.locale, keymap: l.keymap }));
    }

    Timer {
        id: applyPoll
        interval: 1000; repeat: true; running: false
        property int ticks: 0
        onTriggered: {
            ticks += 1;
            if (sysManager.accountConfigured()) {
                stop();
                if (sysManager.shouldRestartForRemote && sysManager.shouldRestartForRemote()) {
                    sysManager.restartForRemote();
                    return;
                }
                if (typeof root !== "undefined") {
                    root.onboardingDone = true;
                    var u = sysManager.loginUser ? sysManager.loginUser() : "hermes-user";
                    root.onLoginSuccess(u, "system");
                }
            } else if (ticks > 20) {
                stop(); wizard.busy = false;
                wizard.errorMsg = "La cuenta no se aplicó a tiempo. Revisa e inténtalo de nuevo.";
            }
        }
    }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "ob-stage") {
                var r = {}; try { r = JSON.parse(jsonStr || "{}"); } catch (e) {}
                if (ok && r.staged) { wizard.step = 1; wizard.busy = false; }
                else { wizard.busy = false; wizard.errorMsg = wizard._accountError(r.error); }
            } else if (reqId === "ob-locale") {
                applyPoll.ticks = 0; applyPoll.start();
            }
        }
    }

    function _accountError(code) {
        if (code === "already_configured") return "El equipo ya tiene una cuenta configurada.";
        if (code === "invalid_username") return "Usuario no válido.";
        if (code === "invalid_password") return "Contraseña no válida (8–256 caracteres).";
        return "No se pudo crear la cuenta. Inténtalo de nuevo.";
    }

    // ── Background: igual que Desktop.qml ──
    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0;  color: Tokens.bgSurface }
            GradientStop { position: 0.45; color: Tokens.bgVoid    }
            GradientStop { position: 1.0;  color: Tokens.bgSunken  }
        }
    }
    Rectangle {
        x: parent.width * 0.14; y: -parent.height * 0.06
        width: parent.width * 0.74; height: parent.height * 0.52
        radius: width / 2; opacity: 0.10; rotation: -6
        color: Tokens.accentBase
    }
    Rectangle {
        x: parent.width * 0.22; y: parent.height * 0.06
        width: parent.width * 0.62; height: parent.height * 0.55
        radius: width / 2; opacity: 0.08; rotation: 10
        color: Tokens.accentHover
    }
    Canvas {
        anchors.fill: parent; opacity: 0.35
        Component.onCompleted: requestPaint()
        onPaint: {
            var ctx = getContext("2d"); var seed = 137;
            function rand() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
            for (var i = 0; i < 130; i++) {
                var sx = rand() * width, sy = rand() * height, sr = rand() * 1.2 + 0.3, so = rand() * 0.5 + 0.08;
                ctx.beginPath(); ctx.fillStyle = "rgba(255,255,255," + so + ")"; ctx.arc(sx, sy, sr, 0, Math.PI * 2); ctx.fill();
            }
        }
    }

    // Orbe de marca Lumen — detrás de la tarjeta
    Item {
        anchors.centerIn: parent
        width: Math.round(Math.min(parent.width, parent.height) * 0.34); height: width
        opacity: 0.45
        Rectangle {
            anchors.centerIn: parent; width: parent.width; height: width; radius: width / 2
            color: "transparent"
            border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.12)
            border.width: 1
            Rectangle {
                width: 5; height: 5; radius: 2.5
                color: Tokens.accentBase; opacity: 0.7
                x: parent.width / 2 - 2.5; y: -2.5
            }
        }
        Rectangle {
            anchors.centerIn: parent; width: parent.width * 0.78; height: width; radius: width / 2
            color: "transparent"
            border.color: Qt.rgba(Tokens.infoBase.r, Tokens.infoBase.g, Tokens.infoBase.b, 0.08)
            border.width: 1
        }
        Rectangle {
            anchors.centerIn: parent; width: parent.width * 0.6; height: width; radius: width / 2
            color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.45)
            border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.12)
            border.width: 1
        }
    }

    // ── Tarjeta central: LumenCard + entrada animada ──
    LumenCard {
        id: mainCard
        sf: wizard.sf
        elevated: true
        pad: Math.round(28 * wizard.sf)

        width: Math.round(440 * wizard.sf)
        // Altura adaptativa: ajusta al contenido para evitar desbordamiento
        height: cardInnerCol.implicitHeight + Math.round(56 * wizard.sf)
        anchors.centerIn: parent

        // Entrada: scale 0.96 → 1.0 + fade (gated en reduceMotion)
        scale: 0.96
        opacity: 0
        Component.onCompleted: cardEntrance.start()
        ParallelAnimation {
            id: cardEntrance
            running: false
            NumberAnimation {
                target: mainCard; property: "scale"
                from: 0.96; to: 1.0
                duration: !Tokens.reduceMotion ? Tokens.durBase : 0
                easing.type: Easing.OutCubic
            }
            NumberAnimation {
                target: mainCard; property: "opacity"
                from: 0.0; to: 1.0
                duration: !Tokens.reduceMotion ? Tokens.durBase : 0
                easing.type: Easing.OutCubic
            }
        }

        Column {
            id: cardInnerCol
            width: parent.width
            spacing: Math.round(12 * wizard.sf)

            // Paso de indicador
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "LUMEN OS"
                font.family:      Tokens.fontDisplay
                font.pixelSize:   Math.round(11 * wizard.sf)
                font.weight:      Font.Medium
                font.letterSpacing: Math.round(2.5 * wizard.sf)
                color: Tokens.textMuted
            }

            // Título del paso (Space Grotesk — display typography)
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: step === 0 ? "Crea tu cuenta" : "Idioma y teclado"
                font.family:    Tokens.fontDisplay
                font.pixelSize: Math.round(22 * wizard.sf)
                font.weight:    Font.DemiBold
                color: Tokens.textPrimary

                Behavior on text {
                    enabled: !Tokens.reduceMotion
                    // No SequentialAnimation on text — just let it swap; slide is on
                    // the step column below. Text swap is instant and correct.
                }
            }

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: step === 0
                    ? "Protege tu equipo: nadie entra sin tu usuario y contraseña."
                    : "Elige el idioma y la distribución del teclado del sistema."
                font.family:    Tokens.fontBody
                font.pixelSize: Math.round(12 * wizard.sf)
                color: Tokens.textSecondary
            }

            Item { width: 1; height: Math.round(4 * wizard.sf) }

            // ── Paso 0: cuenta ──
            Column {
                width: parent.width
                spacing: Math.round(10 * wizard.sf)
                visible: step === 0

                LumenInput {
                    id: userField
                    sf: wizard.sf
                    width: parent.width
                    placeholder: "Usuario (p.ej. lumen)"
                    onAccepted: passField.forceActiveFocus()
                }

                LumenInput {
                    id: passField
                    sf: wizard.sf
                    width: parent.width
                    placeholder: "Contraseña (mín. 8)"
                    password: true
                    onAccepted: confirmField.forceActiveFocus()
                }

                LumenInput {
                    id: confirmField
                    sf: wizard.sf
                    width: parent.width
                    placeholder: "Repite la contraseña"
                    password: true
                    onAccepted: wizard.submitAccount()
                }
            }

            // ── Paso 1: idioma/teclado ──
            Column {
                width: parent.width
                spacing: Math.round(6 * wizard.sf)
                visible: step === 1

                Repeater {
                    model: wizard.locales
                    delegate: Rectangle {
                        width: parent.width
                        height: Math.round(44 * wizard.sf)
                        radius: Math.round(Tokens.radiusSm * wizard.sf)
                        color: index === wizard.localeIndex
                            ? Tokens.accentSubtle
                            : Tokens.bgElevated
                        border.color: index === wizard.localeIndex
                            ? Tokens.accentBase
                            : Tokens.borderSubtle
                        border.width: 1

                        Behavior on color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }
                        Behavior on border.color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }

                        Row {
                            anchors.left: parent.left
                            anchors.leftMargin: Math.round(Tokens.spLg * wizard.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: Math.round(Tokens.spSm * wizard.sf)

                            Text {
                                text: modelData.name
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(13 * wizard.sf)
                                font.weight:    Font.Medium
                                color: index === wizard.localeIndex ? Tokens.textPrimary : Tokens.textSecondary
                            }
                            Text {
                                text: "· teclado " + modelData.keymap
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(11 * wizard.sf)
                                color: Tokens.textMuted
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }

                        // Accent bar on left edge when selected
                        Rectangle {
                            visible: index === wizard.localeIndex
                            width: Math.round(3 * wizard.sf)
                            anchors.top:         parent.top
                            anchors.bottom:      parent.bottom
                            anchors.left:        parent.left
                            anchors.topMargin:   Math.round(6 * wizard.sf)
                            anchors.bottomMargin: Math.round(6 * wizard.sf)
                            radius: width / 2
                            color: Tokens.accentBase
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: wizard.localeIndex = index
                        }
                    }
                }
            }

            // Error message
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                visible: wizard.errorMsg.length > 0
                text: wizard.errorMsg
                font.family:    Tokens.fontBody
                font.pixelSize: Math.round(11 * wizard.sf)
                color: Tokens.dangerBase
            }

            Item { width: 1; height: Math.round(2 * wizard.sf) }

            // Primary action button
            LumenButton {
                width: parent.width
                sf: wizard.sf
                variant: "primary"
                loading: wizard.busy
                label: step === 0 ? "Continuar" : "Finalizar y entrar"
                onClicked: step === 0 ? wizard.submitAccount() : wizard.submitLocale()
            }
        }
    }
}
