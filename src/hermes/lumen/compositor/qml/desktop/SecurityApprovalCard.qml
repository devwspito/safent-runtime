import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "."

// ── SecurityApprovalCard ──────────────────────────────────────────────────────
// Overlay modal para solicitudes de permiso del Cerebro (Modo Guardado).
// El daemon emite ApprovalRequested(payload_json) por D-Bus; el backend Python
// lo reenvía como hermes.approvalRequested(payloadJson); este componente lo
// recibe a través de main.qml y llama a ResolveApproval vía hermes.call().
//
// Cola: múltiples solicitudes se encolan; se muestra una en cada momento.
// Teclado: Enter = Permitir una vez, Escape = Denegar.
// Fail-safe: si resolve_approval falla el modal se mantiene visible con error;
//            NO se descarta silenciosamente (el comando permanece bloqueado).
//
// Reskin Sereno 2026-06-14:
//   - Toda paleta migrada a Tokens (sin hex arbitrario).
//   - fontMono para el bloque de comando.
//   - Comando largo: Flickable vertical con altura máxima → siempre visible completo.
//   - Entrada modal: scrim fade + scale 0.94→1.0, gateado en !Tokens.reduceMotion.
//   - TODA la lógica (enqueueRequest, _showNext, _resolve, Connections,
//     focusCatcher, Keys, resolving, resolveError) INTACTA sin cambios.
// z: 200100 — por encima de InstallReview y del ApprovalCard de HITL/broker.
Item {
    id: secApproval
    anchors.fill: parent
    visible: currentRequest !== null

    // ── Cola de solicitudes pendientes ───────────────────────────────────────
    property var requestQueue: []
    property var currentRequest: null
    property bool resolving: false
    property string resolveError: ""

    // Llamada desde main.qml cuando llega hermes.approvalRequested(payloadJson)
    function enqueueRequest(payloadJson) {
        var payload;
        try { payload = JSON.parse(payloadJson); }
        catch (e) {
            console.warn("SecurityApprovalCard: payload JSON inválido", e);
            return;
        }
        if (!payload || !payload.request_id) return;
        var q = requestQueue.slice();
        q.push(payload);
        requestQueue = q;
        if (currentRequest === null) _showNext();
    }

    function _showNext() {
        if (requestQueue.length === 0) {
            currentRequest = null;
            return;
        }
        var q = requestQueue.slice();
        currentRequest = q.shift();
        requestQueue = q;
        resolving = false;
        resolveError = "";
        focusCatcher.forceActiveFocus();
    }

    function _resolve(choice) {
        if (!currentRequest || resolving) return;
        resolving = true;
        resolveError = "";
        hermes.call(
            "secapproval-resolve-" + currentRequest.request_id,
            "resolve_approval",
            JSON.stringify({ request_id: currentRequest.request_id, choice: choice })
        );
    }

    // ── Manejador de respuestas D-Bus ────────────────────────────────────────
    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (!reqId.startsWith("secapproval-resolve-")) return;
            resolving = false;
            if (ok) {
                _showNext();
            } else {
                var errMsg = "Error al resolver. Inténtalo de nuevo.";
                try {
                    var parsed = JSON.parse(jsonStr || "{}");
                    if (parsed.error) errMsg = parsed.error;
                } catch (e) {}
                resolveError = errMsg;
            }
        }
    }

    // ── Capturador de foco para keyboard nav ─────────────────────────────────
    Item {
        id: focusCatcher
        anchors.fill: parent
        focus: secApproval.visible
        Keys.onPressed: (event) => {
            if (!secApproval.currentRequest) return;
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                secApproval._resolve("once");
                event.accepted = true;
            } else if (event.key === Qt.Key_Escape) {
                secApproval._resolve("deny");
                event.accepted = true;
            }
        }
    }

    // ── Scrim — no dismissible sin elegir ────────────────────────────────────
    Rectangle {
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.76)
        opacity: secApproval.visible ? 1.0 : 0.0
        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation { duration: Tokens.durModal; easing.type: Easing.OutCubic }
        }
        // Absorber clics: el usuario no puede evadir el modal sin resolver.
        MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }
    }

    // ── Tarjeta modal ─────────────────────────────────────────────────────────
    Rectangle {
        id: modalCard
        anchors.centerIn: parent
        width: Math.round(480 * root.sf)
        // Height driven by content; capped to avoid overflow on small screens
        height: Math.min(
            cardBody.implicitHeight + Math.round(Tokens.spXxl * root.sf),
            parent.height - Math.round(80 * root.sf)
        )

        radius: Math.round(Tokens.radiusLg * root.sf)
        color: Tokens.bgCard
        border.width: 1
        border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.55)
        clip: true

        // Outer glow ring
        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            radius: parent.radius + 1
            color: "transparent"
            border.width: 2
            border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.14)
            z: -1
        }

        // Modal entrance: scrim + scale spring
        opacity: secApproval.visible ? 1.0 : 0.0
        scale:   secApproval.visible ? 1.0 : 0.94
        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation { duration: Tokens.durModal; easing.type: Easing.OutCubic }
        }
        Behavior on scale {
            enabled: !Tokens.reduceMotion
            NumberAnimation { duration: Tokens.durModal; easing.type: Easing.OutBack; easing.overshoot: Tokens.springOvershoot }
        }

        // Scrollable body — handles very long commands + many queue entries
        Flickable {
            id: bodyFlick
            anchors.fill: parent
            contentHeight: cardBody.implicitHeight + Math.round(Tokens.spXxl * root.sf)
            boundsBehavior: Flickable.StopAtBounds
            clip: true

            ScrollBar.vertical: LumenScrollBar { sf: root.sf }

            Column {
                id: cardBody
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Math.round(Tokens.spXl * root.sf)
                spacing: Math.round(Tokens.spLg * root.sf)

                // ── Header ─────────────────────────────────────────────────────
                RowLayout {
                    width: parent.width
                    spacing: Math.round(Tokens.spMd * root.sf)

                    // Alert icon
                    Rectangle {
                        width: Math.round(40 * root.sf)
                        height: Math.round(40 * root.sf)
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color: Tokens.warnSubtle
                        border.width: 1
                        border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.30)
                        Layout.alignment: Qt.AlignTop

                        Text {
                            anchors.centerIn: parent
                            text: "⚠"
                            font.pixelSize: Math.round(20 * root.sf)
                            font.family: Tokens.fontBody
                            color: Tokens.warnBase
                        }
                    }

                    Column {
                        Layout.fillWidth: true
                        spacing: Math.round(Tokens.spXs * root.sf)

                        Text {
                            text: "Permiso requerido"
                            font.pixelSize: Math.round(15 * root.sf)
                            font.weight: Font.Bold
                            font.family: Tokens.fontDisplay
                            color: Tokens.textPrimary
                        }

                        Text {
                            text: "El asistente necesita tu autorización antes de continuar."
                            font.pixelSize: Math.round(11 * root.sf)
                            font.family: Tokens.fontBody
                            color: Tokens.textMuted
                            wrapMode: Text.WordWrap
                            width: parent.width
                        }
                    }
                }

                // Separator
                Rectangle {
                    width: parent.width
                    height: 1
                    color: Tokens.borderSubtle
                }

                // ── Comando (monoespaciado, scrollable si largo) ────────────────
                Column {
                    width: parent.width
                    spacing: Math.round(Tokens.spXs * root.sf)

                    Text {
                        text: "ACCIÓN"
                        font.pixelSize: Math.round(10 * root.sf)
                        font.weight: Font.DemiBold
                        font.family: Tokens.fontBody
                        font.letterSpacing: 0.8
                        color: Tokens.textMuted
                    }

                    // Command block — Flickable so arbitrarily-long commands
                    // never get clipped; max height prevents card overflow.
                    Rectangle {
                        width: parent.width
                        // Clamp height: show up to ~5 mono lines before scrolling
                        height: Math.min(
                            cmdText.implicitHeight + Math.round(Tokens.spLg * root.sf),
                            Math.round(100 * root.sf)
                        )
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color: Tokens.bgSunken
                        border.width: 1
                        border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.22)
                        clip: true

                        Flickable {
                            id: cmdFlick
                            anchors.fill: parent
                            anchors.margins: Math.round(Tokens.spSm * root.sf)
                            contentHeight: cmdText.implicitHeight
                            boundsBehavior: Flickable.StopAtBounds
                            clip: true

                            Text {
                                id: cmdText
                                width: parent.width
                                text: secApproval.currentRequest
                                      ? (secApproval.currentRequest.command || "—")
                                      : "—"
                                font.pixelSize: Math.round(12 * root.sf)
                                font.family: Tokens.fontMono
                                color: Tokens.warnBase
                                wrapMode: Text.WrapAnywhere
                            }
                        }
                    }
                }

                // ── Motivo ─────────────────────────────────────────────────────
                Column {
                    width: parent.width
                    spacing: Math.round(Tokens.spXs * root.sf)
                    visible: {
                        var d = secApproval.currentRequest
                                ? (secApproval.currentRequest.description || "")
                                : "";
                        return d.length > 0;
                    }

                    Text {
                        text: "MOTIVO"
                        font.pixelSize: Math.round(10 * root.sf)
                        font.weight: Font.DemiBold
                        font.family: Tokens.fontBody
                        font.letterSpacing: 0.8
                        color: Tokens.textMuted
                    }

                    Text {
                        width: parent.width
                        text: secApproval.currentRequest
                              ? (secApproval.currentRequest.description || "")
                              : ""
                        font.pixelSize: Math.round(12 * root.sf)
                        font.family: Tokens.fontBody
                        color: Tokens.textSecondary
                        wrapMode: Text.WordWrap
                    }
                }

                // ── Cola restante ───────────────────────────────────────────────
                Rectangle {
                    visible: secApproval.requestQueue.length > 0
                    width: parent.width
                    height: Math.round(28 * root.sf)
                    radius: Math.round(Tokens.radiusSm * root.sf)
                    color: Tokens.infoSubtle
                    border.width: 1
                    border.color: Qt.rgba(Tokens.infoBase.r, Tokens.infoBase.g, Tokens.infoBase.b, 0.20)

                    Text {
                        anchors.centerIn: parent
                        text: secApproval.requestQueue.length + " solicitud"
                              + (secApproval.requestQueue.length > 1 ? "es" : "")
                              + " más en espera"
                        font.pixelSize: Math.round(10 * root.sf)
                        font.family: Tokens.fontBody
                        color: Tokens.infoBase
                    }
                }

                // ── Error de resolución (fail-safe) ────────────────────────────
                Rectangle {
                    visible: secApproval.resolveError.length > 0
                    width: parent.width
                    height: errText.implicitHeight + Math.round(Tokens.spMd * root.sf)
                    radius: Math.round(Tokens.radiusSm * root.sf)
                    color: Tokens.dangerSubtle
                    border.width: 1
                    border.color: Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.30)

                    Text {
                        id: errText
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.margins: Math.round(Tokens.spSm * root.sf)
                        text: "✕  " + secApproval.resolveError
                        font.pixelSize: Math.round(11 * root.sf)
                        font.family: Tokens.fontBody
                        color: Tokens.dangerBase
                        wrapMode: Text.WordWrap
                    }
                }

                // ── Botones principales ────────────────────────────────────────
                RowLayout {
                    width: parent.width
                    spacing: Math.round(Tokens.spSm * root.sf)

                    // Denegar (destructivo)
                    Rectangle {
                        Layout.fillWidth: true
                        height: Math.round(42 * root.sf)
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color: denyMa.containsMouse ? Tokens.dangerSubtle : Qt.rgba(1, 1, 1, 0.04)
                        border.width: 1
                        border.color: denyMa.containsMouse
                                      ? Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.45)
                                      : Tokens.borderDefault
                        opacity: secApproval.resolving ? 0.45 : 1.0
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }

                        Text {
                            anchors.centerIn: parent
                            text: "Denegar"
                            font.pixelSize: Math.round(13 * root.sf)
                            font.weight: Font.DemiBold
                            font.family: Tokens.fontBody
                            color: denyMa.containsMouse ? Tokens.dangerBase : Tokens.textSecondary
                            Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        }

                        MouseArea {
                            id: denyMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            enabled: !secApproval.resolving
                            onClicked: secApproval._resolve("deny")
                        }
                    }

                    // Solo esta sesión
                    Rectangle {
                        Layout.preferredWidth: Math.round(148 * root.sf)
                        height: Math.round(42 * root.sf)
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color: sessionMa.containsMouse ? Tokens.warnSubtle : Qt.rgba(1, 1, 1, 0.03)
                        border.width: 1
                        border.color: sessionMa.containsMouse
                                      ? Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.35)
                                      : Tokens.borderSubtle
                        opacity: secApproval.resolving ? 0.45 : 1.0
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }

                        Text {
                            anchors.centerIn: parent
                            text: "Solo esta sesión"
                            font.pixelSize: Math.round(12 * root.sf)
                            font.weight: Font.DemiBold
                            font.family: Tokens.fontBody
                            color: sessionMa.containsMouse ? Tokens.warnBase : Tokens.textSecondary
                            Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        }

                        MouseArea {
                            id: sessionMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            enabled: !secApproval.resolving
                            onClicked: secApproval._resolve("session")
                        }
                    }

                    // Permitir una vez — primario (Enter)
                    Rectangle {
                        Layout.fillWidth: true
                        height: Math.round(42 * root.sf)
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color: onceMa.containsMouse ? Tokens.accentHover : Tokens.accentBase
                        opacity: secApproval.resolving ? 0.55 : 1.0
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }

                        Text {
                            anchors.centerIn: parent
                            text: secApproval.resolving ? "Enviando…" : "Permitir una vez"
                            font.pixelSize: Math.round(13 * root.sf)
                            font.weight: Font.DemiBold
                            font.family: Tokens.fontBody
                            color: Tokens.textOnAccent
                        }

                        MouseArea {
                            id: onceMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            enabled: !secApproval.resolving
                            onClicked: secApproval._resolve("once")
                        }
                    }
                }

                // ── Permitir siempre — acción permanente, fila secundaria ───────
                RowLayout {
                    width: parent.width
                    spacing: 0

                    Item { Layout.fillWidth: true }

                    Text {
                        text: "Siempre  →"
                        font.pixelSize: Math.round(10 * root.sf)
                        font.family: Tokens.fontBody
                        color: Tokens.textMuted
                    }

                    Rectangle {
                        width: alwaysBtnText.implicitWidth + Math.round(Tokens.spLg * root.sf)
                        height: Math.round(24 * root.sf)
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color: alwaysMa.containsMouse ? Tokens.warnSubtle : "transparent"
                        border.width: 1
                        border.color: alwaysMa.containsMouse
                                      ? Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.35)
                                      : Tokens.borderSubtle
                        opacity: secApproval.resolving ? 0.45 : 1.0
                        Layout.leftMargin: Math.round(Tokens.spXs * root.sf)
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }

                        Text {
                            id: alwaysBtnText
                            anchors.centerIn: parent
                            text: "Permitir siempre"
                            font.pixelSize: Math.round(10 * root.sf)
                            font.weight: Font.DemiBold
                            font.family: Tokens.fontBody
                            color: alwaysMa.containsMouse ? Tokens.warnBase : Tokens.textMuted
                            Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        }

                        MouseArea {
                            id: alwaysMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            enabled: !secApproval.resolving
                            onClicked: secApproval._resolve("always")
                        }
                    }
                }

                // Keyboard hint
                Text {
                    width: parent.width
                    text: "Enter — Permitir una vez   ·   Esc — Denegar"
                    font.pixelSize: Math.round(9 * root.sf)
                    font.family: Tokens.fontBody
                    color: Tokens.textMuted
                    horizontalAlignment: Text.AlignHCenter
                }
            }
        }
    }
}
