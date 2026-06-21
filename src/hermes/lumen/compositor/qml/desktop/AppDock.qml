import QtQuick
import QtQuick.Layouts
import "." // Tokens singleton — required for Tokens.X references

Item {
    id: dock
    // Dock width caps at root.width so it never overflows on narrow screens.
    // On wide screens it sizes to its content (dockBg). The Flickable inside
    // handles the case where content is wider than the available width.
    readonly property real maxDockWidth: root.width - Math.round(16 * root.sf)
    width: Math.min(dockBg.width, maxDockWidth)
    height: Math.round(78 * root.sf)

    // Per-item width shrinks on compact screens so 7 items always fit without scroll
    // if possible; if still too tight the Flickable takes over.
    readonly property real itemW: root.width < Tokens.bpCompact * root.sf
        ? Math.round(48 * root.sf)   // compact: tighter items, labels still visible
        : Math.round(64 * root.sf)   // normal

    // ── Glass dock background (Sereno signature) ──
    LumenGlass {
        id: dockBg
        width: dockRow.width + Math.round(28 * root.sf)
        height: parent.height
        intensity: "panel"
        radius: Math.round(Tokens.radiusLg * root.sf)
    }

    // ── Amber hairline top highlight (override LumenGlass default to be centered) ──
    Rectangle {
        anchors.top: dockBg.top; anchors.topMargin: 1
        anchors.left: dockBg.left; anchors.leftMargin: Math.round(16 * root.sf)
        anchors.right: dockBg.right; anchors.rightMargin: Math.round(16 * root.sf)
        height: 1; radius: 1
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "transparent" }
            GradientStop { position: 0.2; color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.30) }
            GradientStop { position: 0.5; color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.45) }
            GradientStop { position: 0.8; color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.30) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }

    // ── Scroll container — clips the row to dock.width on narrow screens ──
    // Interactive scroll lets the user swipe/drag to reveal clipped icons.
    Flickable {
        id: dockFlickable
        anchors.fill: parent
        contentWidth: dockRow.width + Math.round(28 * root.sf)
        contentHeight: parent.height
        clip: true
        interactive: contentWidth > width  // only grab input when content overflows
        flickableDirection: Flickable.HorizontalFlick
        boundsBehavior: Flickable.StopAtBounds

        Row {
            id: dockRow
            // Centre in the Flickable content area (mirrors old anchors.centerIn: dockBg)
            x: Math.round(14 * root.sf)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Math.round(2 * root.sf)

        Repeater {
            model: [
                // System apps (QML-based)
                { appId: "nativeapps", label: "Apps" },
                { appId: "skills",     label: "Skills" },
                { appId: "integrations", label: "Integraciones" },
                { appId: "providers",  label: "Providers" },
                { appId: "mcp",        label: "MCP Apps" },
                { appId: "agents",     label: "Agents" },
                { appId: "tasks",      label: "Tareas" }
                // Terminal eliminado del dock — centralizado en "Apps"
                // (NativeAppsLauncher → qterminal). Un solo punto de entrada.
            ]

            delegate: Item {
                id: dockItem
                width: dock.itemW
                height: Math.round(68 * root.sf)
                visible: true

                property bool isOpen: {
                    for (var i = 0; i < root.openWindows.length; i++) {
                        if (root.openWindows[i].appId === modelData.appId) return true;
                    }
                    return false;
                }

                // Hover lift effect — spring OutBack per motion spec
                transform: Translate {
                    y: dockItemMa.containsMouse ? Math.round(-7 * root.sf) : 0
                    Behavior on y {
                        enabled: !Tokens.reduceMotion
                        NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutBack; easing.overshoot: Tokens.springOvershoot }
                    }
                }

                // Dock item hover tint
                Rectangle {
                    anchors.fill: parent; radius: Math.round(Tokens.radiusMd * root.sf)
                    color: dockItemMa.containsMouse ? Tokens.accentGhost : "transparent"
                    Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad } }

                    Column {
                        anchors.centerIn: parent; spacing: Math.round(4 * root.sf)

                        // Icon container with subtle bg
                        Rectangle {
                            width: Math.round(32 * root.sf); height: Math.round(32 * root.sf)
                            radius: Math.round(Tokens.radiusSm * root.sf)
                            anchors.horizontalCenter: parent.horizontalCenter
                            color: dockItemMa.containsMouse ? Tokens.accentSubtle : "transparent"
                            Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad } }

                            Canvas {
                                anchors.centerIn: parent
                                width: Math.round(22 * root.sf); height: Math.round(22 * root.sf)
                                property string appId: modelData.appId
                                property bool hovered: dockItemMa.containsMouse
                                property real s: root.sf
                                onHoveredChanged: requestPaint()
                                onSChanged: requestPaint()
                                Component.onCompleted: requestPaint()

                                onPaint: {
                                    var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                                    ctx.save(); ctx.scale(s, s);
                                    var mainColor = hovered ? Tokens.accentHover : Tokens.accentBase;
                                    ctx.strokeStyle = mainColor;
                                    ctx.fillStyle = mainColor;
                                    ctx.lineWidth = 1.5; ctx.lineCap = "round"; ctx.lineJoin = "round";

                                    // ── App icons (clean line-art, no gradient) ──
                                    if (appId === "nativeapps") {
                                        // Grid of 4 rounded squares
                                        ctx.lineWidth = 1.4;
                                        var s1 = 6.5, g = 1.5;
                                        // Top-left
                                        roundedRect(ctx, 3, 3, s1, s1, 2);
                                        // Top-right
                                        roundedRect(ctx, 3 + s1 + g, 3, s1, s1, 2);
                                        // Bottom-left
                                        roundedRect(ctx, 3, 3 + s1 + g, s1, s1, 2);
                                        // Bottom-right - filled for variety
                                        ctx.fillStyle = mainColor;
                                        roundedRectFill(ctx, 3 + s1 + g, 3 + s1 + g, s1, s1, 2);
                                    }
                                    else if (appId === "skills") {
                                        // Lightning bolt - sharp, clean
                                        ctx.lineWidth = 1.5;
                                        ctx.beginPath();
                                        ctx.moveTo(12.5, 2);
                                        ctx.lineTo(6, 11);
                                        ctx.lineTo(10.5, 11);
                                        ctx.lineTo(9.5, 20);
                                        ctx.lineTo(16, 11);
                                        ctx.lineTo(11.5, 11);
                                        ctx.closePath();
                                        ctx.stroke();
                                    }
                                    else if (appId === "extensions") {
                                        // Puzzle piece - clean outline
                                        ctx.lineWidth = 1.5;
                                        ctx.beginPath();
                                        ctx.moveTo(3, 8); ctx.lineTo(7, 8);
                                        ctx.arc(9, 7, 2, Math.PI, 0);
                                        ctx.lineTo(11, 8); ctx.lineTo(19, 8);
                                        ctx.lineTo(19, 12);
                                        ctx.arc(18, 14, 2, -Math.PI / 2, Math.PI / 2);
                                        ctx.lineTo(19, 16); ctx.lineTo(19, 20);
                                        ctx.lineTo(3, 20); ctx.lineTo(3, 8);
                                        ctx.stroke();
                                    }
                                    else if (appId === "integrations") {
                                        // Integraciones: dos eslabones entrelazados (conectar apps).
                                        ctx.lineWidth = 1.6;
                                        // Eslabón izquierdo (cápsula redondeada)
                                        roundedRect(ctx, 2.5, 8, 10, 6, 3);
                                        // Eslabón derecho, solapado (= cadena)
                                        roundedRect(ctx, 9.5, 8, 10, 6, 3);
                                    }
                                    else if (appId === "providers") {
                                        // Cloud - clean outline
                                        ctx.lineWidth = 1.5;
                                        ctx.beginPath();
                                        ctx.arc(8, 13, 4.5, Math.PI, 1.5 * Math.PI);
                                        ctx.arc(14, 11, 3.5, 1.25 * Math.PI, 2 * Math.PI);
                                        ctx.arc(17, 15, 2.5, 1.5 * Math.PI, 0.5 * Math.PI);
                                        ctx.lineTo(5, 17.5);
                                        ctx.arc(5, 15, 2.5, 0.5 * Math.PI, Math.PI);
                                        ctx.closePath(); ctx.stroke();
                                    }
                                    else if (appId === "mcp") {
                                        // Hub/nodes - center circle with 3 satellite circles + lines
                                        ctx.lineWidth = 1.4;
                                        // Center
                                        ctx.beginPath(); ctx.arc(11, 11, 3, 0, Math.PI * 2); ctx.stroke();
                                        // Lines to satellites
                                        ctx.beginPath(); ctx.moveTo(11, 8); ctx.lineTo(11, 4); ctx.stroke();
                                        ctx.beginPath(); ctx.moveTo(8.5, 12.5); ctx.lineTo(5, 16); ctx.stroke();
                                        ctx.beginPath(); ctx.moveTo(13.5, 12.5); ctx.lineTo(17, 16); ctx.stroke();
                                        // Satellite circles (outlined, not filled)
                                        ctx.beginPath(); ctx.arc(11, 3, 2, 0, Math.PI * 2); ctx.stroke();
                                        ctx.beginPath(); ctx.arc(4, 17, 2, 0, Math.PI * 2); ctx.stroke();
                                        ctx.beginPath(); ctx.arc(18, 17, 2, 0, Math.PI * 2); ctx.stroke();
                                    }
                                    else if (appId === "agents") {
                                        // Robot face - clean outlined
                                        ctx.lineWidth = 1.4;
                                        // Head
                                        roundedRect(ctx, 4, 5, 14, 11, 3);
                                        // Eyes
                                        ctx.beginPath(); ctx.arc(8.5, 10, 1.5, 0, Math.PI * 2); ctx.stroke();
                                        ctx.beginPath(); ctx.arc(13.5, 10, 1.5, 0, Math.PI * 2); ctx.stroke();
                                        // Antenna
                                        ctx.beginPath(); ctx.moveTo(11, 5); ctx.lineTo(11, 2); ctx.stroke();
                                        ctx.beginPath(); ctx.arc(11, 1.5, 1.2, 0, Math.PI * 2); ctx.stroke();
                                        // Mouth line
                                        ctx.beginPath(); ctx.moveTo(8, 13); ctx.lineTo(14, 13); ctx.stroke();
                                    }
                                    else if (appId === "terminal") {
                                        // Terminal prompt - clean
                                        ctx.lineWidth = 1.4;
                                        roundedRect(ctx, 1, 3, 20, 17, 2.5);
                                        // Prompt chevron
                                        ctx.lineWidth = 1.6;
                                        ctx.beginPath(); ctx.moveTo(5, 10); ctx.lineTo(9, 13.5); ctx.lineTo(5, 17); ctx.stroke();
                                        // Cursor line
                                        ctx.beginPath(); ctx.moveTo(12, 17); ctx.lineTo(17, 17); ctx.stroke();
                                    }
                                    else if (appId === "tasks") {
                                        // Calendario: cuerpo + anillas + divisor + puntos de tarea
                                        ctx.lineWidth = 1.5;
                                        // Cuerpo del calendario
                                        roundedRect(ctx, 2, 4, 18, 16, 2.5);
                                        // Anilla izquierda
                                        ctx.beginPath(); ctx.moveTo(7, 2); ctx.lineTo(7, 6); ctx.stroke();
                                        // Anilla derecha
                                        ctx.beginPath(); ctx.moveTo(15, 2); ctx.lineTo(15, 6); ctx.stroke();
                                        // Línea divisoria horizontal (cabecera del mes)
                                        ctx.lineWidth = 1.3;
                                        ctx.beginPath(); ctx.moveTo(2, 8); ctx.lineTo(20, 8); ctx.stroke();
                                        // Tres puntos de tarea en el cuerpo
                                        ctx.beginPath(); ctx.arc(7, 12, 1.4, 0, Math.PI * 2); ctx.fill();
                                        ctx.beginPath(); ctx.arc(11, 12, 1.4, 0, Math.PI * 2); ctx.fill();
                                        ctx.beginPath(); ctx.arc(15, 12, 1.4, 0, Math.PI * 2); ctx.fill();
                                        ctx.beginPath(); ctx.arc(7, 17, 1.4, 0, Math.PI * 2); ctx.fill();
                                    }
                                    ctx.restore();
                                }

                                // Helper: draw rounded rect outline
                                function roundedRect(ctx, x, y, w, h, r) {
                                    ctx.beginPath();
                                    ctx.moveTo(x + r, y);
                                    ctx.lineTo(x + w - r, y); ctx.arcTo(x + w, y, x + w, y + r, r);
                                    ctx.lineTo(x + w, y + h - r); ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
                                    ctx.lineTo(x + r, y + h); ctx.arcTo(x, y + h, x, y + h - r, r);
                                    ctx.lineTo(x, y + r); ctx.arcTo(x, y, x + r, y, r);
                                    ctx.closePath(); ctx.stroke();
                                }

                                // Helper: draw rounded rect filled
                                function roundedRectFill(ctx, x, y, w, h, r) {
                                    ctx.beginPath();
                                    ctx.moveTo(x + r, y);
                                    ctx.lineTo(x + w - r, y); ctx.arcTo(x + w, y, x + w, y + r, r);
                                    ctx.lineTo(x + w, y + h - r); ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
                                    ctx.lineTo(x + r, y + h); ctx.arcTo(x, y + h, x, y + h - r, r);
                                    ctx.lineTo(x, y + r); ctx.arcTo(x, y, x + r, y, r);
                                    ctx.closePath(); ctx.fill();
                                }
                            }
                        }

                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: modelData.label || ""; font.pixelSize: Math.round(9 * root.sf)
                            font.family: Tokens.fontBody
                            font.weight: dockItemMa.containsMouse ? Font.Medium : Font.Normal
                            color: dockItemMa.containsMouse ? Tokens.accentBase : Tokens.textSecondary
                            Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad } }
                        }

                        // Active indicator dot
                        Rectangle {
                            anchors.horizontalCenter: parent.horizontalCenter
                            width: Math.round(4 * root.sf); height: Math.round(4 * root.sf)
                            radius: width / 2
                            color: dockItem.isOpen ? Tokens.accentBase : "transparent"
                            Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        }
                    }

                    MouseArea {
                        id: dockItemMa; anchors.fill: parent; hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            // Terminal nativo: kgx (GNOME Console, GTK4/VTE). foot NO sirve
                            // aquí: exige wl_seat v5/data_device v3 y el compositor Qt
                            // anuncia v4/v1 (ver memoria terminal_foot_diagnostico).
                            if (modelData.appId === "terminal") { root.launchNative("qterminal", "qterminal"); return; }
                            openApp(modelData.appId, modelData.label, modelData.appId, modelData.cmd || "", modelData.searchName || "");
                        }
                    }
                }
            }
        }
    } // Row

    } // Flickable

    function openApp(appId, title, icon, cmd, searchName) {
        for (var i = 0; i < root.openWindows.length; i++) {
            if (root.openWindows[i].appId === appId) return;
        }
        var wins = root.openWindows.slice();
        wins.push({ appId: appId, title: title, icon: icon, cmd: cmd || "", searchName: searchName || "" });
        root.openWindows = wins;
    }

    // implicitWidth: visible content width (capped so Desktop centering works on narrow screens)
    implicitWidth: dock.width
    implicitHeight: Math.round(78 * root.sf)
} // dock Item
