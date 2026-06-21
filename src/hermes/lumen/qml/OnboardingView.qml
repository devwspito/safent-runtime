import QtQuick
import QtQuick.Layouts
import "."

// Lumen — first-run onboarding wizard.
// Steps: 0=welcome  1=language  2=account  3=done
//
// Model is de-gated: the AI provider is NOT required during onboarding.
// The wizard collects account + locale only. The service is connected later
// via the "Conectar un servicio" deep-link or the ConnectAI view.
//
// Progress dots: 3 (Idioma · Cuenta · Listo). Welcome has no dot.
//
// Design: macOS Welcome / Ubuntu Yaru style — large light title, real Lucide
// icons, neutral dark palette, intentional 8pt-grid spacing, one focal
// point per step. NO emoji, NO gradient orbs, NO blurEnabled/MultiEffect.
Item {
    id: onboarding

    signal finished()

    property int step: 0
    property var shell: null

    // ── Background — neutral dark, no color tint ──────────────────────────
    Rectangle {
        anchors.fill: parent
        color: Theme.bg0
    }

    // Scrollable content anchored fill, from top
    Flickable {
        id: outerFlick
        anchors.fill: parent
        contentHeight: wizardCol.height + 72 + 64
        clip: true
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds

        WheelScroll { target: outerFlick }

        ColumnLayout {
            id: wizardCol
            // Clamp: fill screen minus 64px gutters, max 560px
            width: Math.min(onboarding.width - 64, 560)
            anchors.horizontalCenter: parent.horizontalCenter
            y: 72
            spacing: 0

            // ── Identity mark — Lumen aperture symbol ─────────────────────
            Item {
                Layout.alignment: Qt.AlignHCenter
                width: 72; height: 72

                // Outer glow — barely-there, neutral
                Rectangle {
                    anchors.centerIn: parent
                    width: 96; height: 96; radius: 48
                    color: Theme.alpha(Theme.accentBright, 0.06)
                }

                // The SVG mark
                Image {
                    anchors.centerIn: parent
                    width: 72; height: 72
                    source: Theme.mode === "light" ? "icons/lumen-mark-light.svg" : "icons/lumen-mark.svg"
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    mipmap: true
                }
            }

            Item { width: 1; height: 32 }

            // ── Step title ────────────────────────────────────────────────
            Text {
                Layout.alignment: Qt.AlignHCenter
                text: onboarding.step === 0 ? "Bienvenido a Lumen"
                    : onboarding.step === 1 ? "Elige tu idioma"
                    : onboarding.step === 2 ? "Crea tu cuenta"
                    : "Todo listo"
                color: Theme.ink
                font.family: Theme.font
                font.pixelSize: 40
                font.weight: 300
                font.letterSpacing: -0.5
            }

            Item { width: 1; height: 10 }

            // ── Subtitle ──────────────────────────────────────────────────
            Text {
                Layout.alignment: onboarding.step === 2 ? Qt.AlignLeft : Qt.AlignHCenter
                text: onboarding.step === 0
                    ? "Tu asistente personal. Trabaja por ti, aprende contigo\ny protege tu privacidad."
                    : onboarding.step === 1
                    ? "Lumen usará este idioma en su interfaz y respuestas."
                    : onboarding.step === 2
                    ? "Elige un nombre de usuario y una contraseña\npara proteger tu sesión local."
                    : "Lumen está listo para empezar."
                color: Theme.ink3
                font.family: Theme.font
                font.pixelSize: 16
                font.weight: Font.Normal
                lineHeight: 1.6
                horizontalAlignment: onboarding.step === 2 ? Text.AlignLeft : Text.AlignHCenter
            }

            Item { width: 1; height: 36 }

            // Single Loader — one step active at a time
            Loader {
                id: stepLoader
                Layout.fillWidth: true
                height: item ? item.implicitHeight : 0
                sourceComponent: onboarding.step === 0 ? welcomeStep
                              :  onboarding.step === 1 ? languageStep
                              :  onboarding.step === 2 ? accountStep
                              :                         doneStep
            }

            Item { width: 1; height: 32 }

            // ── Step indicator — 3 dots (Language · Account · Done) ───────
            // Welcome (step 0) shows no dots — it's the splash screen.
            Row {
                Layout.alignment: Qt.AlignHCenter; spacing: 6
                visible: onboarding.step > 0

                Repeater {
                    model: 3
                    // index 0 = Language (step 1), 1 = Account (step 2), 2 = Done (step 3)
                    Rectangle {
                        property int dotStep: index + 1
                        width: dotStep === onboarding.step ? 20 : 4
                        height: 4; radius: 2
                        color: dotStep === onboarding.step ? Theme.accentBright : Theme.line2

                        Behavior on width { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
                        Behavior on color { ColorAnimation { duration: 200 } }
                    }
                }
            }

            Item { width: 1; height: 40 }
        }
    }

    // ── Step 0: Welcome ───────────────────────────────────────────────────
    Component {
        id: welcomeStep

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: 10

            // Three feature rows — real Lucide icons, generous height
            Repeater {
                model: [
                    { icon: "icons/shield-check-accent.svg", title: "Privacidad local",  desc: "Todo corre en tu máquina. Tus datos no salen sin tu permiso." },
                    { icon: "icons/sparkles-accent.svg",     title: "IA que aprende",     desc: "Enseña skills y tu asistente las recuerda para siempre." },
                    { icon: "icons/activity-accent.svg",     title: "Siempre activo",      desc: "Trabaja en segundo plano mientras tú haces otra cosa." }
                ]

                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 72

                    // Subtle static shadow — low-opacity offset rectangle (no blur)
                    Rectangle {
                        anchors { left: parent.left; right: parent.right; top: parent.top }
                        anchors.topMargin: 3
                        anchors.leftMargin: 2; anchors.rightMargin: -2
                        height: parent.height; radius: Theme.rLg
                        color: "#000000"; opacity: 0.18
                    }

                    Rectangle {
                        anchors.fill: parent; radius: Theme.rLg
                        color: Theme.card
                        border.color: Theme.line; border.width: 1

                        // Top hairline highlight — 1px inner edge
                        Rectangle {
                            anchors { top: parent.top; left: parent.left; right: parent.right }
                            anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                            height: 1; radius: Theme.rLg - 1
                            color: "#FFFFFF"; opacity: 0.05
                        }

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp3; rightMargin: Theme.sp3 }
                            spacing: Theme.sp2

                            // Icon container — accent-tinted square
                            Rectangle {
                                width: 40; height: 40; radius: Theme.rSm
                                color: Theme.alpha(Theme.accent, 0.14)
                                border.color: Theme.alpha(Theme.accentBright, 0.18); border.width: 1

                                Image {
                                    anchors.centerIn: parent
                                    width: 20; height: 20
                                    source: Theme.accentIcon(modelData.icon)
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true; mipmap: true
                                }
                            }

                            ColumnLayout {
                                spacing: 3
                                Layout.fillWidth: true

                                Text {
                                    text: modelData.title
                                    color: Theme.ink
                                    font.family: Theme.font
                                    font.pixelSize: 14
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    text: modelData.desc
                                    color: Theme.ink3
                                    font.family: Theme.font
                                    font.pixelSize: 13
                                    lineHeight: 1.45
                                    Layout.fillWidth: true
                                    wrapMode: Text.WordWrap
                                }
                            }
                        }
                    }
                }
            }

            Item { height: 8; width: 1 }

            // Primary CTA
            Item {
                Layout.fillWidth: true; Layout.preferredHeight: 52

                Rectangle {
                    anchors.fill: parent; radius: Theme.rLg; color: Theme.accent

                    RowLayout {
                        anchors.centerIn: parent; spacing: 8

                        Text {
                            text: "Empezar"
                            color: "#FFFFFF"
                            font.family: Theme.font
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                        }

                        Image {
                            width: 16; height: 16
                            source: "icons/arrow-right-white.svg"
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: onboarding.step = 1
                    }
                }
            }
        }
    }

    // ── Step 1: Language ──────────────────────────────────────────────────
    Component {
        id: languageStep

        ColumnLayout {
            id: langForm
            width: parent ? parent.width : 0
            spacing: Theme.sp1

            property string selectedLocale: "es_ES"

            Repeater {
                model: [
                    { label: "Español",  locale: "es_ES",  flag: "ES" },
                    { label: "English",  locale: "en_US",  flag: "EN" },
                    { label: "Français", locale: "fr_FR",  flag: "FR" },
                    { label: "Deutsch",  locale: "de_DE",  flag: "DE" },
                    { label: "Italiano", locale: "it_IT",  flag: "IT" },
                    { label: "Português",locale: "pt_PT",  flag: "PT" }
                ]

                Item {
                    Layout.fillWidth: true; Layout.preferredHeight: 52

                    property bool isSelected: langForm.selectedLocale === modelData.locale

                    Rectangle {
                        anchors.fill: parent; radius: Theme.rLg
                        color: parent.isSelected
                               ? Theme.alpha(Theme.accent, 0.12)
                               : (langHover.containsMouse ? Theme.surface2 : Theme.card)
                        border.color: parent.isSelected
                                      ? Theme.alpha(Theme.accentBright, 0.40)
                                      : (langHover.containsMouse ? Theme.line2 : Theme.line)
                        border.width: parent.isSelected ? 1.5 : 1
                        Behavior on color { ColorAnimation { duration: 120 } }
                        Behavior on border.color { ColorAnimation { duration: 120 } }

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                            spacing: Theme.sp2

                            // Language code badge
                            Rectangle {
                                width: 36; height: 28; radius: Theme.rSm - 2
                                color: parent.parent.parent.isSelected
                                       ? Theme.alpha(Theme.accent, 0.20)
                                       : Theme.alpha(Theme.accent, 0.10)
                                border.color: Theme.alpha(Theme.accentBright, 0.16); border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: modelData.flag
                                    color: parent.parent.parent.parent.isSelected
                                           ? Theme.accentBright : Theme.ink3
                                    font.family: Theme.font
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                            }

                            Text {
                                text: modelData.label
                                color: Theme.ink
                                font.family: Theme.font
                                font.pixelSize: Theme.tsBody
                                font.weight: Font.Medium
                                Layout.fillWidth: true
                            }

                            // Check mark for selected
                            Image {
                                visible: langForm.selectedLocale === modelData.locale
                                width: 16; height: 16
                                source: "icons/check-ok.svg"
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                        }

                        MouseArea {
                            id: langHover
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: langForm.selectedLocale = modelData.locale
                        }
                    }
                }
            }

            Item { height: 8; width: 1 }

            // Continue button
            Item {
                Layout.fillWidth: true; Layout.preferredHeight: 52

                Rectangle {
                    anchors.fill: parent; radius: Theme.rLg; color: Theme.accent

                    RowLayout {
                        anchors.centerIn: parent; spacing: 8

                        Text {
                            text: "Continuar"
                            color: "#FFFFFF"
                            font.family: Theme.font
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                        }

                        Image {
                            width: 16; height: 16
                            source: "icons/arrow-right-white.svg"
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            backend.setLocale(langForm.selectedLocale)
                            onboarding.step = 2
                        }
                    }
                }
            }
        }
    }

    // ── Step 2: Account creation ──────────────────────────────────────────
    Component {
        id: accountStep

        ColumnLayout {
            id: accountForm
            width: parent ? parent.width : 0
            spacing: Theme.sp2

            property string errorMsg: ""
            property bool submitting: false

            Connections {
                target: backend
                function onAccountCreated(success, msg) {
                    accountForm.submitting = false
                    if (success) { onboarding.step = 3 }
                    else { accountForm.errorMsg = msg }
                }
            }

            // Username field
            ColumnLayout {
                Layout.fillWidth: true; spacing: 6

                RowLayout {
                    spacing: 6
                    Image {
                        width: 14; height: 14
                        source: Theme.dimIcon("icons/user-dim.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                    Text {
                        text: "Nombre de usuario"
                        color: Theme.ink2
                        font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium
                    }
                }

                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 46; radius: Theme.rMd
                    color: Theme.card2
                    border.color: usernameInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line
                    border.width: 1
                    Behavior on border.color { ColorAnimation { duration: 150 } }

                    TextInput {
                        id: usernameInput
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                        verticalAlignment: Text.AlignVCenter
                        color: Theme.ink
                        font.family: Theme.font; font.pixelSize: Theme.tsBody; clip: true
                        inputMethodHints: Qt.ImhNoAutoUppercase | Qt.ImhNoPredictiveText
                        onAccepted: passwordInput.forceActiveFocus()
                    }
                    Text {
                        visible: usernameInput.text.length === 0
                        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; leftMargin: Theme.sp2 }
                        text: "p.ej. alex"
                        color: Theme.ink4; font.family: Theme.font; font.pixelSize: Theme.tsBody
                    }
                }
            }

            // Password field
            ColumnLayout {
                Layout.fillWidth: true; spacing: 6

                RowLayout {
                    spacing: 6
                    Image {
                        width: 14; height: 14
                        source: Theme.dimIcon("icons/lock-dim.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                    Text {
                        text: "Contraseña"
                        color: Theme.ink2
                        font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium
                    }
                }

                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 46; radius: Theme.rMd
                    color: Theme.card2
                    border.color: passwordInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line
                    border.width: 1
                    Behavior on border.color { ColorAnimation { duration: 150 } }

                    RowLayout {
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: 8 }
                        spacing: Theme.sp1

                        TextInput {
                            id: passwordInput
                            Layout.fillWidth: true
                            verticalAlignment: Text.AlignVCenter
                            color: Theme.ink
                            font.family: Theme.font; font.pixelSize: Theme.tsBody
                            echoMode: eyePassBtn.revealed ? TextInput.Normal : TextInput.Password
                            clip: true
                            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoAutoUppercase
                            onAccepted: confirmInput.forceActiveFocus()
                        }

                        // Eye toggle — Lucide icon
                        Rectangle {
                            id: eyePassBtn
                            property bool revealed: false
                            width: 32; height: 32; radius: Theme.rSm - 2
                            color: eyePassHover.containsMouse ? Theme.alpha(Theme.accentBright, 0.10) : "transparent"
                            Behavior on color { ColorAnimation { duration: 120 } }

                            Image {
                                anchors.centerIn: parent
                                width: 16; height: 16
                                source: eyePassBtn.revealed ? "icons/eye-off-dim.svg" : "icons/eye-dim.svg"
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                            MouseArea {
                                id: eyePassHover
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                hoverEnabled: true
                                onClicked: eyePassBtn.revealed = !eyePassBtn.revealed
                            }
                        }
                    }

                    Text {
                        visible: passwordInput.text.length === 0
                        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; leftMargin: Theme.sp2 }
                        text: "mínimo 8 caracteres"
                        color: Theme.ink4; font.family: Theme.font; font.pixelSize: Theme.tsBody
                    }
                }
            }

            // Confirm password field
            ColumnLayout {
                Layout.fillWidth: true; spacing: 6

                RowLayout {
                    spacing: 6
                    Image {
                        width: 14; height: 14
                        source: Theme.dimIcon("icons/key-dim.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                    Text {
                        text: "Repite la contraseña"
                        color: Theme.ink2
                        font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium
                    }
                }

                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 46; radius: Theme.rMd
                    color: Theme.card2
                    border.color: confirmInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line
                    border.width: 1
                    Behavior on border.color { ColorAnimation { duration: 150 } }

                    TextInput {
                        id: confirmInput
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                        verticalAlignment: Text.AlignVCenter
                        color: Theme.ink
                        font.family: Theme.font; font.pixelSize: Theme.tsBody
                        echoMode: eyePassBtn.revealed ? TextInput.Normal : TextInput.Password
                        clip: true
                        inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoAutoUppercase
                        onAccepted: { if (accountForm._canSubmit()) accountForm._submit() }
                    }
                    Text {
                        visible: confirmInput.text.length === 0
                        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; leftMargin: Theme.sp2 }
                        text: "repite la contraseña"
                        color: Theme.ink4; font.family: Theme.font; font.pixelSize: Theme.tsBody
                    }
                }
            }

            // Password mismatch hint
            Rectangle {
                Layout.fillWidth: true
                visible: confirmInput.text.length > 0 && passwordInput.text !== confirmInput.text
                height: visible ? 36 : 0
                radius: Theme.rSm
                color: Theme.alpha(Theme.warn, 0.08)
                border.color: Theme.alpha(Theme.warn, 0.24); border.width: 1

                RowLayout {
                    anchors { fill: parent; leftMargin: 10; rightMargin: 10 }
                    spacing: 6

                    Image {
                        width: 14; height: 14
                        source: "icons/alert-circle-warn.svg"
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                    Text {
                        text: "Las contraseñas no coinciden"
                        color: Theme.warn; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                        Layout.fillWidth: true
                    }
                }
            }

            // Error card
            Rectangle {
                Layout.fillWidth: true
                visible: accountForm.errorMsg.length > 0
                radius: Theme.rMd
                color: Theme.alpha(Theme.warn, 0.07)
                border.color: Theme.alpha(Theme.warn, 0.26); border.width: 1
                height: errDetail.height + Theme.sp3

                Text {
                    id: errDetail
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.sp2; topMargin: 12 }
                    text: accountForm.errorMsg
                    color: Theme.ink2; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                    wrapMode: Text.WordWrap; lineHeight: 1.45
                }
            }

            // ── Avanzado — collapsible Disclosure ─────────────────────────
            Disclosure {
                id: advancedDisclosure
                Layout.fillWidth: true
                label: "Avanzado"
                contentComponent: advancedContent
            }

            Component {
                id: advancedContent

                ColumnLayout {
                    // Width bound to the Disclosure item's width via its Loader anchor
                    width: parent ? parent.width : advancedDisclosure.width
                    spacing: Theme.sp2

                    Item { height: 4; width: 1 }

                    // System profile
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 6

                        RowLayout {
                            spacing: 6
                            Image {
                                width: 14; height: 14
                                source: Theme.dimIcon("icons/settings-dim.svg")
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                            Text {
                                text: "Perfil del sistema"
                                color: Theme.ink2
                                font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true; Layout.preferredHeight: 44; radius: Theme.rMd
                            color: Theme.card2
                            border.color: profileInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line
                            border.width: 1
                            Behavior on border.color { ColorAnimation { duration: 150 } }

                            TextInput {
                                id: profileInput
                                anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                                verticalAlignment: Text.AlignVCenter
                                color: Theme.ink
                                font.family: Theme.mono; font.pixelSize: Theme.tsCaption + 1; clip: true
                                text: "personal_desktop"
                                inputMethodHints: Qt.ImhNoAutoUppercase | Qt.ImhNoPredictiveText
                            }
                        }
                    }

                    // Org URL
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 6

                        RowLayout {
                            spacing: 6
                            Image {
                                width: 14; height: 14
                                source: Theme.dimIcon("icons/globe-dim.svg")
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                            Text {
                                text: "Vínculo de organización (URL)"
                                color: Theme.ink2
                                font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true; Layout.preferredHeight: 44; radius: Theme.rMd
                            color: Theme.card2
                            border.color: orgUrlInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line
                            border.width: 1
                            Behavior on border.color { ColorAnimation { duration: 150 } }

                            TextInput {
                                id: orgUrlInput
                                anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                                verticalAlignment: Text.AlignVCenter
                                color: Theme.ink
                                font.family: Theme.mono; font.pixelSize: Theme.tsCaption + 1; clip: true
                                inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoAutoUppercase
                            }
                            Text {
                                visible: orgUrlInput.text.length === 0
                                anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; leftMargin: Theme.sp2 }
                                text: "https://empresa.ejemplo.com"
                                color: Theme.ink4; font.family: Theme.mono; font.pixelSize: Theme.tsCaption + 1
                            }
                        }
                    }

                    // Org token
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 6

                        RowLayout {
                            spacing: 6
                            Image {
                                width: 14; height: 14
                                source: Theme.dimIcon("icons/key-dim.svg")
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                            Text {
                                text: "Token de organización"
                                color: Theme.ink2
                                font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true; Layout.preferredHeight: 44; radius: Theme.rMd
                            color: Theme.card2
                            border.color: orgTokenInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line
                            border.width: 1
                            Behavior on border.color { ColorAnimation { duration: 150 } }

                            TextInput {
                                id: orgTokenInput
                                anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                                verticalAlignment: Text.AlignVCenter
                                color: Theme.ink
                                font.family: Theme.mono; font.pixelSize: Theme.tsCaption + 1
                                echoMode: TextInput.Password; clip: true
                                inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoAutoUppercase
                            }
                            Text {
                                visible: orgTokenInput.text.length === 0
                                anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; leftMargin: Theme.sp2 }
                                text: "opcional"
                                color: Theme.ink4; font.family: Theme.mono; font.pixelSize: Theme.tsCaption + 1
                            }
                        }
                    }

                    Item { height: 4; width: 1 }
                }
            }

            Item { height: 4; width: 1 }

            // Submit button
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 52; radius: Theme.rLg
                color: Theme.accent
                opacity: accountForm._canSubmit() && !accountForm.submitting ? 1.0 : 0.32
                Behavior on opacity { NumberAnimation { duration: 150 } }

                RowLayout {
                    anchors.centerIn: parent; spacing: 8
                    visible: !accountForm.submitting

                    Text {
                        text: "Crear cuenta"
                        color: "#FFFFFF"
                        font.family: Theme.font; font.pixelSize: 15; font.weight: Font.DemiBold
                    }
                    Image {
                        width: 16; height: 16
                        source: "icons/arrow-right-white.svg"
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                }

                // Static loading dots — no animation (VNC safe)
                Row {
                    anchors.centerIn: parent
                    visible: accountForm.submitting
                    spacing: 6
                    Repeater {
                        model: 3
                        Rectangle {
                            width: 6; height: 6; radius: 3
                            color: "#FFFFFF"; opacity: 0.6 + index * 0.2
                        }
                    }
                }

                MouseArea {
                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: { if (accountForm._canSubmit() && !accountForm.submitting) accountForm._submit() }
                }
            }

            function _canSubmit() {
                var user = usernameInput.text.trim()
                var pass = passwordInput.text; var conf = confirmInput.text
                if (user.length === 0) return false
                if (pass.length < 8) return false
                if (pass !== conf) return false
                return true
            }

            function _submit() {
                accountForm.errorMsg = ""; accountForm.submitting = true
                backend.createAccount(usernameInput.text.trim(), passwordInput.text)
            }
        }
    }

    // ── Step 3: Done — finalize + CTA ─────────────────────────────────────
    Component {
        id: doneStep

        ColumnLayout {
            id: doneCol
            width: parent ? parent.width : 0; spacing: Theme.sp3

            // Finalize state machine
            // States: idle → running → done (or partial on error)
            property string finalizeState: "idle"
            property string finalizeNote: ""

            Component.onCompleted: {
                if (doneCol.finalizeState === "idle") doneCol._doFinalize()
            }

            function _doFinalize() {
                doneCol.finalizeState = "running"
                // Best-effort chain — partial failures produce a soft note, not a blocker.
                // Each call is fire-and-forget; we watch onFinalizeStepDone from backend.
                backend.setProfile("personal_desktop")
                backend.setLocale(backend.currentLocale || "es_ES")
                backend.setNetwork("connected")
                backend.setTenant("defer")
                backend.setConsents([])
                backend.reviewServices(true)
                backend.finalizeOnboarding()
            }

            Connections {
                target: backend
                function onFinalizeOnboardingDone(success, partial) {
                    doneCol.finalizeState = "done"
                    if (partial) {
                        doneCol.finalizeNote = "Algunos ajustes se aplicarán al iniciar"
                    }
                }
                // Fallback: if backend doesn't emit the signal within 3s, treat as done
            }

            Timer {
                id: finalizeFallback
                interval: 3000; running: doneCol.finalizeState === "running"; repeat: false
                onTriggered: {
                    if (doneCol.finalizeState === "running") doneCol.finalizeState = "done"
                }
            }

            // ── Success mark — geometric, not emoji checkmark ───────────
            Item {
                Layout.alignment: Qt.AlignHCenter; width: 80; Layout.preferredHeight: 80

                Rectangle {
                    anchors.fill: parent; radius: 40
                    color: Theme.alpha(Theme.ok, 0.10)
                    border.color: Theme.alpha(Theme.ok, 0.28); border.width: 1.5
                }

                Image {
                    anchors.centerIn: parent
                    width: 36; height: 36
                    source: "icons/circle-check-ok.svg"
                    fillMode: Image.PreserveAspectFit
                    smooth: true; mipmap: true
                }

                // One-shot pop animation — loops: 1, no Infinite
                SequentialAnimation on scale {
                    running: onboarding.step === 3
                    loops: 1
                    NumberAnimation { from: 0.7; to: 1.08; duration: 300; easing.type: Easing.OutBack }
                    NumberAnimation { from: 1.08; to: 1.0; duration: 180; easing.type: Easing.InOutQuad }
                }
            }

            // ── Progress bar while finalizing ────────────────────────────
            // Shown during "running" state; plain bar without shimmer animation.
            Rectangle {
                Layout.fillWidth: true
                visible: doneCol.finalizeState === "running"
                height: 4; radius: 2
                color: Theme.alpha(Theme.accent, 0.15)

                Rectangle {
                    id: progressFill
                    height: parent.height; radius: 2
                    color: Theme.accent

                    // Indeterminate: width oscillates once (loops:1, not Infinite)
                    // Under VNC (reduceMotion), it's a static 60% bar.
                    width: Theme.reduceMotion ? parent.width * 0.6 : parent.width * 0.6

                    SequentialAnimation on width {
                        running: !Theme.reduceMotion && doneCol.finalizeState === "running"
                        loops: 1
                        NumberAnimation { from: parent.width * 0.10; to: parent.width * 0.85; duration: 2800; easing.type: Easing.InOutCubic }
                        NumberAnimation { from: parent.width * 0.85; to: parent.width * 1.0;  duration: 400;  easing.type: Easing.OutCubic }
                    }
                }
            }

            Text {
                Layout.alignment: Qt.AlignHCenter
                visible: doneCol.finalizeState === "running"
                text: "Preparando tu entorno…"
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption + 1
            }

            // ── Soft note for partial failures ────────────────────────────
            Rectangle {
                Layout.fillWidth: true
                visible: doneCol.finalizeNote.length > 0
                height: noteRow.height + Theme.sp2
                radius: Theme.rSm
                color: Theme.alpha(Theme.info, 0.07)
                border.color: Theme.alpha(Theme.info, 0.22); border.width: 1

                RowLayout {
                    id: noteRow
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: 10; topMargin: 10 }
                    spacing: 6

                    Image {
                        width: 13; height: 13
                        source: Theme.dimIcon("icons/info-dim.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                    Text {
                        text: doneCol.finalizeNote
                        color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                        wrapMode: Text.WordWrap; Layout.fillWidth: true
                    }
                }
            }

            // ── Summary row — Cuenta · Idioma · Listo (no Modelo IA) ────
            Rectangle {
                Layout.fillWidth: true; radius: Theme.rLg
                color: Theme.card; border.color: Theme.line; border.width: 1
                Layout.preferredHeight: 64

                RowLayout {
                    anchors { fill: parent; leftMargin: Theme.sp3; rightMargin: Theme.sp3 }
                    spacing: 0

                    Repeater {
                        model: [
                            { label: "Cuenta",  icon: "icons/user-dim.svg" },
                            { label: "Idioma",  icon: "icons/globe-dim.svg" },
                            { label: "Listo",   icon: "icons/circle-check-ok.svg" }
                        ]

                        RowLayout {
                            Layout.fillWidth: true; spacing: 8

                            // Separator between items (not before first)
                            Rectangle {
                                visible: index > 0
                                width: 1; height: 28; color: Theme.line2
                                Layout.leftMargin: 0
                            }

                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 3
                                Layout.alignment: Qt.AlignHCenter

                                Image {
                                    Layout.alignment: Qt.AlignHCenter
                                    width: 18; height: 18
                                    source: Theme.dimIcon(modelData.icon)
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true; mipmap: true
                                }
                                Text {
                                    Layout.alignment: Qt.AlignHCenter
                                    text: modelData.label
                                    color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                                }
                            }
                        }
                    }
                }
            }

            Item { height: 8; width: 1 }

            // Primary CTA — "Ir al escritorio"
            Item {
                Layout.fillWidth: true; Layout.preferredHeight: 52
                // Available as soon as finalize is done (or falls back after timeout)
                opacity: doneCol.finalizeState !== "running" ? 1.0 : 0.40
                Behavior on opacity { NumberAnimation { duration: 200 } }

                Rectangle {
                    anchors.fill: parent; radius: Theme.rLg; color: Theme.accent

                    RowLayout {
                        anchors.centerIn: parent; spacing: 8

                        Text {
                            text: "Ir al escritorio"
                            color: "#FFFFFF"
                            font.family: Theme.font; font.pixelSize: 15; font.weight: Font.DemiBold
                        }
                        Image {
                            width: 16; height: 16
                            source: "icons/arrow-right-white.svg"
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (doneCol.finalizeState !== "running") onboarding.finished()
                        }
                    }
                }
            }

            // Secondary CTA — "Conectar un servicio" (optional deep-link)
            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Conectar un servicio"
                color: Theme.accentBright
                font.family: Theme.font
                font.pixelSize: Theme.tsCaption + 1
                font.weight: Font.Medium
                topPadding: 4

                MouseArea {
                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        onboarding.finished()
                        // ConnectAI is view index 8 — route after finishing onboarding
                        Qt.callLater(function() {
                            if (onboarding.shell) onboarding.shell.go(8)
                        })
                    }
                }
            }
        }
    }
}
