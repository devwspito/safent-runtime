import QtQuick
import QtQuick.Layouts
import "."

// Lumen — Conecta tu IA. Matches onboarding step-2 design exactly.
// Monogram provider tiles, Lucide chevrons, neutral dark, no emoji.
// Backend wiring preserved: addProvider / testProvider / activateProvider /
// hasActiveModel + signals onProvidersChanged / onProviderTestResult /
// onActiveProviderChanged.
Item {
    id: connectView
    property var shell: null

    property string stage: "pick"
    property int selectedIndex: -1
    property string providerId: ""
    property string testErrorMsg: ""
    property string activeModelLabel: ""

    readonly property var catalogue: [
        { kind: "anthropic",  label: "Anthropic",      monogram: "A", placeholder: "sk-ant-…",      defaultModel: "claude-opus-4-5",         needsKey: true,  needsUrl: false },
        { kind: "openai",     label: "OpenAI",          monogram: "O", placeholder: "sk-…",           defaultModel: "gpt-4o",                  needsKey: true,  needsUrl: false },
        { kind: "gemini",     label: "Google Gemini",   monogram: "G", placeholder: "AIza…",          defaultModel: "gemini-2.0-flash",         needsKey: true,  needsUrl: false },
        { kind: "groq",       label: "Groq",            monogram: "Q", placeholder: "gsk_…",          defaultModel: "llama-3.3-70b-versatile", needsKey: true,  needsUrl: false },
        { kind: "deepseek",   label: "DeepSeek",        monogram: "D", placeholder: "sk-…",           defaultModel: "deepseek-chat",           needsKey: true,  needsUrl: false },
        { kind: "openrouter", label: "OpenRouter",      monogram: "R", placeholder: "sk-or-…",        defaultModel: "openai/gpt-4o-mini",      needsKey: true,  needsUrl: false },
        { kind: "ollama",     label: "Ollama (local)",  monogram: "L", placeholder: "no necesaria",   defaultModel: "llama3.2",                needsKey: false, needsUrl: true  },
        { kind: "vllm",       label: "vLLM (local)",    monogram: "V", placeholder: "no necesaria",   defaultModel: "Qwen/Qwen3-8B",           needsKey: false, needsUrl: true  }
    ]

    Rectangle { anchors.fill: parent; color: Theme.bg0 }

    Connections {
        target: backend

        function onProviderTestResult(pid, ok, errMsg) {
            if (connectView.stage !== "testing") return
            if (ok) { connectView.stage = "success"; connectView.testErrorMsg = "" }
            else    { connectView.stage = "error";   connectView.testErrorMsg = errMsg || "No se pudo conectar. Revisa la clave de acceso e inténtalo de nuevo." }
        }

        function onActiveProviderChanged() {
            if (backend.hasActiveModel && connectView.stage === "success") backend.listProviders()
        }

        function onProvidersChanged(jsonList) {
            try {
                var arr = JSON.parse(jsonList)
                for (var i = 0; i < arr.length; i++) {
                    if (arr[i].is_active) { connectView.activeModelLabel = arr[i].default_model; return }
                }
            } catch(e) {}
        }
    }

    Flickable {
        id: connectFlick
        anchors.fill: parent
        contentHeight: mainCol.height + Theme.sp4 * 2
        clip: true
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds

        WheelScroll { target: connectFlick }

        ColumnLayout {
            id: mainCol
            // Clamp: fill screen minus 64px gutters, max 560px — matches onboarding
            width: Math.min(parent.width - 64, 560)
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: 0
            y: Theme.sp4

            // ── Identity mark ─────────────────────────────────────────────────
            Item {
                Layout.alignment: Qt.AlignHCenter
                width: 72; height: 72

                Rectangle {
                    anchors.centerIn: parent
                    width: 96; height: 96; radius: 48
                    color: Theme.alpha(Theme.accentBright, 0.06)
                }

                Image {
                    anchors.centerIn: parent
                    width: 72; height: 72
                    source: Theme.mode === "light" ? "icons/lumen-mark-light.svg" : "icons/lumen-mark.svg"
                    fillMode: Image.PreserveAspectFit
                    smooth: true; mipmap: true
                }
            }

            Item { width: 1; height: Theme.sp4 }

            // ── Title — same weight/size as onboarding ────────────────────────
            // Font.Light (300) works because InterVariable.ttf covers all weights.
            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Conecta tu IA"
                color: Theme.ink
                font.family: Theme.font
                font.pixelSize: 32
                font.weight: Font.Light
                font.letterSpacing: -0.5
            }

            Item { width: 1; height: Theme.sp1 }

            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Elige qué IA quieres usar.\nCon eso, Lumen puede ponerse a trabajar."
                color: Theme.ink3
                font.family: Theme.font
                font.pixelSize: Theme.tsLead
                font.weight: Font.Normal
                lineHeight: 1.6
                horizontalAlignment: Text.AlignHCenter
            }

            Item { width: 1; height: Theme.sp4 }

            // Stage loader — one at a time
            Loader {
                id: stageLoader
                Layout.fillWidth: true
                height: item ? item.implicitHeight : 0
                sourceComponent: connectView.stage === "pick"      ? pickStage
                              :  connectView.stage === "configure" ? configureStage
                              :  connectView.stage === "testing"   ? testingStage
                              :  connectView.stage === "success"   ? successStage
                              :                                      errorStage
            }

            Item { width: 1; height: Theme.sp3 }
        }
    }

    // ── Pick stage — identical monogram tiles to onboarding step 2 ───────────
    Component {
        id: pickStage

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: 8

            Repeater {
                model: connectView.catalogue

                Item {
                    Layout.fillWidth: true; Layout.preferredHeight: 60

                    // Static shadow
                    Rectangle {
                        anchors { left: parent.left; right: parent.right; top: parent.top }
                        anchors.topMargin: 2; anchors.leftMargin: 1; anchors.rightMargin: -1
                        height: parent.height; radius: Theme.rLg
                        color: "#000000"; opacity: 0.14
                    }

                    Rectangle {
                        anchors.fill: parent; radius: Theme.rLg
                        color: provHover.containsMouse ? Theme.surface2 : Theme.card
                        border.color: provHover.containsMouse ? Theme.line2 : Theme.line
                        border.width: 1
                        Behavior on color { ColorAnimation { duration: 120 } }
                        Behavior on border.color { ColorAnimation { duration: 120 } }

                        // Inner top hairline
                        Rectangle {
                            anchors { top: parent.top; left: parent.left; right: parent.right }
                            anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                            height: 1; radius: Theme.rLg - 1; color: "#FFFFFF"; opacity: 0.04
                        }

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                            spacing: Theme.sp2

                            // Monogram tile — accent rounded square, consistent brand identifier
                            Rectangle {
                                width: 36; height: 36; radius: 10
                                color: Theme.alpha(Theme.accent, 0.16)
                                border.color: Theme.alpha(Theme.accentBright, 0.18); border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: modelData.monogram
                                    color: Theme.accentBright
                                    font.family: Theme.font
                                    font.pixelSize: 16
                                    font.weight: Font.DemiBold
                                }
                            }

                            ColumnLayout {
                                spacing: 2; Layout.fillWidth: true

                                Text {
                                    text: modelData.label
                                    color: Theme.ink
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsBody
                                    font.weight: Font.Medium
                                }
                                Text {
                                    text: modelData.needsUrl ? "Local · sin clave necesaria" : "API key requerida"
                                    color: Theme.ink3
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption
                                }
                            }

                            // Lucide chevron-right — not "›"
                            Image {
                                width: 16; height: 16
                                source: Theme.dimIcon("icons/chevron-right-dim.svg")
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                        }

                        MouseArea {
                            id: provHover
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: { connectView.selectedIndex = index; connectView.stage = "configure" }
                        }
                    }
                }
            }
        }
    }

    // ── Configure stage ────────────────────────────────────────────────────────
    Component {
        id: configureStage

        ColumnLayout {
            id: cfgCol
            width: parent ? parent.width : 0
            spacing: Theme.sp2

            property var prov: connectView.selectedIndex >= 0
                                ? connectView.catalogue[connectView.selectedIndex]
                                : connectView.catalogue[0]

            // Back link — mirrored Lucide chevron
            RowLayout {
                spacing: 6

                Image {
                    width: 14; height: 14
                    source: Theme.dimIcon("icons/chevron-right-dim.svg")
                    fillMode: Image.PreserveAspectFit
                    smooth: true; mipmap: true
                    mirror: true
                }
                Text {
                    text: "Cambiar proveedor"
                    color: Theme.accentBright
                    font.family: Theme.font
                    font.pixelSize: Theme.tsCaption + 1
                }
                MouseArea {
                    Layout.fillWidth: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: { connectView.stage = "pick"; connectView.selectedIndex = -1 }
                }
            }

            // Selected provider badge
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 58; radius: Theme.rLg
                color: Theme.alpha(Theme.accent, 0.09)
                border.color: Theme.alpha(Theme.accentBright, 0.28); border.width: 1

                RowLayout {
                    anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                    spacing: Theme.sp2

                    Rectangle {
                        width: 36; height: 36; radius: 10
                        color: Theme.alpha(Theme.accent, 0.22)
                        border.color: Theme.alpha(Theme.accentBright, 0.24); border.width: 1

                        Text {
                            anchors.centerIn: parent
                            text: cfgCol.prov.monogram
                            color: Theme.accentBright
                            font.family: Theme.font; font.pixelSize: 16; font.weight: Font.DemiBold
                        }
                    }

                    Text {
                        text: cfgCol.prov.label
                        color: Theme.ink
                        font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.DemiBold
                        Layout.fillWidth: true
                    }

                    Rectangle {
                        width: 8; height: 8; radius: 4
                        color: Theme.accentBright
                    }
                }
            }

            // API Key — shown when needsKey
            ColumnLayout {
                Layout.fillWidth: true; spacing: 6
                visible: cfgCol.prov.needsKey

                RowLayout {
                    spacing: 6
                    Image {
                        width: 14; height: 14
                        source: Theme.dimIcon("icons/key-dim.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                    }
                    Text {
                        text: "Clave de acceso"
                        color: Theme.ink2; font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium
                    }
                }

                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 44; radius: Theme.rMd; color: Theme.card2
                    border.color: apiKeyInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line; border.width: 1
                    Behavior on border.color { ColorAnimation { duration: 150 } }

                    RowLayout {
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: 8 }
                        spacing: Theme.sp1

                        TextInput {
                            id: apiKeyInput
                            Layout.fillWidth: true
                            verticalAlignment: Text.AlignVCenter
                            color: Theme.ink; font.family: Theme.mono; font.pixelSize: 13; clip: true
                            echoMode: eyeKeyBtn.revealed ? TextInput.Normal : TextInput.Password
                            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoAutoUppercase
                        }

                        Rectangle {
                            id: eyeKeyBtn
                            property bool revealed: false
                            width: 30; height: 30; radius: Theme.rSm - 2
                            color: eyeKeyHover.containsMouse ? Theme.alpha(Theme.accentBright, 0.10) : "transparent"
                            Behavior on color { ColorAnimation { duration: 120 } }

                            Image {
                                anchors.centerIn: parent; width: 15; height: 15
                                source: eyeKeyBtn.revealed ? "icons/eye-off-dim.svg" : "icons/eye-dim.svg"
                                fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                            }
                            MouseArea {
                                id: eyeKeyHover; anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                hoverEnabled: true; onClicked: eyeKeyBtn.revealed = !eyeKeyBtn.revealed
                            }
                        }
                    }

                    Text {
                        visible: apiKeyInput.text.length === 0
                        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; leftMargin: Theme.sp2 }
                        text: cfgCol.prov.placeholder
                        color: Theme.ink4; font.family: Theme.mono; font.pixelSize: 13
                    }
                }
            }

            // Base URL — shown when needsUrl
            ColumnLayout {
                Layout.fillWidth: true; spacing: 6
                visible: cfgCol.prov.needsUrl

                RowLayout {
                    spacing: 6
                    Image {
                        width: 14; height: 14
                        source: Theme.dimIcon("icons/server-dim.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                    }
                    Text {
                        text: "URL del servidor"
                        color: Theme.ink2; font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium
                    }
                }

                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 44; radius: Theme.rMd; color: Theme.card2
                    border.color: baseUrlInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line; border.width: 1
                    Behavior on border.color { ColorAnimation { duration: 150 } }

                    TextInput {
                        id: baseUrlInput
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                        verticalAlignment: Text.AlignVCenter
                        color: Theme.ink; font.family: Theme.mono; font.pixelSize: 13; clip: true
                        text: {
                            if (connectView.selectedIndex < 0) return ""
                            var p = connectView.catalogue[connectView.selectedIndex]
                            if (p.kind === "ollama") return "http://localhost:11434/v1"
                            if (p.kind === "vllm")   return "http://localhost:8000/v1"
                            return ""
                        }
                    }
                    Text {
                        visible: baseUrlInput.text.length === 0
                        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; leftMargin: Theme.sp2 }
                        text: "http://localhost:11434/v1"; color: Theme.ink4; font.family: Theme.mono; font.pixelSize: 13
                    }
                }
            }

            // Default model
            ColumnLayout {
                Layout.fillWidth: true; spacing: 6

                RowLayout {
                    spacing: 6
                    Image {
                        width: 14; height: 14; source: Theme.dimIcon("icons/cpu-dim.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                    }
                    Text { text: "Versión de la IA"; color: Theme.ink2; font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium }
                }

                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 44; radius: Theme.rMd; color: Theme.card2
                    border.color: modelInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line; border.width: 1
                    Behavior on border.color { ColorAnimation { duration: 150 } }

                    TextInput {
                        id: modelInput
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                        verticalAlignment: Text.AlignVCenter; color: Theme.ink; font.family: Theme.mono; font.pixelSize: 13; clip: true
                        text: connectView.selectedIndex >= 0 ? connectView.catalogue[connectView.selectedIndex].defaultModel : ""
                    }
                }
            }

            // Alias
            ColumnLayout {
                Layout.fillWidth: true; spacing: 6

                RowLayout {
                    spacing: 6
                    Image {
                        width: 14; height: 14; source: Theme.dimIcon("icons/user-dim.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                    }
                    Text { text: "Nombre para reconocerla"; color: Theme.ink2; font.family: Theme.font; font.pixelSize: 13; font.weight: Font.Medium }
                }

                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 44; radius: Theme.rMd; color: Theme.card2
                    border.color: aliasInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.55) : Theme.line; border.width: 1
                    Behavior on border.color { ColorAnimation { duration: 150 } }

                    TextInput {
                        id: aliasInput
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                        verticalAlignment: Text.AlignVCenter; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; clip: true
                        text: connectView.selectedIndex >= 0 ? connectView.catalogue[connectView.selectedIndex].label : ""
                    }
                }
            }

            // Connect button — full-width accent, matches onboarding CTA
            Rectangle {
                id: connectBtn
                Layout.fillWidth: true; Layout.preferredHeight: 52; radius: Theme.rLg
                color: Theme.accent
                opacity: _canConnect() ? 1.0 : 0.32
                Behavior on opacity { NumberAnimation { duration: 150 } }

                function _canConnect() {
                    if (connectView.selectedIndex < 0) return false
                    var p = connectView.catalogue[connectView.selectedIndex]
                    if (p.needsKey && apiKeyInput.text.trim().length < 8) return false
                    if (modelInput.text.trim().length === 0) return false
                    return true
                }

                RowLayout {
                    anchors.centerIn: parent; spacing: 8

                    Text {
                        text: "Comprobar y conectar"
                        color: "#FFFFFF"
                        font.family: Theme.font; font.pixelSize: 15; font.weight: Font.DemiBold
                    }
                    Image {
                        width: 16; height: 16
                        source: "icons/arrow-right-white.svg"
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                    }
                }

                MouseArea {
                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (!connectBtn._canConnect()) return
                        var p = connectView.catalogue[connectView.selectedIndex]
                        var alias = aliasInput.text.trim() || p.label
                        var model = modelInput.text.trim()
                        var key   = p.needsKey ? apiKeyInput.text.trim() : ""
                        connectView.stage = "testing"
                        connectView._connectProvider(p.kind, alias, model, key)
                    }
                }
            }
        }
    }

    // ── Testing stage — static indicator, no RotationAnimator ─────────────────
    Component {
        id: testingStage

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: Theme.sp3

            Item { height: Theme.sp2; width: 1 }

            Item {
                Layout.alignment: Qt.AlignHCenter; width: 64; Layout.preferredHeight: 64

                Rectangle {
                    anchors.fill: parent; radius: 32
                    color: Theme.alpha(Theme.accent, 0.10)
                    border.color: Theme.alpha(Theme.accentBright, 0.28); border.width: 2
                }

                Image {
                    anchors.centerIn: parent
                    width: 28; height: 28
                    source: Theme.dimIcon("icons/wifi-dim.svg")
                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                }
            }

            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Comprobando…"
                color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.Medium
            }
            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Esto puede tardar unos segundos"
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption
            }

            Item { height: Theme.sp2; width: 1 }
        }
    }

    // ── Success stage ──────────────────────────────────────────────────────────
    Component {
        id: successStage

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: Theme.sp3

            Item { height: Theme.sp2; width: 1 }

            Item {
                Layout.alignment: Qt.AlignHCenter; width: 72; Layout.preferredHeight: 72

                Rectangle {
                    anchors.fill: parent; radius: 36
                    color: Theme.alpha(Theme.ok, 0.10)
                    border.color: Theme.alpha(Theme.ok, 0.28); border.width: 2
                }

                Image {
                    anchors.centerIn: parent; width: 32; height: 32
                    source: "icons/circle-check-ok.svg"
                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                }

                // One-shot pop — loops:1, not Infinite
                SequentialAnimation on scale {
                    running: connectView.stage === "success"
                    loops: 1
                    NumberAnimation { from: 0.7; to: 1.08; duration: 300; easing.type: Easing.OutBack }
                    NumberAnimation { from: 1.08; to: 1.0; duration: 180; easing.type: Easing.InOutQuad }
                }
            }

            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Todo listo"
                color: Theme.ok; font.family: Theme.font; font.pixelSize: Theme.tsSubtitle; font.weight: Font.DemiBold
            }
            Text {
                Layout.alignment: Qt.AlignHCenter
                text: connectView.activeModelLabel.length > 0
                      ? "Usando " + connectView.activeModelLabel
                      : "Lista para trabajar"
                color: Theme.ink2; font.family: Theme.font; font.pixelSize: Theme.tsBody
            }

            Item { height: 4; width: 1 }

            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 52; radius: Theme.rLg; color: Theme.accent

                RowLayout {
                    anchors.centerIn: parent; spacing: 8
                    Text { text: "Ir al chat"; color: "#FFFFFF"; font.family: Theme.font; font.pixelSize: 15; font.weight: Font.DemiBold }
                    Image { width: 16; height: 16; source: "icons/arrow-right-white.svg"; fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true }
                }

                MouseArea {
                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: { if (connectView.shell) connectView.shell.go(1) }
                }
            }

            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Conectar otro proveedor"
                color: Theme.accentBright; font.family: Theme.font; font.pixelSize: Theme.tsCaption + 1
                topPadding: 4

                MouseArea {
                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: { connectView.stage = "pick"; connectView.selectedIndex = -1; connectView.testErrorMsg = ""; connectView.activeModelLabel = "" }
                }
            }

            Item { height: Theme.sp2; width: 1 }
        }
    }

    // ── Error stage ────────────────────────────────────────────────────────────
    Component {
        id: errorStage

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: Theme.sp2

            Item { height: Theme.sp2; width: 1 }

            Item {
                Layout.alignment: Qt.AlignHCenter; width: 64; Layout.preferredHeight: 64

                Rectangle {
                    anchors.fill: parent; radius: 32
                    color: Theme.alpha(Theme.warn, 0.10)
                    border.color: Theme.alpha(Theme.warn, 0.30); border.width: 2
                }

                Image {
                    anchors.centerIn: parent; width: 28; height: 28
                    source: "icons/alert-circle-warn.svg"
                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                }
            }

            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "No se pudo conectar"
                color: Theme.warn; font.family: Theme.font; font.pixelSize: Theme.tsSubtitle; font.weight: Font.DemiBold
            }

            Rectangle {
                Layout.fillWidth: true; radius: Theme.rMd
                color: Theme.alpha(Theme.warn, 0.06); border.color: Theme.alpha(Theme.warn, 0.22); border.width: 1
                height: aiErrTxt.height + Theme.sp3

                Text {
                    id: aiErrTxt
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.sp2; topMargin: 12 }
                    text: connectView.testErrorMsg || "Error desconocido"
                    color: Theme.ink2; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                    wrapMode: Text.WordWrap; lineHeight: 1.45
                }
            }

            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 48; radius: Theme.rLg
                color: Theme.alpha(Theme.warn, 0.12); border.color: Theme.alpha(Theme.warn, 0.30); border.width: 1

                Text {
                    anchors.centerIn: parent
                    text: "Revisar los datos"
                    color: Theme.warn; font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.Medium
                }
                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: connectView.stage = "configure" }
            }

            Text {
                Layout.alignment: Qt.AlignHCenter
                text: "Elegir otro proveedor"
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption + 1

                MouseArea {
                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: { connectView.stage = "pick"; connectView.selectedIndex = -1; connectView.testErrorMsg = "" }
                }
            }

            Item { height: Theme.sp2; width: 1 }
        }
    }

    // ── Helper: add provider then test ─────────────────────────────────────────
    property string _pendingTestKind: ""

    Connections {
        id: postAddWatcher
        target: backend
        enabled: false

        function onProvidersChanged(jsonList) {
            try {
                var arr = JSON.parse(jsonList)
                for (var i = 0; i < arr.length; i++) {
                    if (arr[i].kind === connectView._pendingTestKind) {
                        postAddWatcher.enabled = false
                        connectView._pendingTestKind = ""
                        backend.testProvider(arr[i].provider_id)
                        return
                    }
                }
            } catch(e) {}
        }
    }

    function _connectProvider(kind, alias, model, key) {
        connectView._pendingTestKind = kind
        postAddWatcher.enabled = true
        backend.addProvider(kind, alias, model, key)
    }
}
