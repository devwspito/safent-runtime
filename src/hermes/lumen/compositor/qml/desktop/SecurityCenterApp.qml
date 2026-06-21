import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

Rectangle {
    id: app
    anchors.fill: parent
    color: Tokens.bgSurface
    radius: Math.round(Tokens.radiusLg * sf)
    clip: true

    readonly property real sf: root.sf
    readonly property bool _compact: app.width < Tokens.bpCompact * root.sf

    // ── State ──
    property var recentScans: []
    property bool scansLoading: true
    property var policy: ({})
    property bool policyLoading: true
    property bool policySaving: false
    property string note: ""
    property int activeTab: 0          // 0=Activity 1=Policy 2=ActiveScan
    property var selectedScan: null    // scan shown in right-drawer
    property bool drawerOpen: false

    // Policy form fields (synced from policy object)
    property bool policyAutoBlock: true
    property bool policyAskOnWarn: true
    property int  policyPassThreshold: 70
    property int  policyWarnThreshold: 40
    property bool policyCheckCve: true
    property bool policyCheckProvenance: true
    property bool policyCheckMcpLint: true
    property bool policyCheckSignature: true

    // ── Helpers ──
    function verdictTone(v) {
        if (v === "PASS") return "success"
        if (v === "WARN") return "warn"
        if (v === "FAIL") return "danger"
        return "neutral"
    }
    function verdictColor(v) {
        if (v === "PASS") return Tokens.successBase
        if (v === "WARN") return Tokens.warnBase
        if (v === "FAIL") return Tokens.dangerBase
        return Tokens.textMuted
    }
    function severityColor(s) {
        if (s === "CRITICAL" || s === "HIGH") return Tokens.dangerBase
        if (s === "MEDIUM") return Tokens.warnBase
        return Tokens.textMuted
    }
    function severityBorderColor(s) {
        if (s === "CRITICAL" || s === "HIGH")
            return Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.30)
        if (s === "MEDIUM")
            return Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.25)
        return Tokens.borderSubtle
    }
    function kindTone(k) {
        if (k === "mcp_server") return "info"
        if (k === "skill")      return "success"
        return "neutral"
    }
    function scoreColor(s) {
        if (s >= 70) return Tokens.successBase
        if (s >= 40) return Tokens.warnBase
        return Tokens.dangerBase
    }
    function scoreSubtle(s) {
        if (s >= 70) return Tokens.successSubtle
        if (s >= 40) return Tokens.warnSubtle
        return Tokens.dangerSubtle
    }
    function relTime(ts) {
        if (!ts) return "—"
        var diff = Math.floor((Date.now() - ts * 1000) / 1000)
        if (diff < 60)    return diff + "s ago"
        if (diff < 3600)  return Math.floor(diff / 60) + "m ago"
        if (diff < 86400) return Math.floor(diff / 3600) + "h ago"
        return Math.floor(diff / 86400) + "d ago"
    }

    // ── D-Bus / backend calls (preserved verbatim) ──
    function loadScans() {
        scansLoading = true;
        hermes.call("sec-scans", "list_recent_scans", JSON.stringify({ limit: 50 }));
    }

    function loadPolicy() {
        policyLoading = true;
        hermes.call("sec-policy-get", "get_security_policy", "{}");
    }

    function savePolicy() {
        policySaving = true;
        var p = {
            auto_block_fail: policyAutoBlock,
            ask_on_warn: policyAskOnWarn,
            pass_threshold: policyPassThreshold,
            warn_threshold: policyWarnThreshold,
            scanners: {
                cve: policyCheckCve,
                provenance: policyCheckProvenance,
                mcp_lint: policyCheckMcpLint,
                signature: policyCheckSignature
            }
        };
        hermes.call("sec-policy-set", "set_security_policy", JSON.stringify({ policy_json: JSON.stringify(p) }));
    }

    function applyPolicy(p) {
        if (!p) return;
        policy = p;
        policyAutoBlock  = (p.auto_block_fail !== undefined) ? !!p.auto_block_fail  : true;
        policyAskOnWarn  = (p.ask_on_warn     !== undefined) ? !!p.ask_on_warn      : true;
        policyPassThreshold = (p.pass_threshold !== undefined) ? p.pass_threshold   : 70;
        policyWarnThreshold = (p.warn_threshold !== undefined) ? p.warn_threshold   : 40;
        var sc = p.scanners || {};
        policyCheckCve        = (sc.cve        !== undefined) ? !!sc.cve        : true;
        policyCheckProvenance = (sc.provenance  !== undefined) ? !!sc.provenance : true;
        policyCheckMcpLint    = (sc.mcp_lint    !== undefined) ? !!sc.mcp_lint   : true;
        policyCheckSignature  = (sc.signature   !== undefined) ? !!sc.signature  : true;
    }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "sec-scans") {
                scansLoading = false;
                if (ok) {
                    try { recentScans = JSON.parse(jsonStr) || []; } catch(e) { recentScans = []; }
                }
            } else if (reqId === "sec-policy-get") {
                policyLoading = false;
                if (ok) {
                    try { applyPolicy(JSON.parse(jsonStr)); } catch(e) {}
                }
            } else if (reqId === "sec-policy-set") {
                policySaving = false;
                var rc = {}; try { rc = JSON.parse(jsonStr || "{}"); } catch(e) {}
                note = (ok && rc.ok) ? "Policy saved." : ("Failed to save policy" + (rc.error ? ": " + rc.error : ""));
                noteTimer.restart();
            }
        }
    }

    Timer { id: noteTimer; interval: 3000; onTriggered: note = "" }

    Component.onCompleted: { loadScans(); loadPolicy(); }

    // ─────────────────────────────────────────────
    // TAB BAR
    // ─────────────────────────────────────────────
    Rectangle {
        id: tabBar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: Math.round(44 * sf)
        color: Tokens.bgVoid
        radius: Math.round(Tokens.radiusLg * sf)

        // Fill bottom-corners so only top corners are rounded
        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            height: parent.radius
            color: parent.color
        }

        // Bottom divider
        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: Tokens.borderSubtle
        }

        Row {
            anchors.left: parent.left
            anchors.leftMargin: Math.round(Tokens.spLg * sf)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Math.round(Tokens.spXs * sf)

            Repeater {
                model: ["Activity", "Policy", "Active Scan"]

                Rectangle {
                    height: Math.round(30 * sf)
                    width: tabLabel.implicitWidth + Math.round(Tokens.spLg * sf)
                    radius: Math.round(Tokens.radiusSm * sf)
                    color: activeTab === index
                        ? Tokens.accentSubtle
                        : tabMa.containsMouse ? Qt.rgba(1, 1, 1, 0.05) : "transparent"
                    border.width: activeTab === index ? 1 : 0
                    border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.30)

                    Behavior on color {
                        enabled: !Tokens.reduceMotion
                        ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                    }

                    Text {
                        id: tabLabel
                        anchors.centerIn: parent
                        text: modelData
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(12 * sf)
                        font.weight: activeTab === index ? Font.DemiBold : Font.Normal
                        color: activeTab === index ? Tokens.accentBase : Tokens.textSecondary
                    }

                    MouseArea {
                        id: tabMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: activeTab = index
                    }
                }
            }
        }
    }

    // ─────────────────────────────────────────────
    // BODY
    // ─────────────────────────────────────────────
    Item {
        anchors.top: tabBar.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom

        // ══════════════════════════════════════════
        // TAB 0 — ACTIVITY
        // ══════════════════════════════════════════
        Item {
            anchors.fill: parent
            visible: activeTab === 0

            // Scan list pane — shrinks right on normal width when drawer open;
            // on compact, the drawer stacks below (scan list takes top half)
            Item {
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.bottom: app._compact && drawerOpen ? parent.verticalCenter : parent.bottom
                anchors.right: parent.right
                anchors.topMargin: Math.round(Tokens.spMd * sf)
                anchors.leftMargin: Math.round(Tokens.spMd * sf)
                anchors.bottomMargin: Math.round(Tokens.spMd * sf)
                anchors.rightMargin: (!app._compact && drawerOpen)
                    ? Math.round(320 * sf)
                    : Math.round(Tokens.spMd * sf)

                Behavior on anchors.rightMargin {
                    enabled: !Tokens.reduceMotion
                    NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutCubic }
                }

                // Loading state
                Text {
                    visible: scansLoading
                    anchors.centerIn: parent
                    text: "Loading…"
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(13 * sf)
                    color: Tokens.textMuted
                }

                // Empty state
                Text {
                    visible: !scansLoading && recentScans.length === 0
                    anchors.centerIn: parent
                    text: "No security scans recorded yet."
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(13 * sf)
                    color: Tokens.textMuted
                }

                // Scan list
                Flickable {
                    id: scansFlick
                    visible: !scansLoading && recentScans.length > 0
                    anchors.fill: parent
                    contentHeight: scanCol.height
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds

                    ScrollBar.vertical: LumenScrollBar { sf: app.sf }

                    WheelHandler {
                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                        onWheel: function(event) {
                            scansFlick.contentY = Math.max(0, Math.min(
                                Math.max(0, scansFlick.contentHeight - scansFlick.height),
                                scansFlick.contentY - event.angleDelta.y
                            ))
                        }
                    }

                    Column {
                        id: scanCol
                        width: parent.width
                        spacing: Math.round(Tokens.spXs * sf)

                        Repeater {
                            model: recentScans

                            Rectangle {
                                id: scanRow
                                width: scanCol.width
                                height: Math.round(52 * sf)
                                radius: Math.round(Tokens.radiusSm * sf)
                                color: scanRowMa.containsMouse
                                    ? Tokens.bgElevated
                                    : selectedScan && selectedScan.scan_id === modelData.scan_id
                                        ? Tokens.accentSubtle
                                        : Qt.rgba(1, 1, 1, 0.02)
                                border.width: selectedScan && selectedScan.scan_id === modelData.scan_id ? 1 : 0
                                border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.25)

                                // Stagger fade-in
                                opacity: 0.0
                                Component.onCompleted: {
                                    if (Tokens.reduceMotion) {
                                        scanRow.opacity = 1.0
                                    } else {
                                        fadeInDelay.start()
                                    }
                                }
                                Timer {
                                    id: fadeInDelay
                                    interval: Math.min(index * 30, 300)
                                    onTriggered: fadeInAnim.start()
                                }
                                NumberAnimation {
                                    id: fadeInAnim
                                    target: scanRow
                                    property: "opacity"
                                    from: 0.0; to: 1.0
                                    duration: Tokens.durBase
                                    easing.type: Easing.OutCubic
                                }

                                Behavior on color {
                                    enabled: !Tokens.reduceMotion
                                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                }

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: Math.round(Tokens.spMd * sf)
                                    anchors.rightMargin: Math.round(Tokens.spMd * sf)
                                    spacing: Math.round(Tokens.spSm * sf)

                                    // Kind chip
                                    LumenChip {
                                        sf: app.sf
                                        text: modelData.kind || "?"
                                        tone: kindTone(modelData.kind || "")
                                        Layout.alignment: Qt.AlignVCenter
                                    }

                                    // Identifier
                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.identifier || "unknown"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(12 * sf)
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                        elide: Text.ElideRight
                                    }

                                    // Score badge
                                    Rectangle {
                                        width: Math.round(40 * sf)
                                        height: Math.round(22 * sf)
                                        radius: Math.round(Tokens.radiusSm * sf)
                                        color: scoreSubtle(modelData.score !== undefined ? modelData.score : -1)
                                        Layout.alignment: Qt.AlignVCenter

                                        Text {
                                            anchors.centerIn: parent
                                            text: modelData.score !== undefined ? modelData.score : "?"
                                            font.family: Tokens.fontDisplay
                                            font.pixelSize: Math.round(10 * sf)
                                            font.weight: Font.Bold
                                            color: scoreColor(modelData.score !== undefined ? modelData.score : -1)
                                        }
                                    }

                                    // Verdict chip
                                    LumenChip {
                                        sf: app.sf
                                        text: modelData.verdict || "—"
                                        tone: verdictTone(modelData.verdict || "")
                                        Layout.alignment: Qt.AlignVCenter
                                    }

                                    // Decision
                                    Text {
                                        text: modelData.decision || "—"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(10 * sf)
                                        color: Tokens.textMuted
                                        width: Math.round(70 * sf)
                                        elide: Text.ElideRight
                                    }

                                    // Relative timestamp
                                    Text {
                                        text: relTime(modelData.timestamp)
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(10 * sf)
                                        color: Tokens.textMuted
                                    }
                                }

                                MouseArea {
                                    id: scanRowMa
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        selectedScan = modelData
                                        drawerOpen = true
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // ── Detail drawer — right panel (normal) or bottom half (compact) ──
            Rectangle {
                id: scanDrawer
                visible: drawerOpen
                // Normal: right-side panel; Compact: spans full width in bottom half
                anchors.top: app._compact ? parent.verticalCenter : parent.top
                anchors.bottom: parent.bottom
                anchors.right: parent.right
                // In compact we stretch left-to-right; in normal we use fixed width
                width: app._compact ? (parent.width - Math.round(Tokens.spSm * sf) * 2) : Math.round(308 * sf)
                anchors.topMargin: Math.round(Tokens.spSm * sf)
                anchors.bottomMargin: Math.round(Tokens.spSm * sf)
                anchors.rightMargin: Math.round(Tokens.spSm * sf)
                color: Tokens.bgCard
                border.width: 1
                border.color: Tokens.borderSubtle
                radius: Math.round(Tokens.radiusMd * sf)
                clip: true

                // Slide-in (right→left) only on normal width
                property real _targetX: drawerOpen ? 0 : Math.round(320 * sf)
                x: app._compact ? (Math.round(Tokens.spSm * sf)) : _targetX
                Behavior on x {
                    enabled: !Tokens.reduceMotion && !app._compact
                    NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutCubic }
                }

                Column {
                    anchors.fill: parent
                    anchors.margins: Math.round(14 * sf)
                    spacing: Math.round(Tokens.spSm * sf)

                    // Drawer header
                    RowLayout {
                        width: parent.width

                        Column {
                            Layout.fillWidth: true
                            spacing: Math.round(3 * sf)

                            Text {
                                text: selectedScan ? (selectedScan.identifier || "—") : "—"
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(13 * sf)
                                font.weight: Font.DemiBold
                                color: Tokens.textPrimary
                                elide: Text.ElideRight
                                width: parent.width
                            }

                            LumenChip {
                                sf: app.sf
                                text: selectedScan ? ("Verdict: " + (selectedScan.verdict || "—")) : "—"
                                tone: selectedScan ? verdictTone(selectedScan.verdict || "") : "neutral"
                                visible: selectedScan !== null
                            }
                        }

                        // Close button
                        Rectangle {
                            width: Math.round(24 * sf)
                            height: Math.round(24 * sf)
                            radius: Math.round(12 * sf)
                            color: drawerCloseMa.containsMouse
                                ? Qt.rgba(1, 1, 1, 0.10)
                                : "transparent"

                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast }
                            }

                            Text {
                                anchors.centerIn: parent
                                text: "✕"
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                color: Tokens.textMuted
                            }
                            MouseArea {
                                id: drawerCloseMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: drawerOpen = false
                            }
                        }
                    }

                    // Divider
                    Rectangle {
                        width: parent.width
                        height: 1
                        color: Tokens.borderSubtle
                    }

                    Text {
                        text: "Risks"
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        font.weight: Font.DemiBold
                        color: Tokens.textSecondary
                        font.letterSpacing: 0.5
                    }

                    Flickable {
                        id: risksFlick
                        width: parent.width
                        height: scanDrawer.height
                              - Math.round(14 * sf) * 2   // drawer margins
                              - Math.round(30 * sf)        // header
                              - Math.round(10 * sf)        // spacing
                              - 1                          // divider
                              - Math.round(16 * sf)        // "Risks" label
                        contentHeight: riskCol.height
                        clip: true
                        boundsBehavior: Flickable.StopAtBounds

                        ScrollBar.vertical: LumenScrollBar { sf: app.sf }

                        WheelHandler {
                            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                            onWheel: function(event) {
                                risksFlick.contentY = Math.max(0, Math.min(
                                    Math.max(0, risksFlick.contentHeight - risksFlick.height),
                                    risksFlick.contentY - event.angleDelta.y
                                ))
                            }
                        }

                        Column {
                            id: riskCol
                            width: parent.width
                            spacing: Math.round(Tokens.spSm * sf)

                            Repeater {
                                model: {
                                    if (!selectedScan || !selectedScan.risks) return []
                                    var r = selectedScan.risks.slice()
                                    var order = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }
                                    r.sort(function(a, b) {
                                        return (order[a.severity] || 9) - (order[b.severity] || 9)
                                    })
                                    return r
                                }

                                Rectangle {
                                    width: riskCol.width
                                    height: riskContent.height + Math.round(Tokens.spLg * sf)
                                    radius: Math.round(Tokens.radiusSm * sf)
                                    color: Qt.rgba(1, 1, 1, 0.03)
                                    border.width: 1
                                    border.color: severityBorderColor(modelData.severity || "")

                                    Column {
                                        id: riskContent
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        anchors.margins: Math.round(Tokens.spSm * sf)
                                        spacing: Math.round(Tokens.spXs * sf)

                                        RowLayout {
                                            width: parent.width

                                            Text {
                                                text: modelData.severity || "?"
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(9 * sf)
                                                font.weight: Font.Bold
                                                color: severityColor(modelData.severity || "")
                                                font.letterSpacing: 0.6
                                            }
                                            Item { Layout.fillWidth: true }
                                            Text {
                                                text: modelData.scanner || ""
                                                font.family: Tokens.fontBody
                                                font.pixelSize: Math.round(9 * sf)
                                                color: Tokens.textMuted
                                            }
                                        }

                                        Text {
                                            width: parent.width
                                            text: modelData.message || "—"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * sf)
                                            color: Tokens.textPrimary
                                            wrapMode: Text.WordWrap
                                        }

                                        Text {
                                            visible: (modelData.evidence || "") !== ""
                                            width: parent.width
                                            text: modelData.evidence || ""
                                            font.family: Tokens.fontMono
                                            font.pixelSize: Math.round(10 * sf)
                                            color: Tokens.textMuted
                                            wrapMode: Text.WordWrap
                                        }
                                    }
                                }
                            }

                            // Empty risks state
                            Text {
                                visible: !selectedScan || !selectedScan.risks || selectedScan.risks.length === 0
                                width: parent.width
                                text: "No risks recorded."
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(12 * sf)
                                color: Tokens.textMuted
                                horizontalAlignment: Text.AlignHCenter
                            }
                        }
                    }
                }
            }
        }

        // ══════════════════════════════════════════
        // TAB 1 — POLICY
        // ══════════════════════════════════════════
        Item {
            anchors.fill: parent
            visible: activeTab === 1

            Flickable {
                id: policyFlick
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spLg * sf)
                contentHeight: policyCol.height + Math.round(Tokens.spLg * sf)
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                ScrollBar.vertical: LumenScrollBar { sf: app.sf }

                WheelHandler {
                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                    onWheel: function(event) {
                        policyFlick.contentY = Math.max(0, Math.min(
                            Math.max(0, policyFlick.contentHeight - policyFlick.height),
                            policyFlick.contentY - event.angleDelta.y
                        ))
                    }
                }

                Column {
                    id: policyCol
                    width: parent.width
                    spacing: Math.round(Tokens.spLg * sf)

                    // Loading
                    Text {
                        text: "Loading policy…"
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(12 * sf)
                        color: Tokens.textMuted
                        visible: policyLoading
                    }

                    // ── Section: Behavior ──
                    Column {
                        visible: !policyLoading
                        width: parent.width
                        spacing: Math.round(Tokens.spSm * sf)

                        Text {
                            text: "BEHAVIOR"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(10 * sf)
                            font.weight: Font.DemiBold
                            color: Tokens.textMuted
                            font.letterSpacing: 0.8
                        }

                        // Auto-block FAIL row
                        Rectangle {
                            width: parent.width
                            height: Math.round(52 * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: Qt.rgba(1, 1, 1, 0.03)
                            border.width: 1
                            border.color: Tokens.borderSubtle

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: Math.round(Tokens.spMd * sf)

                                Column {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    Text {
                                        text: "Auto-block FAIL"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(12 * sf)
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                    }
                                    Text {
                                        text: "Automatically block installs that fail security check"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(10 * sf)
                                        color: Tokens.textMuted
                                    }
                                }

                                LumenSwitch {
                                    sf: app.sf
                                    checked: policyAutoBlock
                                    onToggled: function(v) { policyAutoBlock = v }
                                }
                            }
                        }

                        // Confirm on WARN row
                        Rectangle {
                            width: parent.width
                            height: Math.round(52 * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: Qt.rgba(1, 1, 1, 0.03)
                            border.width: 1
                            border.color: Tokens.borderSubtle

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: Math.round(Tokens.spMd * sf)

                                Column {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    Text {
                                        text: "Confirm on WARN"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(12 * sf)
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                    }
                                    Text {
                                        text: "Show review dialog when verdict is WARN"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(10 * sf)
                                        color: Tokens.textMuted
                                    }
                                }

                                LumenSwitch {
                                    sf: app.sf
                                    checked: policyAskOnWarn
                                    onToggled: function(v) { policyAskOnWarn = v }
                                }
                            }
                        }
                    }

                    // ── Section: Thresholds ──
                    Column {
                        visible: !policyLoading
                        width: parent.width
                        spacing: Math.round(Tokens.spSm * sf)

                        Text {
                            text: "THRESHOLDS"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(10 * sf)
                            font.weight: Font.DemiBold
                            color: Tokens.textMuted
                            font.letterSpacing: 0.8
                        }

                        // PASS threshold row
                        Rectangle {
                            width: parent.width
                            height: Math.round(52 * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: Qt.rgba(1, 1, 1, 0.03)
                            border.width: 1
                            border.color: Tokens.borderSubtle

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: Math.round(Tokens.spMd * sf)
                                spacing: Math.round(Tokens.spMd * sf)

                                Column {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    Text {
                                        text: "PASS threshold"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(12 * sf)
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                    }
                                    Text {
                                        text: "Score ≥ this value = PASS"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(10 * sf)
                                        color: Tokens.textMuted
                                    }
                                }

                                Rectangle {
                                    width: Math.round(52 * sf)
                                    height: Math.round(30 * sf)
                                    radius: Math.round(Tokens.radiusSm * sf)
                                    color: Tokens.bgSunken
                                    border.width: 1
                                    border.color: Tokens.borderDefault

                                    TextInput {
                                        anchors.fill: parent
                                        anchors.margins: Math.round(Tokens.spSm * sf)
                                        text: policyPassThreshold.toString()
                                        font.family: Tokens.fontDisplay
                                        font.pixelSize: Math.round(13 * sf)
                                        font.weight: Font.Bold
                                        color: Tokens.successBase
                                        horizontalAlignment: TextInput.AlignHCenter
                                        inputMethodHints: Qt.ImhDigitsOnly
                                        validator: IntValidator { bottom: 1; top: 100 }
                                        onEditingFinished: policyPassThreshold = parseInt(text) || 70
                                    }
                                }
                            }
                        }

                        // WARN threshold row
                        Rectangle {
                            width: parent.width
                            height: Math.round(52 * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: Qt.rgba(1, 1, 1, 0.03)
                            border.width: 1
                            border.color: Tokens.borderSubtle

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: Math.round(Tokens.spMd * sf)
                                spacing: Math.round(Tokens.spMd * sf)

                                Column {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    Text {
                                        text: "WARN threshold"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(12 * sf)
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                    }
                                    Text {
                                        text: "Score ≥ this value = WARN (below = FAIL)"
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(10 * sf)
                                        color: Tokens.textMuted
                                    }
                                }

                                Rectangle {
                                    width: Math.round(52 * sf)
                                    height: Math.round(30 * sf)
                                    radius: Math.round(Tokens.radiusSm * sf)
                                    color: Tokens.bgSunken
                                    border.width: 1
                                    border.color: Tokens.borderDefault

                                    TextInput {
                                        anchors.fill: parent
                                        anchors.margins: Math.round(Tokens.spSm * sf)
                                        text: policyWarnThreshold.toString()
                                        font.family: Tokens.fontDisplay
                                        font.pixelSize: Math.round(13 * sf)
                                        font.weight: Font.Bold
                                        color: Tokens.warnBase
                                        horizontalAlignment: TextInput.AlignHCenter
                                        inputMethodHints: Qt.ImhDigitsOnly
                                        validator: IntValidator { bottom: 1; top: 100 }
                                        onEditingFinished: policyWarnThreshold = parseInt(text) || 40
                                    }
                                }
                            }
                        }
                    }

                    // ── Section: Scanners ──
                    Column {
                        visible: !policyLoading
                        width: parent.width
                        spacing: Math.round(Tokens.spSm * sf)

                        Text {
                            text: "SCANNERS"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(10 * sf)
                            font.weight: Font.DemiBold
                            color: Tokens.textMuted
                            font.letterSpacing: 0.8
                        }

                        Repeater {
                            model: [
                                { key: "cve",         label: "CVE / Vulnerability" },
                                { key: "provenance",   label: "Provenance" },
                                { key: "mcp_lint",     label: "MCP Lint" },
                                { key: "signature",    label: "Signature" }
                            ]

                            Rectangle {
                                width: policyCol.width
                                height: Math.round(40 * sf)
                                radius: Math.round(Tokens.radiusSm * sf)
                                color: Qt.rgba(1, 1, 1, 0.03)
                                border.width: 1
                                border.color: Tokens.borderSubtle

                                property bool checked: {
                                    if (modelData.key === "cve")        return policyCheckCve
                                    if (modelData.key === "provenance")  return policyCheckProvenance
                                    if (modelData.key === "mcp_lint")    return policyCheckMcpLint
                                    return policyCheckSignature
                                }

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: Math.round(Tokens.spMd * sf)

                                    Text {
                                        text: modelData.label
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(12 * sf)
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                        Layout.fillWidth: true
                                    }

                                    // Custom checkbox using design tokens
                                    Rectangle {
                                        width: Math.round(20 * sf)
                                        height: Math.round(20 * sf)
                                        radius: Math.round(5 * sf)
                                        color: parent.parent.checked ? Tokens.successBase : Qt.rgba(1, 1, 1, 0.08)
                                        border.width: 1
                                        border.color: parent.parent.checked
                                            ? Tokens.successBase
                                            : Tokens.borderDefault

                                        Behavior on color {
                                            enabled: !Tokens.reduceMotion
                                            ColorAnimation { duration: Tokens.durFast }
                                        }

                                        Text {
                                            anchors.centerIn: parent
                                            text: "✓"
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * sf)
                                            font.weight: Font.Bold
                                            color: Tokens.textOnAccent
                                            visible: parent.parent.parent.checked
                                        }

                                        MouseArea {
                                            anchors.fill: parent
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                var k = modelData.key
                                                if (k === "cve")             policyCheckCve        = !policyCheckCve
                                                else if (k === "provenance")  policyCheckProvenance = !policyCheckProvenance
                                                else if (k === "mcp_lint")    policyCheckMcpLint    = !policyCheckMcpLint
                                                else                          policyCheckSignature  = !policyCheckSignature
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // ── Save button + feedback ──
                    Column {
                        visible: !policyLoading
                        width: parent.width
                        spacing: Math.round(Tokens.spSm * sf)

                        // Feedback note
                        Text {
                            visible: note !== ""
                            text: note
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * sf)
                            color: note.indexOf("Failed") >= 0 ? Tokens.dangerBase : Tokens.successBase
                            horizontalAlignment: Text.AlignHCenter
                            width: parent.width
                        }

                        LumenButton {
                            sf: app.sf
                            width: parent.width
                            label: policySaving ? "Saving…" : "Save Policy"
                            variant: "primary"
                            enabled: !policySaving
                            loading: policySaving
                            onClicked: savePolicy()
                        }
                    }
                }
            }
        }

        // ══════════════════════════════════════════
        // TAB 2 — ACTIVE SCAN
        // ══════════════════════════════════════════
        Item {
            id: activeScanTab
            anchors.fill: parent
            visible: activeTab === 2

            // State for the last scan result and on-demand scanner
            property string lastScanId: ""
            property string lastVerdict: ""
            property var    lastScanData: null
            property bool   scanning: false
            property string scanNote: ""

            // Listen to daemon D-Bus signals forwarded by HermesBackend
            Connections {
                target: hermes

                function onScanCompleted(scanId, verdict) {
                    activeScanTab.lastScanId  = scanId
                    activeScanTab.lastVerdict = verdict
                    activeScanTab.scanning    = false
                }

                function onInstallReviewRequested(scanId, scanDataJson) {
                    activeScanTab.lastScanId = scanId
                    activeScanTab.scanning   = false
                    try {
                        activeScanTab.lastScanData = JSON.parse(scanDataJson)
                    } catch(e) {
                        activeScanTab.lastScanData = null
                    }
                }
            }

            // Result handler for on-demand ScanInstall call
            Connections {
                target: hermes

                function onResult(reqId, ok, jsonStr) {
                    if (reqId !== "activescan-run") return
                    activeScanTab.scanning = false
                    if (!ok) {
                        activeScanTab.scanNote = "Error al escanear."
                        return
                    }
                    try {
                        var d = JSON.parse(jsonStr)
                        if (d.error) {
                            activeScanTab.scanNote = d.error
                        } else {
                            activeScanTab.lastScanData = d
                            activeScanTab.lastScanId   = d.scan_id || ""
                            activeScanTab.lastVerdict  = d.verdict || ""
                            activeScanTab.scanNote     = ""
                        }
                    } catch(e) {
                        activeScanTab.scanNote = "Respuesta inesperada del daemon."
                    }
                }
            }

            Flickable {
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spLg * sf)
                contentHeight: activeScanCol.height
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                ScrollBar.vertical: LumenScrollBar { sf: app.sf }

                Column {
                    id: activeScanCol
                    width: parent.width
                    spacing: Math.round(Tokens.spMd * sf)

                    // ── Section title ──
                    Text {
                        text: "Análisis bajo demanda"
                        font.family: Tokens.fontDisplay
                        font.pixelSize: Math.round(15 * sf)
                        font.weight: Font.DemiBold
                        color: Tokens.textPrimary
                        font.letterSpacing: -0.2
                    }

                    // ── Input + scan button row ──
                    Row {
                        width: parent.width
                        spacing: Math.round(Tokens.spSm * sf)

                        LumenInput {
                            id: scanIdentInput
                            sf: app.sf
                            placeholder: "github.com/user/skill-repo"
                            width: parent.width - Math.round(110 * sf) - Math.round(Tokens.spSm * sf)
                        }

                        LumenButton {
                            sf: app.sf
                            width: Math.round(102 * sf)
                            label: activeScanTab.scanning ? "Analizando…" : "Analizar"
                            variant: "primary"
                            enabled: !activeScanTab.scanning && scanIdentInput.text.trim() !== ""
                            loading: activeScanTab.scanning
                            onClicked: {
                                activeScanTab.scanning     = true
                                activeScanTab.scanNote     = ""
                                activeScanTab.lastScanData = null
                                hermes.call("activescan-run", "scan_install",
                                    JSON.stringify({ kind: "skill", identifier: scanIdentInput.text.trim() }))
                            }
                        }
                    }

                    // Error note
                    Text {
                        visible: activeScanTab.scanNote !== ""
                        text: activeScanTab.scanNote
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        color: Tokens.dangerBase
                        wrapMode: Text.WordWrap
                        width: parent.width
                    }

                    // ── Scan result card ──
                    Rectangle {
                        visible: activeScanTab.lastScanData !== null
                        width: parent.width
                        height: resultCardContent.height + Math.round(Tokens.spLg * sf) * 2
                        radius: Math.round(Tokens.radiusMd * sf)
                        color: Tokens.bgCard
                        border.width: 1
                        border.color: {
                            var v = activeScanTab.lastVerdict
                            if (v === "FAIL") return Qt.rgba(Tokens.dangerBase.r,  Tokens.dangerBase.g,  Tokens.dangerBase.b,  0.40)
                            if (v === "WARN") return Qt.rgba(Tokens.warnBase.r,    Tokens.warnBase.g,    Tokens.warnBase.b,    0.40)
                            return Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.35)
                        }

                        // Slide-in on appearance
                        opacity: 0.0
                        onVisibleChanged: {
                            if (visible) {
                                if (Tokens.reduceMotion) {
                                    opacity = 1.0
                                } else {
                                    resultCardFade.start()
                                }
                            }
                        }
                        NumberAnimation {
                            id: resultCardFade
                            target: parent
                            property: "opacity"
                            from: 0.0; to: 1.0
                            duration: Tokens.durBase
                            easing.type: Easing.OutCubic
                        }

                        Column {
                            id: resultCardContent
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.margins: Math.round(Tokens.spLg * sf)
                            spacing: Math.round(Tokens.spSm * sf)

                            // Header: identifier + score badge + verdict chip
                            RowLayout {
                                width: parent.width
                                spacing: Math.round(Tokens.spSm * sf)

                                Text {
                                    Layout.fillWidth: true
                                    text: activeScanTab.lastScanData
                                        ? (activeScanTab.lastScanData.identifier || "—")
                                        : "—"
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(13 * sf)
                                    font.weight: Font.Medium
                                    color: Tokens.textPrimary
                                    elide: Text.ElideRight
                                }

                                // Score
                                Rectangle {
                                    width: Math.round(40 * sf)
                                    height: Math.round(24 * sf)
                                    radius: Math.round(Tokens.radiusSm * sf)
                                    color: scoreSubtle(activeScanTab.lastScanData
                                        ? (activeScanTab.lastScanData.score !== undefined ? activeScanTab.lastScanData.score : -1)
                                        : -1)

                                    Text {
                                        anchors.centerIn: parent
                                        text: activeScanTab.lastScanData
                                            ? String(activeScanTab.lastScanData.score !== undefined
                                                ? activeScanTab.lastScanData.score : "?")
                                            : "?"
                                        font.family: Tokens.fontDisplay
                                        font.pixelSize: Math.round(12 * sf)
                                        font.weight: Font.Bold
                                        color: scoreColor(activeScanTab.lastScanData
                                            ? (activeScanTab.lastScanData.score !== undefined ? activeScanTab.lastScanData.score : -1)
                                            : -1)
                                    }
                                }

                                LumenChip {
                                    sf: app.sf
                                    text: activeScanTab.lastVerdict || "—"
                                    tone: verdictTone(activeScanTab.lastVerdict || "")
                                }
                            }

                            // Divider
                            Rectangle {
                                width: parent.width
                                height: 1
                                color: Tokens.borderSubtle
                                visible: activeScanTab.lastScanData !== null
                                    && activeScanTab.lastScanData.risks
                                    && activeScanTab.lastScanData.risks.length > 0
                            }

                            // Top-5 risks
                            Repeater {
                                model: activeScanTab.lastScanData
                                    ? activeScanTab.lastScanData.risks.slice(0, 5)
                                    : []

                                RowLayout {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * sf)

                                    Text {
                                        text: modelData.severity || ""
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(9 * sf)
                                        font.weight: Font.DemiBold
                                        color: severityColor(modelData.severity || "")
                                        font.letterSpacing: 0.6
                                        Layout.alignment: Qt.AlignTop
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.message || ""
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(11 * sf)
                                        color: Tokens.textSecondary
                                        wrapMode: Text.WordWrap
                                    }
                                }
                            }
                        }
                    }

                    // Idle / hint state
                    Text {
                        visible: !activeScanTab.scanning
                            && activeScanTab.lastScanData === null
                            && activeScanTab.scanNote === ""
                        text: "Introduce un identificador y pulsa Analizar, o espera a que un install active el scanner automáticamente."
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        color: Tokens.textMuted
                        width: parent.width
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
    }
}
