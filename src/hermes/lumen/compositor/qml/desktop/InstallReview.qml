import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "."

// ── InstallReview ─────────────────────────────────────────────────────────
// Global modal overlay — shown when daemon signals InstallReviewRequested.
// PASS verdict: auto-dismissed (no modal shown).
// WARN verdict: user can install with acknowledged risk checkbox.
// FAIL verdict: auto-blocked, only dismiss button shown.
//
// Styled as a Sereno LumenModal overlay:
//   enter = scale 0.94→1.0 + scrim fade (gated on Tokens.reduceMotion)
//   semantic colors sourced exclusively from Tokens (no hex literals)
Item {
    id: installReview
    anchors.fill: parent
    visible:      false
    z:            200000

    property string scanId: ""
    property string identifier: ""
    property string kind: ""
    property int    score: 0
    property string verdict: ""          // "PASS" | "WARN" | "FAIL"
    property var    risks: []
    property bool   riskAcknowledged: false

    // Instalación pendiente de confirmación.
    property var    pendingInstall: null
    property bool   infoOnly: false

    // ── Internal helpers (logic preserved verbatim) ──
    function notifyResolved(reqId) { if (reqId) root.installResolved(reqId); }

    function _fireInstall() {
        if (!pendingInstall) return;
        hermes.call(pendingInstall.reqId, pendingInstall.verb,
            JSON.stringify(pendingInstall.args));
        pendingInstall = null;
    }

    function _applyScan(scanData) {
        scanId     = scanData.scan_id   || scanData.scanId || "";
        identifier = scanData.identifier || "Unknown";
        kind       = scanData.kind       || "";
        score      = scanData.score      !== undefined ? scanData.score : -1;
        verdict    = scanData.verdict    || "FAIL";
        risks      = scanData.risks      || [];
        riskAcknowledged = false;
    }

    function _recordDecision(decision) {
        if (!scanId) return;
        hermes.call("sec-decision-" + scanId, "record_install_decision",
            JSON.stringify({
                scan_id: scanId, decision: decision,
                identifier: identifier, kind: kind, score: score,
                verdict: verdict, risks_json: JSON.stringify(risks || [])
            }));
    }

    function openGated(scanData) {
        infoOnly = false;
        _applyScan(scanData);
        if (verdict === "PASS") {
            _recordDecision("installed");
            _fireInstall();
            return;
        }
        visible = true;
    }

    function openInfo(id, scanData) {
        infoOnly       = true;
        pendingInstall = null;
        scanId         = id || scanData.scan_id || "";
        identifier     = scanData.identifier || "Unknown";
        kind           = scanData.kind       || "";
        score          = scanData.score      !== undefined ? scanData.score : -1;
        verdict        = scanData.verdict    || "FAIL";
        risks          = scanData.risks      || [];
        riskAcknowledged = false;
        if (verdict === "PASS") return;
        visible = true;
    }

    // Defensivo: legado open() → camino informativo
    function open(id, scanData) { openInfo(id, scanData); }

    function close(decision) {
        _recordDecision(decision);
        if (!infoOnly) {
            if (decision === "installed") {
                _fireInstall();
            } else {
                if (pendingInstall) notifyResolved(pendingInstall.reqId);
                pendingInstall = null;
            }
        }
        visible          = false;
        riskAcknowledged = false;
        infoOnly         = false;
    }

    // ── Verdict → semantic token helpers ──
    readonly property color _verdictFg: {
        if (verdict === "FAIL") return Tokens.dangerBase;
        if (verdict === "WARN") return Tokens.warnBase;
        return Tokens.successBase;
    }
    readonly property color _verdictBgSubtle: {
        if (verdict === "FAIL") return Tokens.dangerSubtle;
        if (verdict === "WARN") return Tokens.warnSubtle;
        return Tokens.successSubtle;
    }
    readonly property color _verdictBorder: {
        if (verdict === "FAIL") return Qt.rgba(Tokens.dangerBase.r,  Tokens.dangerBase.g,  Tokens.dangerBase.b,  0.35);
        if (verdict === "WARN") return Qt.rgba(Tokens.warnBase.r,    Tokens.warnBase.g,    Tokens.warnBase.b,    0.30);
        return Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.30);
    }

    // ── Scrim ──
    readonly property real _scrimOpacity: visible ? 0.55 : 0.0

    Rectangle {
        anchors.fill: parent
        color:        Tokens.bgVoid
        opacity:      installReview._scrimOpacity

        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation {
                duration:    Tokens.durModal
                easing.type: Easing.OutCubic
            }
        }

        // Absorb clicks — modal is non-dismissible (user must make a decision)
        MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }
    }

    // ── Panel enter/exit animation ──
    readonly property real _panelScale:   visible ? 1.0 : 0.94
    readonly property real _panelOpacity: visible ? 1.0 : 0.0

    // ── Panel card ──
    Rectangle {
        id: panel
        anchors.centerIn: parent
        width:  Math.min(Math.round(480 * root.sf), parent.width - Math.round(Tokens.spXxxl * root.sf) * 2)
        height: Math.min(Math.round(600 * root.sf), parent.height - Math.round(80 * root.sf))
        radius: Math.round(Tokens.radiusLg * root.sf)
        color:  Tokens.bgCard
        clip:   true

        border.width: 1
        border.color: installReview._verdictBorder

        scale:   installReview._panelScale
        opacity: installReview._panelOpacity

        Behavior on scale {
            enabled: !Tokens.reduceMotion
            NumberAnimation {
                duration:    Tokens.durModal
                easing.type: Easing.OutCubic
            }
        }
        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation {
                duration:    Tokens.durModal
                easing.type: Easing.OutCubic
            }
        }

        // Absorb clicks inside panel so they don't reach scrim
        MouseArea { anchors.fill: parent; onClicked: { /* absorb */ } }

        Column {
            anchors.fill: parent
            spacing:      0

            // ── Header ──
            Rectangle {
                width:  parent.width
                height: Math.round(88 * root.sf)
                color:  installReview._verdictBgSubtle
                radius: Math.round(Tokens.radiusLg * root.sf)

                // Square off bottom corners
                Rectangle {
                    anchors.bottom: parent.bottom
                    anchors.left:   parent.left
                    anchors.right:  parent.right
                    height:         Math.round(Tokens.radiusLg * root.sf)
                    color:          installReview._verdictBgSubtle
                }

                // Bottom border separator
                Rectangle {
                    anchors.bottom: parent.bottom
                    anchors.left:   parent.left
                    anchors.right:  parent.right
                    height:         1
                    color:          Tokens.borderSubtle
                }

                RowLayout {
                    anchors {
                        fill:    parent
                        margins: Math.round(Tokens.spXl * root.sf)
                    }
                    spacing: Math.round(Tokens.spLg * root.sf)

                    Column {
                        Layout.fillWidth: true
                        spacing: Math.round(Tokens.spXs * root.sf)

                        Text {
                            text:            installReview.identifier
                            font.family:     Tokens.fontDisplay
                            font.pixelSize:  Math.round(15 * root.sf)
                            font.weight:     Font.Medium
                            color:           Tokens.textPrimary
                            elide:           Text.ElideRight
                            width:           parent.width
                        }

                        Text {
                            text: {
                                if (installReview.verdict === "FAIL")
                                    return "Instalación bloqueada — el análisis de seguridad no pasó";
                                if (installReview.verdict === "WARN")
                                    return "Advertencia de seguridad — revisa antes de instalar";
                                return "Análisis de seguridad superado";
                            }
                            font.family:    Tokens.fontBody
                            font.pixelSize: Math.round(11 * root.sf)
                            color:          installReview._verdictFg
                        }
                    }

                    // Score badge
                    Rectangle {
                        width:  Math.round(54 * root.sf)
                        height: Math.round(54 * root.sf)
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color:  installReview._verdictBgSubtle
                        border.width: 1
                        border.color: installReview._verdictBorder

                        Column {
                            anchors.centerIn: parent
                            spacing:          Math.round(1 * root.sf)

                            Text {
                                anchors.horizontalCenter: parent.horizontalCenter
                                text:            installReview.score >= 0 ? installReview.score.toString() : "?"
                                font.family:     Tokens.fontDisplay
                                font.pixelSize:  Math.round(20 * root.sf)
                                font.weight:     Font.Bold
                                color:           installReview._verdictFg
                            }
                            Text {
                                anchors.horizontalCenter: parent.horizontalCenter
                                text:           installReview.verdict
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(9 * root.sf)
                                font.weight:    Font.DemiBold
                                color:          installReview._verdictFg
                                font.letterSpacing:  0.6
                            }
                        }
                    }
                }
            }

            // ── Risks list (scrollable) ──
            Flickable {
                id: risksFlick
                width:         parent.width
                height:        Math.round(300 * root.sf)
                contentHeight: riskCol.implicitHeight + Math.round(Tokens.spLg * root.sf)
                clip:          true
                boundsBehavior: Flickable.StopAtBounds

                WheelHandler {
                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                    onWheel: (event) => {
                        risksFlick.contentY = Math.max(0,
                            Math.min(Math.max(0, risksFlick.contentHeight - risksFlick.height),
                                     risksFlick.contentY - event.angleDelta.y));
                    }
                }

                ScrollBar.vertical: LumenScrollBar { sf: root.sf }

                Column {
                    id: riskCol
                    width:               parent.width - Math.round(Tokens.spXl * root.sf) * 2
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top:         parent.top
                    anchors.topMargin:   Math.round(Tokens.spMd * root.sf)
                    spacing:             Math.round(Tokens.spSm * root.sf)

                    Text {
                        text:           "RIESGOS"
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(10 * root.sf)
                        font.weight:    Font.DemiBold
                        color:          Tokens.textMuted
                        font.letterSpacing:  0.8
                    }

                    Text {
                        visible:        installReview.risks.length === 0
                        text:           "Sin riesgos específicos registrados."
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(12 * root.sf)
                        color:          Tokens.textMuted
                    }

                    Repeater {
                        model: {
                            var r = installReview.risks.slice();
                            var order = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };
                            r.sort(function(a, b) {
                                return (order[a.severity] || 9) - (order[b.severity] || 9);
                            });
                            return r;
                        }

                        // Risk card
                        Rectangle {
                            width:        riskCol.width
                            height:       riskBody.implicitHeight + Math.round(Tokens.spLg * root.sf)
                            radius:       Math.round(Tokens.radiusSm * root.sf)
                            color:        Tokens.bgSunken
                            border.width: 1
                            border.color: {
                                var s = modelData.severity || "";
                                if (s === "CRITICAL") return Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.50);
                                if (s === "HIGH")     return Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.30);
                                if (s === "MEDIUM")   return Qt.rgba(Tokens.warnBase.r,   Tokens.warnBase.g,   Tokens.warnBase.b,   0.25);
                                return Tokens.borderSubtle;
                            }

                            Column {
                                id: riskBody
                                anchors {
                                    left:    parent.left
                                    right:   parent.right
                                    top:     parent.top
                                    margins: Math.round(Tokens.spMd * root.sf)
                                }
                                spacing: Math.round(Tokens.spXs * root.sf)

                                RowLayout {
                                    width: parent.width

                                    LumenChip {
                                        sf:   root.sf
                                        text: modelData.severity || "?"
                                        tone: {
                                            var s = modelData.severity || "";
                                            if (s === "CRITICAL" || s === "HIGH") return "danger";
                                            if (s === "MEDIUM")                   return "warn";
                                            return "neutral";
                                        }
                                    }

                                    Item { Layout.fillWidth: true }

                                    Text {
                                        text:           modelData.scanner || ""
                                        font.family:    Tokens.fontBody
                                        font.pixelSize: Math.round(9 * root.sf)
                                        color:          Tokens.textMuted
                                    }
                                }

                                Text {
                                    width:          parent.width
                                    text:           modelData.message || "—"
                                    font.family:    Tokens.fontBody
                                    font.pixelSize: Math.round(12 * root.sf)
                                    color:          Tokens.textPrimary
                                    wrapMode:       Text.WordWrap
                                }

                                Text {
                                    visible:        (modelData.evidence || "") !== ""
                                    width:          parent.width
                                    text:           modelData.evidence || ""
                                    font.family:    Tokens.fontMono
                                    font.pixelSize: Math.round(10 * root.sf)
                                    color:          Tokens.textMuted
                                    wrapMode:       Text.WordWrap
                                }
                            }
                        }
                    }
                }
            }

            // ── Footer ──
            Rectangle {
                width:  parent.width
                height: footerCol.implicitHeight + Math.round(Tokens.spXl * root.sf)
                color:  Qt.rgba(0, 0, 0, 0.20)

                Rectangle {
                    anchors.top:   parent.top
                    anchors.left:  parent.left
                    anchors.right: parent.right
                    height:        1
                    color:         Tokens.borderSubtle
                }

                Column {
                    id: footerCol
                    anchors {
                        left:    parent.left
                        right:   parent.right
                        top:     parent.top
                        margins: Math.round(Tokens.spLg * root.sf)
                    }
                    spacing: Math.round(Tokens.spMd * root.sf)

                    // ── WARN: acknowledge checkbox ──
                    Rectangle {
                        visible:      installReview.verdict === "WARN"
                        width:        parent.width
                        height:       Math.round(38 * root.sf)
                        radius:       Math.round(Tokens.radiusSm * root.sf)
                        color:        installReview.riskAcknowledged ? Tokens.warnSubtle
                                                                     : Qt.rgba(1, 1, 1, 0.04)
                        border.width: 1
                        border.color: installReview.riskAcknowledged
                            ? Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.35)
                            : Tokens.borderDefault

                        Behavior on color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }

                        RowLayout {
                            anchors {
                                fill:    parent
                                margins: Math.round(Tokens.spMd * root.sf)
                            }
                            spacing: Math.round(Tokens.spMd * root.sf)

                            // Checkbox box
                            Rectangle {
                                width:  Math.round(18 * root.sf)
                                height: Math.round(18 * root.sf)
                                radius: Math.round(4 * root.sf)
                                color:  installReview.riskAcknowledged ? Tokens.warnBase
                                                                       : Qt.rgba(1, 1, 1, 0.08)
                                border.width: 1
                                border.color: installReview.riskAcknowledged
                                    ? Tokens.warnBase
                                    : Tokens.borderStrong

                                Behavior on color {
                                    enabled: !Tokens.reduceMotion
                                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                }

                                Text {
                                    anchors.centerIn: parent
                                    text:    "✓"
                                    font.pixelSize: Math.round(11 * root.sf)
                                    font.weight:    Font.Bold
                                    color:   Tokens.textOnAccent
                                    visible: installReview.riskAcknowledged
                                }
                            }

                            Text {
                                Layout.fillWidth: true
                                text:           "Entiendo los riesgos y acepto la responsabilidad"
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(12 * root.sf)
                                color:          Tokens.textPrimary
                                wrapMode:       Text.WordWrap
                            }
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape:  Qt.PointingHandCursor
                            onClicked:    installReview.riskAcknowledged = !installReview.riskAcknowledged
                        }
                    }

                    // ── Buttons row — preserved: close("cancelled"), close("installed"), close("blocked") ──
                    RowLayout {
                        width:   parent.width
                        spacing: Math.round(Tokens.spSm * root.sf)

                        // WARN: Cancel (safe default)
                        LumenButton {
                            visible:     installReview.verdict === "WARN"
                            Layout.fillWidth: true
                            sf:          root.sf
                            label:       "Cancelar"
                            variant:     "secondary"
                            onClicked:   installReview.close("cancelled")
                        }

                        // WARN: Install anyway (requires acknowledge)
                        LumenButton {
                            visible:     installReview.verdict === "WARN"
                            Layout.fillWidth: true
                            sf:          root.sf
                            label:       "Instalar de todos modos"
                            variant:     "danger"
                            enabled:     installReview.riskAcknowledged
                            onClicked:   installReview.close("installed")
                        }

                        // FAIL: Close only
                        LumenButton {
                            visible:     installReview.verdict === "FAIL"
                            Layout.fillWidth: true
                            sf:          root.sf
                            label:       "Cerrar"
                            variant:     "danger"
                            onClicked:   installReview.close("blocked")
                        }
                    }
                }
            }
        }
    }
}
