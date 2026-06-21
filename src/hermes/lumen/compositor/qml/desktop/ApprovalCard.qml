import QtQuick
import QtQuick.Layouts
import "."

// ── ApprovalCard ──────────────────────────────────────────────────────────────
// Overlay flotante — muestra la primera acción HIGH-risk pendiente de aprobación.
// Sondea list_pending cada 2 s mientras el escritorio está activo.
//
// Contrato D-Bus (manejado en onResult con prefijo "pend-"):
//   list_pending   → JSON array [{proposal_id, tool_name, justification, risk}]
//   approve        → {proposal_id}
//   reject         → {proposal_id, reason}
//
// Reskin Sereno 2026-06-14:
//   - Todos los colores usan Tokens (sin hex excepto textOnAccent oscuro "#0B0C0F").
//   - Entrada scale 0.94→1.0 + fade, gateado en !Tokens.reduceMotion.
//   - Scroll de justificación si el texto es largo.
//   - Lógica (hermes.call, señales, Connections, doApprove/doReject) intacta.
// z: 100001 — engancha en main.qml después de teachingOverlay.
Item {
    id: approvalCard

    // ── Estado ──────────────────────────────────────────────────────────────
    property var pendingProposal: null
    // `visible` es FINAL en Item (Qt6) — se BINDEA, no se redeclara.
    visible: pendingProposal !== null

    // ── Mapeo tool_name → texto legible ─────────────────────────────────────
    function toolLabel(toolName) {
        var map = {
            "delete_file":          "borrar un fichero",
            "remove_file":          "borrar un fichero",
            "run_command":          "ejecutar un comando en terminal",
            "run_terminal":         "ejecutar un comando en terminal",
            "execute_command":      "ejecutar un comando en terminal",
            "install_package":      "instalar software",
            "install_software":     "instalar software",
            "begin_computer_use":   "controlar tu pantalla (ratón/teclado)",
            "computer_use":         "controlar tu pantalla (ratón/teclado)",
            "take_screenshot":      "capturar tu pantalla",
            "screenshot":           "capturar tu pantalla",
            "type_in_app":          "interactuar con una app",
            "click_app_element":    "interactuar con una app",
            "write_file":           "escribir en un fichero"
        }
        return map[toolName] || toolName
    }

    // ── Sondeo cada 2 s ─────────────────────────────────────────────────────
    Timer {
        id: pendingPollTimer
        interval: 2000
        repeat: true
        running: root.loggedIn
        onTriggered: {
            hermes.call("pend-list", "list_hitl_pending", "{}")
        }
    }

    // ── Acciones ─────────────────────────────────────────────────────────────
    function doApprove() {
        if (!pendingProposal) return
        hermes.call("pend-approve", "approve",
                    JSON.stringify({ proposal_id: pendingProposal.proposal_id }))
        pendingProposal = null
        pendingPollTimer.restart()
    }

    function doReject() {
        if (!pendingProposal) return
        hermes.call("pend-reject", "reject",
                    JSON.stringify({
                        proposal_id: pendingProposal.proposal_id,
                        reason: "denegado por el usuario"
                    }))
        pendingProposal = null
        pendingPollTimer.restart()
    }

    // ── Manejador de respuestas D-Bus ────────────────────────────────────────
    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "pend-list") {
                if (!ok || !jsonStr) return
                try {
                    var arr = JSON.parse(jsonStr)
                    if (Array.isArray(arr) && arr.length > 0) {
                        approvalCard.pendingProposal = arr[0]
                    } else {
                        approvalCard.pendingProposal = null
                    }
                } catch (e) {
                    approvalCard.pendingProposal = null
                }
                return
            }
            if (reqId === "pend-approve" || reqId === "pend-reject") {
                hermes.call("pend-list", "list_hitl_pending", "{}")
            }
        }
    }

    // ── Tarjeta visual ───────────────────────────────────────────────────────
    Rectangle {
        id: card
        visible: approvalCard.pendingProposal !== null

        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Math.round(110 * root.sf)

        width: Math.min(
            parent.width - Math.round(48 * root.sf),
            Math.round(520 * root.sf)
        )
        height: cardCol.implicitHeight + Math.round(Tokens.spXl * root.sf)

        radius: Math.round(Tokens.radiusLg * root.sf)
        color: Tokens.bgCard
        border.width: 1
        border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.55)

        // Outer glow ring — reinforces "action required"
        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            radius: parent.radius + 1
            color: "transparent"
            border.width: 1
            border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.15)
            z: -1
        }

        // Entrance: fade + scale spring
        opacity: approvalCard.pendingProposal !== null ? 1.0 : 0.0
        scale:   approvalCard.pendingProposal !== null ? 1.0 : 0.94

        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutCubic }
        }
        Behavior on scale {
            enabled: !Tokens.reduceMotion
            NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutBack; easing.overshoot: Tokens.springOvershoot }
        }

        Column {
            id: cardCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Math.round(Tokens.spLg * root.sf)
            spacing: Math.round(Tokens.spMd * root.sf)

            // ── Header ────────────────────────────────────────────────────────
            Row {
                width: parent.width
                spacing: Math.round(Tokens.spSm * root.sf)

                // Warning icon badge
                Rectangle {
                    width: Math.round(32 * root.sf)
                    height: Math.round(32 * root.sf)
                    radius: Math.round(Tokens.radiusSm * root.sf)
                    color: Tokens.warnSubtle
                    border.width: 1
                    border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.25)
                    anchors.verticalCenter: parent.verticalCenter

                    Text {
                        anchors.centerIn: parent
                        text: "⚠"
                        font.pixelSize: Math.round(15 * root.sf)
                        font.family: Tokens.fontBody
                        color: Tokens.warnBase
                    }
                }

                Column {
                    width: parent.width - Math.round(42 * root.sf)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Math.round(3 * root.sf)

                    Text {
                        text: "Hermes solicita permiso"
                        font.pixelSize: Math.round(13 * root.sf)
                        font.weight: Font.DemiBold
                        font.family: Tokens.fontBody
                        color: Tokens.textPrimary
                    }

                    // Risk badge
                    LumenChip {
                        sf: root.sf
                        text: approvalCard.pendingProposal
                              ? (approvalCard.pendingProposal.risk || "HIGH").toUpperCase()
                              : "HIGH"
                        tone: "warn"
                    }
                }
            }

            // ── Action description ────────────────────────────────────────────
            Rectangle {
                width: parent.width
                // Scrollable if justification is long — max 120px then scrolls
                height: Math.min(actionFlick.contentHeight + Math.round(Tokens.spLg * root.sf),
                                 Math.round(120 * root.sf))
                radius: Math.round(Tokens.radiusMd * root.sf)
                color: Tokens.warnSubtle
                border.width: 1
                border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.18)
                clip: true

                Flickable {
                    id: actionFlick
                    anchors.fill: parent
                    anchors.margins: Math.round(Tokens.spMd * root.sf)
                    contentHeight: actionCol.implicitHeight
                    boundsBehavior: Flickable.StopAtBounds
                    clip: true

                    Column {
                        id: actionCol
                        width: parent.width
                        spacing: Math.round(Tokens.spXs * root.sf)

                        Text {
                            width: parent.width
                            text: "Quiere: " + (approvalCard.pendingProposal
                                  ? approvalCard.toolLabel(approvalCard.pendingProposal.tool_name)
                                  : "")
                            font.pixelSize: Math.round(13 * root.sf)
                            font.weight: Font.Medium
                            font.family: Tokens.fontBody
                            color: Tokens.textPrimary
                            wrapMode: Text.WordWrap
                        }

                        Text {
                            width: parent.width
                            visible: {
                                var j = approvalCard.pendingProposal
                                        ? (approvalCard.pendingProposal.justification || "")
                                        : ""
                                return j.length > 0
                            }
                            text: approvalCard.pendingProposal
                                  ? (approvalCard.pendingProposal.justification || "")
                                  : ""
                            font.pixelSize: Math.round(11 * root.sf)
                            font.family: Tokens.fontBody
                            color: Tokens.textSecondary
                            wrapMode: Text.WordWrap
                        }
                    }
                }
            }

            // ── Buttons ───────────────────────────────────────────────────────
            Row {
                width: parent.width
                spacing: Math.round(Tokens.spSm * root.sf)
                layoutDirection: Qt.RightToLeft

                // Aprobar — amber primary
                LumenButton {
                    sf: root.sf
                    label: "Aprobar"
                    variant: "primary"
                    implicitWidth: Math.round(110 * root.sf)
                    implicitHeight: Math.round(36 * root.sf)
                    onClicked: approvalCard.doApprove()
                }

                // Denegar — danger secondary
                LumenButton {
                    sf: root.sf
                    label: "Denegar"
                    variant: "danger"
                    implicitWidth: Math.round(110 * root.sf)
                    implicitHeight: Math.round(36 * root.sf)
                    onClicked: approvalCard.doReject()
                }
            }
        }
    }
}
