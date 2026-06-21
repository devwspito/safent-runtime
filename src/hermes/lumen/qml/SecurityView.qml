import QtQuick
import QtQuick.Layouts
import "."

// Lumen — Seguridad. Premium neutral-dark design, matching onboarding bar.
// NO emoji. NO MultiEffect/blurEnabled. NO RotationAnimator/loops:Infinite.
Item {
    id: secView
    property var shell: null

    // Actividad REAL del agente (registro de acciones). Cero mock.
    ListModel { id: secActivity }

    // FR-013: consents activos (resueltos server-side por sender_uid).
    // Modelo: [{capability, scope, granted_at, expires_at}]
    ListModel { id: activeConsents }

    // Catálogo de capabilities disponibles para otorgar consent.
    // Debe coincidir con Capability enum en consent_manager.py.
    readonly property var _capabilityList: [
        { value: "documents",      label: "Documentos",         description: "Leer y escribir en carpeta de documentos" },
        { value: "downloads",      label: "Descargas",          description: "Acceder a la carpeta de descargas" },
        { value: "desktop_files",  label: "Escritorio",         description: "Archivos del escritorio" },
        { value: "terminal",       label: "Terminal",           description: "Ejecutar comandos de terminal" },
        { value: "filesystem_full",label: "Sistema de archivos",description: "Acceso completo al sistema de archivos" },
        { value: "browser",        label: "Navegador",          description: "Abrir y operar el navegador confinado" },
        { value: "input_control",  label: "Control de entrada", description: "Inyectar mouse y teclado en el escritorio" },
        { value: "screen",         label: "Captura de pantalla",description: "Capturar la pantalla" },
        { value: "microphone",     label: "Micrófono",          description: "Acceder al micrófono" },
        { value: "network_local",  label: "Red local",          description: "Conectarse a la red local" },
        { value: "system_settings",label: "Configuración",      description: "Modificar ajustes del sistema" }
    ]

    property string _consentFeedback: ""
    property bool   _consentFeedbackOk: false

    function _relTime(iso) {
        if (!iso) return ""
        var t = Date.parse(iso); if (isNaN(t)) return ""
        var s = Math.max(0, Math.floor((Date.now() - t) / 1000))
        if (s < 60) return "hace " + s + " s"
        if (s < 3600) return "hace " + Math.floor(s / 60) + " min"
        if (s < 86400) return "hace " + Math.floor(s / 3600) + " h"
        return "hace " + Math.floor(s / 86400) + " d"
    }

    // Returns display label for a capability value string.
    function _capLabel(val) {
        for (var i = 0; i < _capabilityList.length; i++) {
            if (_capabilityList[i].value === val) return _capabilityList[i].label
        }
        return val
    }

    // Returns true if capability value is in activeConsents.
    function _isGranted(val) {
        for (var i = 0; i < activeConsents.count; i++) {
            if (activeConsents.get(i).capability === val) return true
        }
        return false
    }

    Connections {
        target: backend
        function onListLoaded(key, json) {
            if (key !== "recent_tasks") return
            var arr = []; try { arr = JSON.parse(json) } catch (e) { arr = [] }
            secActivity.clear()
            for (var i = 0; i < arr.length && i < 12; i++) {
                var r = arr[i]
                var st = (r.status || "").toLowerCase()
                var ok = (st.indexOf("fail") < 0 && st.indexOf("reject") < 0)
                secActivity.append({
                    action: (r.label && r.label.length) ? r.label : (r.trigger_kind || "Acción"),
                    outcome: r.status || "",
                    time: secView._relTime(r.enqueued_at),
                    outcomeOk: ok
                })
            }
        }

        // FR-013: backend.consentsLoaded(json) arrives when ListConsents returns.
        function onConsentsLoaded(json) {
            var arr = []; try { arr = JSON.parse(json) } catch (e) { arr = [] }
            activeConsents.clear()
            for (var i = 0; i < arr.length; i++) {
                var c = arr[i]
                activeConsents.append({
                    capability: c.capability || "",
                    scope:      c.scope || "",
                    granted_at: c.granted_at || "",
                    expires_at: c.expires_at || ""
                })
            }
        }

        // FR-013: feedback from grant/revoke operations.
        function onConsentActionResult(capability, ok, message) {
            secView._consentFeedbackOk = ok
            if (ok) {
                secView._consentFeedback = secView._capLabel(capability) + " actualizado."
            } else {
                secView._consentFeedback = message || "No se pudo aplicar el cambio. Inténtalo de nuevo."
            }
            feedbackTimer.restart()
        }
    }

    Timer {
        id: feedbackTimer
        interval: 4000
        onTriggered: secView._consentFeedback = ""
    }

    Component.onCompleted: {
        backend.loadList("recent_tasks", 12)
        if (typeof backend.refreshConsents === "function") backend.refreshConsents()
    }
    Timer { interval: 6000; running: true; repeat: true; onTriggered: backend.loadList("recent_tasks", 12) }

    Rectangle { anchors.fill: parent; color: Theme.bg0 }

    Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentItem.height + Theme.sp4 * 2
        clip: true
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds

        WheelScroll { target: flick }

        Item {
            id: contentItem
            width: Math.min(flick.width - 64, 1100)
            height: mainCol.implicitHeight
            x: (flick.width - width) / 2
            y: Theme.sp4

            ColumnLayout {
                id: mainCol
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: 0

                // ── Hero ──────────────────────────────────────────────────────
                ColumnLayout {
                    Layout.alignment: Qt.AlignHCenter
                    spacing: 0

                    // Shield icon — Lucide SVG, restrained ok-green tint
                    Item {
                        Layout.alignment: Qt.AlignHCenter
                        width: 72; height: 72

                        // Soft halo — single rectangle, no blur
                        Rectangle {
                            anchors.centerIn: parent
                            width: 100; height: 100; radius: 50
                            color: Theme.alpha(Theme.ok, 0.07)
                        }

                        // Icon container square
                        Rectangle {
                            anchors.centerIn: parent
                            width: 72; height: 72; radius: Theme.rXl
                            color: Theme.alpha(Theme.ok, 0.10)
                            border.color: Theme.alpha(Theme.ok, 0.22); border.width: 1

                            // Inner top hairline
                            Rectangle {
                                anchors { top: parent.top; left: parent.left; right: parent.right }
                                anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                                height: 1; radius: Theme.rXl - 1; color: "#FFFFFF"; opacity: 0.06
                            }

                            Image {
                                anchors.centerIn: parent
                                width: 36; height: 36
                                source: "icons/shield-check-ok.svg"
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                        }
                    }

                    Item { height: Theme.sp3; width: 1 }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "Tu equipo es tuyo."
                        color: Theme.ink
                        font.family: Theme.font
                        font.pixelSize: 32
                        font.weight: Font.Light
                        font.letterSpacing: -0.5
                    }

                    Item { height: 8; width: 1 }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "Nada sale sin tu permiso. Lumen trabaja para ti, dentro de tus límites."
                        color: Theme.ink3
                        font.family: Theme.font
                        font.pixelSize: Theme.tsBody
                        horizontalAlignment: Text.AlignHCenter
                    }

                    Item { height: Theme.sp2; width: 1 }

                    // "All active" status pill — Lucide check icon, no emoji ✓
                    Rectangle {
                        Layout.alignment: Qt.AlignHCenter
                        radius: Theme.rSm; height: 28
                        color: Theme.alpha(Theme.ok, 0.10)
                        border.color: Theme.alpha(Theme.ok, 0.25); border.width: 1
                        implicitWidth: pillRow.implicitWidth + 24

                        RowLayout {
                            id: pillRow
                            anchors.centerIn: parent
                            spacing: 6

                            Image {
                                width: 12; height: 12
                                source: "icons/check-ok.svg"
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }

                            Text {
                                text: "Todas las capas activas"
                                color: Theme.ok
                                font.family: Theme.font
                                font.pixelSize: Theme.tsCaption
                                font.weight: Font.Medium
                            }
                        }
                    }
                }

                Item { height: Theme.sp4; width: 1 }

                // ── Protection cards — 3-column grid ─────────────────────────
                GridLayout {
                    Layout.fillWidth: true
                    columns: 3
                    columnSpacing: Theme.sp2
                    rowSpacing: Theme.sp2

                    Repeater {
                        model: [
                            { icon: "icons/shield-check-ok.svg",  title: "Permiso para todo lo sensible",   body: "Lumen te pregunta antes de enviar, comprar, borrar o salir a internet." },
                            { icon: "icons/box-dim.svg",           title: "Aislamiento del navegador",        body: "Cada acción web corre en una caja a nivel de kernel, separada de tus datos." },
                            { icon: "icons/file-check-ok.svg",    title: "Registro firmado e inviolable",    body: "Todo lo que hace Lumen queda firmado y puedes verificarlo en cualquier momento." },
                            { icon: "icons/server-ok.svg",        title: "Todo en tu equipo",                body: "El agente vive en local; tus datos no se suben a ninguna nube." },
                            { icon: "icons/lock-ok.svg",          title: "Control de salida",                body: "Lumen solo se conecta a donde tú autorizas. El resto está bloqueado." },
                            { icon: "icons/activity-ok.svg",      title: "Tú mandas, siempre",               body: "Pausa, observa o toma el control en cualquier momento." }
                        ]

                        delegate: Item {
                            Layout.fillWidth: true
                            height: cardInner.height + Theme.sp3

                            // Soft static shadow
                            Rectangle {
                                anchors { left: parent.left; right: parent.right; top: parent.top }
                                anchors.topMargin: 3; anchors.leftMargin: 2; anchors.rightMargin: -2
                                height: parent.height; radius: Theme.rLg
                                color: "#000000"; opacity: 0.16
                            }

                            Rectangle {
                                anchors.fill: parent
                                radius: Theme.rLg; color: Theme.card
                                border.color: Theme.line; border.width: 1

                                // Inner top hairline
                                Rectangle {
                                    anchors { top: parent.top; left: parent.left; right: parent.right }
                                    anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                                    height: 1; radius: Theme.rLg - 1; color: "#FFFFFF"; opacity: 0.06
                                }

                                ColumnLayout {
                                    id: cardInner
                                    anchors {
                                        left: parent.left; right: parent.right; top: parent.top
                                        leftMargin: Theme.sp3; rightMargin: Theme.sp3; topMargin: Theme.sp3
                                    }
                                    spacing: 0

                                    RowLayout {
                                        spacing: Theme.sp2

                                        // Icon in restrained ok-green container
                                        Rectangle {
                                            width: 40; height: 40; radius: Theme.rMd
                                            color: Theme.alpha(Theme.ok, 0.10)
                                            border.color: Theme.alpha(Theme.ok, 0.18); border.width: 1

                                            Image {
                                                anchors.centerIn: parent
                                                width: 20; height: 20
                                                source: Theme.dimIcon(modelData.icon)
                                                fillMode: Image.PreserveAspectFit
                                                smooth: true; mipmap: true
                                            }
                                        }

                                        ColumnLayout {
                                            spacing: 4; Layout.fillWidth: true

                                            Text {
                                                text: modelData.title
                                                color: Theme.ink
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsCaption + 1
                                                font.weight: Font.DemiBold
                                                Layout.fillWidth: true
                                                wrapMode: Text.WordWrap
                                            }

                                            // Active status pill — Lucide check icon, no "✓"
                                            Rectangle {
                                                height: 18; radius: Theme.rSm - 4
                                                color: Theme.alpha(Theme.ok, 0.10)
                                                border.color: Theme.alpha(Theme.ok, 0.20); border.width: 1
                                                implicitWidth: statusRow.implicitWidth + 12

                                                RowLayout {
                                                    id: statusRow
                                                    anchors.centerIn: parent
                                                    spacing: 4

                                                    Image {
                                                        width: 10; height: 10
                                                        source: "icons/check-ok.svg"
                                                        fillMode: Image.PreserveAspectFit
                                                        smooth: true; mipmap: true
                                                    }
                                                    Text {
                                                        text: "Activo"
                                                        color: Theme.ok
                                                        font.family: Theme.font
                                                        font.pixelSize: Theme.tsMicro
                                                        font.weight: Font.Medium
                                                    }
                                                }
                                            }
                                        }
                                    }

                                    Item { height: Theme.sp2; width: 1 }

                                    Text {
                                        text: modelData.body
                                        color: Theme.ink3
                                        font.family: Theme.font
                                        font.pixelSize: Theme.tsCaption
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        lineHeight: 1.45
                                    }

                                    Item { height: Theme.sp1 + 2; width: 1 }
                                }
                            }
                        }
                    }
                }

                Item { height: Theme.sp4; width: 1 }

                // ── FR-013: Panel de consentimiento por capability ─────────────
                // Operador otorga / revoca consent en tiempo real.
                // Authorship resuelto server-side por sender_uid (CWE-862).
                Item {
                    Layout.fillWidth: true
                    height: consentCard.height

                    // Static shadow
                    Rectangle {
                        anchors { left: parent.left; right: parent.right; top: parent.top }
                        anchors.topMargin: 3; anchors.leftMargin: 2; anchors.rightMargin: -2
                        height: parent.height; radius: Theme.rLg
                        color: "#000000"; opacity: 0.16
                    }

                    Rectangle {
                        id: consentCard
                        width: parent.width
                        height: consentCol.height + Theme.sp4
                        radius: Theme.rLg; color: Theme.card
                        border.color: Theme.line; border.width: 1

                        // Inner top hairline
                        Rectangle {
                            anchors { top: parent.top; left: parent.left; right: parent.right }
                            anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                            height: 1; radius: Theme.rLg - 1; color: "#FFFFFF"; opacity: 0.06
                        }

                        ColumnLayout {
                            id: consentCol
                            anchors {
                                left: parent.left; right: parent.right; top: parent.top
                                leftMargin: Theme.sp3; rightMargin: Theme.sp3; topMargin: Theme.sp3
                            }
                            spacing: 0

                            // Header row
                            RowLayout {
                                spacing: Theme.sp1

                                Rectangle {
                                    width: 28; height: 28; radius: Theme.rSm
                                    color: Theme.alpha(Theme.accent, 0.10)
                                    border.color: Theme.alpha(Theme.accent, 0.18); border.width: 1

                                    Image {
                                        anchors.centerIn: parent
                                        width: 14; height: 14
                                        source: Theme.accentIcon("icons/lock-ok.svg")
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                    }
                                }

                                Text {
                                    text: "Qué puede hacer Lumen"
                                    color: Theme.ink
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption + 1
                                    font.weight: Font.DemiBold
                                }

                                Item { Layout.fillWidth: true }

                                // Feedback pill (grant / revoke result)
                                Rectangle {
                                    visible: secView._consentFeedback.length > 0
                                    height: 22; radius: Theme.rSm - 4
                                    color: secView._consentFeedbackOk
                                           ? Theme.alpha(Theme.ok, 0.10)
                                           : Theme.alpha(Theme.danger, 0.10)
                                    border.color: secView._consentFeedbackOk
                                                  ? Theme.alpha(Theme.ok, 0.25)
                                                  : Theme.alpha(Theme.danger, 0.25)
                                    border.width: 1
                                    implicitWidth: feedbackText.implicitWidth + 16

                                    Text {
                                        id: feedbackText
                                        anchors.centerIn: parent
                                        text: secView._consentFeedback
                                        color: secView._consentFeedbackOk ? Theme.ok : Theme.danger
                                        font.family: Theme.font
                                        font.pixelSize: Theme.tsMicro
                                        font.weight: Font.Medium
                                    }
                                }
                            }

                            Item { height: Theme.sp2; width: 1 }

                            Text {
                                text: "Activa o desactiva lo que Lumen puede hacer ahora mismo. Los cambios son inmediatos."
                                color: Theme.ink3
                                font.family: Theme.font
                                font.pixelSize: Theme.tsCaption
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }

                            Item { height: Theme.sp2; width: 1 }
                            Rectangle { width: parent.width; height: 1; color: Theme.line }
                            Item { height: Theme.sp2; width: 1 }

                            // Capability rows — one per entry in _capabilityList
                            Repeater {
                                model: secView._capabilityList

                                delegate: ColumnLayout {
                                    width: consentCol.width
                                    spacing: 0

                                    RowLayout {
                                        spacing: Theme.sp2
                                        Layout.fillWidth: true

                                        // Status indicator dot
                                        Rectangle {
                                            width: 8; height: 8; radius: 4
                                            color: secView._isGranted(modelData.value)
                                                   ? Theme.ok
                                                   : Theme.alpha(Theme.ink3, 0.30)
                                        }

                                        ColumnLayout {
                                            spacing: 2; Layout.fillWidth: true

                                            Text {
                                                text: modelData.label
                                                color: Theme.ink
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsCaption
                                                font.weight: Font.Medium
                                            }
                                            Text {
                                                text: modelData.description
                                                color: Theme.ink3
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsMicro
                                            }
                                        }

                                        // Scope selector (visible only when granted)
                                        Rectangle {
                                            visible: secView._isGranted(modelData.value)
                                            height: 20; radius: Theme.rSm - 4
                                            color: Theme.alpha(Theme.ok, 0.08)
                                            border.color: Theme.alpha(Theme.ok, 0.18); border.width: 1
                                            implicitWidth: scopeText.implicitWidth + 12

                                            Text {
                                                id: scopeText
                                                anchors.centerIn: parent
                                                text: (function() {
                                                    for (var i = 0; i < activeConsents.count; i++) {
                                                        if (activeConsents.get(i).capability === modelData.value)
                                                            return activeConsents.get(i).scope
                                                    }
                                                    return ""
                                                })()
                                                color: Theme.ok
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsMicro
                                                font.weight: Font.Medium
                                            }
                                        }

                                        // Grant / Revoke button
                                        Rectangle {
                                            height: 28
                                            radius: Theme.rSm
                                            implicitWidth: capBtnText.implicitWidth + 24
                                            color: secView._isGranted(modelData.value)
                                                   ? Theme.alpha(Theme.danger, 0.10)
                                                   : Theme.alpha(Theme.accent, 0.10)
                                            border.color: secView._isGranted(modelData.value)
                                                          ? Theme.alpha(Theme.danger, 0.22)
                                                          : Theme.alpha(Theme.accent, 0.22)
                                            border.width: 1

                                            property string _cap: modelData.value
                                            property bool _granted: secView._isGranted(modelData.value)

                                            Text {
                                                id: capBtnText
                                                anchors.centerIn: parent
                                                text: parent._granted ? "Quitar permiso" : "Permitir"
                                                color: parent._granted ? Theme.danger : Theme.accentBright
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsCaption
                                                font.weight: Font.Medium
                                            }

                                            MouseArea {
                                                anchors.fill: parent
                                                cursorShape: Qt.PointingHandCursor
                                                onClicked: {
                                                    var cap = parent._cap
                                                    if (parent._granted) {
                                                        if (typeof backend.revokeConsent === "function")
                                                            backend.revokeConsent(cap)
                                                    } else {
                                                        if (typeof backend.grantConsent === "function")
                                                            backend.grantConsent(cap, "session")
                                                    }
                                                }
                                            }
                                        }
                                    }

                                    // Divider (skip after last item)
                                    Rectangle {
                                        visible: index < secView._capabilityList.length - 1
                                        Layout.fillWidth: true
                                        height: 1
                                        color: Theme.line
                                        opacity: 0.45
                                        Layout.topMargin: Theme.sp1
                                        Layout.bottomMargin: Theme.sp1
                                    }
                                }
                            }

                            Item { height: Theme.sp1; width: 1 }
                        }
                    }
                }

                Item { height: Theme.sp4; width: 1 }

                // ── Verified activity strip ────────────────────────────────────
                Item {
                    Layout.fillWidth: true
                    height: activityCard.height

                    // Static shadow
                    Rectangle {
                        anchors { left: parent.left; right: parent.right; top: parent.top }
                        anchors.topMargin: 3; anchors.leftMargin: 2; anchors.rightMargin: -2
                        height: parent.height; radius: Theme.rLg
                        color: "#000000"; opacity: 0.16
                    }

                    Rectangle {
                        id: activityCard
                        width: parent.width
                        height: activityCol.height + Theme.sp4
                        radius: Theme.rLg; color: Theme.card
                        border.color: Theme.line; border.width: 1

                        Rectangle {
                            anchors { top: parent.top; left: parent.left; right: parent.right }
                            anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                            height: 1; radius: Theme.rLg - 1; color: "#FFFFFF"; opacity: 0.06
                        }

                        ColumnLayout {
                            id: activityCol
                            anchors {
                                left: parent.left; right: parent.right; top: parent.top
                                leftMargin: Theme.sp3; rightMargin: Theme.sp3; topMargin: Theme.sp3
                            }
                            spacing: 0

                            // Header
                            RowLayout {
                                spacing: Theme.sp1

                                Rectangle {
                                    width: 28; height: 28; radius: Theme.rSm
                                    color: Theme.alpha(Theme.ok, 0.10)
                                    border.color: Theme.alpha(Theme.ok, 0.18); border.width: 1

                                    Image {
                                        anchors.centerIn: parent
                                        width: 14; height: 14
                                        source: Theme.accentIcon("icons/history-accent.svg")
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                    }
                                }

                                Text {
                                    text: "Actividad reciente verificada"
                                    color: Theme.ink
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption + 1
                                    font.weight: Font.DemiBold
                                }

                                Item { Layout.fillWidth: true }

                                RowLayout {
                                    spacing: 4
                                    Text {
                                        text: "Ver historial completo"
                                        color: Theme.accentBright
                                        font.family: Theme.font
                                        font.pixelSize: Theme.tsCaption
                                        font.weight: Font.Medium
                                    }
                                    Image {
                                        width: 12; height: 12
                                        source: Theme.dimIcon("icons/chevron-right-dim.svg")
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                    }
                                }
                            }

                            Item { height: Theme.sp2; width: 1 }
                            Rectangle { width: parent.width; height: 1; color: Theme.line }

                            Repeater {
                                model: secActivity

                                delegate: ColumnLayout {
                                    width: activityCol.width; spacing: 0

                                    Item { height: Theme.sp2; width: 1 }

                                    RowLayout {
                                        spacing: Theme.sp2

                                        // Status mark — Lucide check or x icon
                                        Rectangle {
                                            width: 32; height: 32; radius: Theme.rSm
                                            color: model.outcomeOk ? Theme.alpha(Theme.ok, 0.10) : Theme.alpha(Theme.ink3, 0.08)
                                            border.color: model.outcomeOk ? Theme.alpha(Theme.ok, 0.18) : Theme.alpha(Theme.ink3, 0.12)
                                            border.width: 1

                                            Image {
                                                anchors.centerIn: parent
                                                width: 14; height: 14
                                                source: model.outcomeOk ? "icons/check-ok.svg" : "icons/x-dim.svg"
                                                fillMode: Image.PreserveAspectFit
                                                smooth: true; mipmap: true
                                            }
                                        }

                                        ColumnLayout {
                                            spacing: 3; Layout.fillWidth: true

                                            Text {
                                                text: model.action
                                                color: Theme.ink
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsCaption
                                                font.weight: Font.Medium
                                            }

                                            RowLayout {
                                                spacing: Theme.sp1
                                                Text {
                                                    text: model.outcome
                                                    color: model.outcomeOk ? Theme.ok : Theme.ink3
                                                    font.family: Theme.font; font.pixelSize: Theme.tsCaption
                                                }
                                                Text { text: "·"; color: Theme.ink4; font.family: Theme.font; font.pixelSize: Theme.tsCaption }
                                                Text { text: model.time; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption }
                                            }
                                        }

                                        // "Signed" badge — Lucide check-ok, no "✓"
                                        Rectangle {
                                            height: 20; radius: Theme.rSm - 4
                                            color: Theme.alpha(Theme.ok, 0.08)
                                            border.color: Theme.alpha(Theme.ok, 0.18); border.width: 1
                                            implicitWidth: sigRow.implicitWidth + 12

                                            RowLayout {
                                                id: sigRow
                                                anchors.centerIn: parent
                                                spacing: 4

                                                Image {
                                                    width: 10; height: 10
                                                    source: "icons/check-ok.svg"
                                                    fillMode: Image.PreserveAspectFit
                                                    smooth: true; mipmap: true
                                                }
                                                Text {
                                                    text: "firmado"
                                                    color: Theme.ok
                                                    font.family: Theme.font
                                                    font.pixelSize: Theme.tsMicro
                                                }
                                            }
                                        }
                                    }

                                    Item { height: Theme.sp2; width: 1 }

                                    Rectangle {
                                        visible: index < 2
                                        Layout.alignment: Qt.AlignRight
                                        Layout.preferredWidth: activityCol.width - 48
                                        Layout.preferredHeight: 1
                                        color: Theme.line; opacity: 0.55
                                    }
                                }
                            }

                            Item { height: 4; width: 1 }
                        }
                    }
                }

                Item { height: Theme.sp4; width: 1 }
            }
        }
    }
}
