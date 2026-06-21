import QtQuick
import QtQuick.Layouts
import "." // Tokens singleton — required for Tokens.X references

Rectangle {
    id: desktop
    anchors.fill: parent
    color: Tokens.bgVoid

    // ── Open Files App ──
    function openFilesApp() {
        for (var i = 0; i < root.openWindows.length; i++) {
            if (root.openWindows[i].appId === "files") return;
        }
        var wins = root.openWindows.slice();
        wins.push({ appId: "files", title: "Works", icon: "files" });
        root.openWindows = wins;
    }

    // ── Wallpaper State ──
    property string currentWallpaper: "default"
    property bool wpExpanded: false
    property bool displayExpanded: false
    property var wallpaperList: [
        { id: "default",        name: "Default Aurora",    file: "" },
        { id: "nebula",         name: "★ Nebula",          file: "assets/wallpapers/nebula.png" },
        { id: "cyber-grid",     name: "◈ Cyber Grid",      file: "assets/wallpapers/cyber-grid.png" },
        { id: "aurora",         name: "◌ Aurora",          file: "assets/wallpapers/aurora.png" },
        { id: "ocean-depth",    name: "≋ Ocean Depth",     file: "assets/wallpapers/ocean-depth.png" },
        { id: "abstract-waves", name: "∿ Abstract Waves",  file: "assets/wallpapers/abstract-waves.png" },
        { id: "crystal",        name: "◆ Crystal",         file: "assets/wallpapers/crystal.png" },
        { id: "ocean",          name: "≈ Ocean",           file: "assets/wallpapers/ocean.png" },
        { id: "topology",       name: "⬡ Topology",        file: "assets/wallpapers/topology.png" },
        { id: "abstract",       name: "✦ Abstract",        file: "assets/wallpapers/abstract.png" },
        { id: "alien",          name: "⊕ Alien",           file: "assets/wallpapers/alien.png" },
        { id: "cosmic",         name: "⬢ Cosmic",          file: "assets/wallpapers/cosmic.png" },
        { id: "cyberpunk",      name: "⚡ Cyberpunk",      file: "assets/wallpapers/cyberpunk.png" }
    ]

    // ── Wallpaper Image (shown when not "default") ──
    Image {
        id: wallpaperImage
        anchors.fill: parent
        fillMode: Image.PreserveAspectCrop
        visible: currentWallpaper !== "default"
        source: {
            for (var i = 0; i < wallpaperList.length; i++) {
                if (wallpaperList[i].id === currentWallpaper && wallpaperList[i].file !== "") {
                    return wallpaperList[i].file;
                }
            }
            return "";
        }

        // Subtle dark overlay to keep UI readable
        Rectangle {
            anchors.fill: parent
            color: Qt.rgba(0, 0, 0, 0.25)
        }
    }

    // ── Default Gradient Wallpaper (shown when "default") ──
    Item {
        id: defaultWallpaper
        anchors.fill: parent
        visible: currentWallpaper === "default"

        // ── Layer 0: Base gradient — deep near-black with a cold-warm axis ──
        Rectangle {
            anchors.fill: parent
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 0.0;  color: Qt.lighter(Tokens.bgVoid, 1.18) }
                GradientStop { position: 0.45; color: Tokens.bgVoid }
                GradientStop { position: 1.0;  color: Qt.darker(Tokens.bgVoid, 1.25) }
            }
        }

        // ── Nebulas suaves (estilo LoginScreen — cálidas, estáticas, no intrusivas) ──
        Rectangle {
            x: parent.width * 0.14; y: -parent.height * 0.06
            width: parent.width * 0.74; height: parent.height * 0.52
            radius: width / 2; opacity: 0.11; rotation: -6
            color: Tokens.accentBase
        }
        Rectangle {
            x: parent.width * 0.22; y: parent.height * 0.06
            width: parent.width * 0.62; height: parent.height * 0.55
            radius: width / 2; opacity: 0.10; rotation: 10
            color: Tokens.accentHover
        }
        Rectangle {
            x: parent.width * 0.52; y: parent.height * 0.52
            width: parent.width * 0.5; height: parent.height * 0.42
            radius: width / 2; opacity: 0.05
            // Warm tertiary nebula — stays in the amber palette, no neon
            color: Qt.rgba(Tokens.accentPressed.r, Tokens.accentPressed.g, Tokens.accentPressed.b, 1)
        }

        // ── Campo de estrellas (pintado una vez, como el login) ──
        Canvas {
            anchors.fill: parent; opacity: 0.45
            Component.onCompleted: requestPaint()
            onPaint: {
                var ctx = getContext("2d");
                var seed = 137;
                function rand() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
                for (var i = 0; i < 130; i++) {
                    var sx = rand() * width; var sy = rand() * height;
                    var sr = rand() * 1.2 + 0.3; var so = rand() * 0.5 + 0.08;
                    ctx.beginPath(); ctx.fillStyle = "rgba(255,255,255," + so + ")";
                    ctx.arc(sx, sy, sr, 0, Math.PI * 2); ctx.fill();
                }
            }
        }

        // ── Orbe central con anillos (marca Lumen — sutil, detrás de todo) ──
        Item {
            anchors.centerIn: parent
            width: Math.round(Math.min(parent.width, parent.height) * 0.34)
            height: width
            opacity: 0.5

            Rectangle {   // anillo exterior
                anchors.centerIn: parent
                width: parent.width; height: width; radius: width / 2
                color: "transparent"
                border.color: Qt.rgba(0.937, 0.643, 0.361, 0.10)
                border.width: 1
                Rectangle {  // punto de acento sobre el anillo
                    width: 5; height: 5; radius: 2.5; color: Tokens.accentBase; opacity: 0.7
                    x: parent.width / 2 - 2.5; y: -2.5
                }
            }
            Rectangle {   // anillo medio
                anchors.centerIn: parent
                width: parent.width * 0.78; height: width; radius: width / 2
                color: "transparent"
                border.color: Qt.rgba(0.70, 0.53, 1.0, 0.08); border.width: 1
            }
            Rectangle {   // núcleo (glass)
                anchors.centerIn: parent
                width: parent.width * 0.6; height: width; radius: width / 2
                gradient: Gradient {
                    GradientStop { position: 0.0; color: Qt.rgba(0.06, 0.06, 0.12, 0.55) }
                    GradientStop { position: 1.0; color: Qt.rgba(0.02, 0.02, 0.05, 0.30) }
                }
                border.color: Qt.rgba(0.937, 0.643, 0.361, 0.12); border.width: 1
            }
        }

        // ── Dot grid — static, painted once, extremely subtle ──
        Canvas {
            id: dotGrid
            anchors.fill: parent
            opacity: 1.0

            Component.onCompleted: requestPaint()

            onPaint: {
                var ctx = getContext("2d");
                ctx.clearRect(0, 0, width, height);
                var spacing = 28;
                var dotR    = 0.9;
                ctx.fillStyle = "rgba(255,255,255,0.055)";
                for (var gx = spacing; gx < width;  gx += spacing) {
                    for (var gy = spacing; gy < height; gy += spacing) {
                        ctx.beginPath();
                        ctx.arc(gx, gy, dotR, 0, Math.PI * 2);
                        ctx.fill();
                    }
                }
            }
        }

        // ── Layer 5: Vignette — subtle weight at the bottom edge ──
        Rectangle {
            anchors.fill: parent
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 0.0;  color: Qt.rgba(0, 0, 0, 0.0) }
                GradientStop { position: 0.75; color: Qt.rgba(0, 0, 0, 0.0) }
                GradientStop { position: 1.0;  color: Qt.rgba(0, 0, 0, 0.32) }
            }
        }
    }

    // ── Right-Click Desktop Context Menu ──
    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.RightButton
        z: 1
        onClicked: function(mouse) {
            contextMenu.x = mouse.x;
            contextMenu.y = mouse.y;
            contextMenu.visible = !contextMenu.visible;
        }
    }

    // ── Click-Outside Dismiss Overlay (only visible when context menu is open) ──
    MouseArea {
        anchors.fill: parent
        visible: contextMenu.visible
        z: 499  // Below context menu (500) but above everything else
        onClicked: contextMenu.visible = false
    }

    // ── Context Menu ──
    Rectangle {
        id: contextMenu
        visible: false
        width: Math.round(220 * root.sf)
        height: menuCol.height + Math.round(16 * root.sf)
        radius: Math.round(Tokens.radiusMd * root.sf)
        color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.97)
        border.color: Tokens.borderSubtle
        border.width: 1
        z: 500

        // Close when clicking elsewhere
        Connections {
            target: desktop
            function onWidthChanged() { contextMenu.visible = false; }
        }

        Column {
            id: menuCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Math.round(8 * root.sf)
            spacing: 2

            // ── Copy ──
            Rectangle {
                width: parent.width; height: Math.round(32 * root.sf); radius: root.radiusSm
                color: copyMa.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.10) : "transparent"
                Row { anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left; anchors.leftMargin: Math.round(8 * root.sf); spacing: Math.round(8 * root.sf)
                    Canvas {
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf); anchors.verticalCenter: parent.verticalCenter
                        property real s: root.sf
                        onPaint: { var ctx = getContext("2d"); ctx.clearRect(0,0,width,height); ctx.save(); ctx.scale(s,s);
                            ctx.strokeStyle = "#8b9dc3"; ctx.lineWidth = 1.2;
                            ctx.strokeRect(1, 3, 8, 10); ctx.strokeRect(5, 1, 8, 10);
                            ctx.restore(); }
                        onSChanged: requestPaint()
                    }
                    Text { text: "Copy"; font.pixelSize: Math.round(12 * root.sf); color: root.textPrimary }
                    Item { width: Math.round(40 * root.sf); height: 1 }
                    Text { text: "Ctrl+C"; font.pixelSize: Math.round(10 * root.sf); color: root.textMuted }
                }
                MouseArea { id: copyMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { root.showToast("Copied to clipboard", "success"); contextMenu.visible = false; } }
            }

            // ── Paste ──
            Rectangle {
                width: parent.width; height: Math.round(32 * root.sf); radius: root.radiusSm
                color: pasteMa2.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.10) : "transparent"
                Row { anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left; anchors.leftMargin: Math.round(8 * root.sf); spacing: Math.round(8 * root.sf)
                    Canvas {
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf); anchors.verticalCenter: parent.verticalCenter
                        property real s: root.sf
                        onPaint: { var ctx = getContext("2d"); ctx.clearRect(0,0,width,height); ctx.save(); ctx.scale(s,s);
                            ctx.strokeStyle = "#8b9dc3"; ctx.lineWidth = 1.2;
                            ctx.strokeRect(2, 4, 10, 10);
                            ctx.beginPath(); ctx.moveTo(4, 1); ctx.lineTo(10, 1); ctx.lineTo(10, 4); ctx.lineTo(4, 4); ctx.closePath(); ctx.stroke();
                            ctx.fillStyle = "#8b9dc3"; ctx.fillRect(5, 7, 6, 1); ctx.fillRect(5, 9, 4, 1); ctx.fillRect(5, 11, 5, 1);
                            ctx.restore(); }
                        onSChanged: requestPaint()
                    }
                    Text { text: "Paste"; font.pixelSize: Math.round(12 * root.sf); color: root.textPrimary }
                    Item { width: Math.round(36 * root.sf); height: 1 }
                    Text { text: "Ctrl+V"; font.pixelSize: Math.round(10 * root.sf); color: root.textMuted }
                }
                MouseArea { id: pasteMa2; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { var clip = sysManager.pasteFromClipboard(); if (clip) root.showToast("Pasted: " + clip.substring(0, 30), "info"); contextMenu.visible = false; } }
            }

            // Separator
            Rectangle { width: parent.width; height: 1; color: Qt.rgba(1, 1, 1, 0.06) }

            // ── Open Works Folder ──
            Rectangle {
                width: parent.width; height: Math.round(32 * root.sf); radius: root.radiusSm
                color: worksMa.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.10) : "transparent"
                Row { anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left; anchors.leftMargin: Math.round(8 * root.sf); spacing: Math.round(8 * root.sf)
                    Canvas {
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf); anchors.verticalCenter: parent.verticalCenter
                        property real s: root.sf
                        onPaint: { var ctx = getContext("2d"); ctx.clearRect(0,0,width,height); ctx.save(); ctx.scale(s,s);
                            ctx.fillStyle = "#EFA45C";
                            ctx.beginPath(); ctx.moveTo(1,5); ctx.lineTo(6,5); ctx.lineTo(7,3); ctx.lineTo(1,3); ctx.closePath(); ctx.fill();
                            ctx.beginPath(); ctx.moveTo(1,5); ctx.lineTo(13,5); ctx.lineTo(13,13); ctx.lineTo(1,13); ctx.closePath(); ctx.fill();
                            ctx.fillStyle = "#C98A4E";
                            ctx.beginPath(); ctx.moveTo(1,7); ctx.lineTo(13,7); ctx.lineTo(13,13); ctx.lineTo(1,13); ctx.closePath(); ctx.fill();
                            ctx.restore(); }
                        onSChanged: requestPaint()
                    }
                    Text { text: "Open Works"; font.pixelSize: Math.round(12 * root.sf); color: root.textPrimary }
                }
                MouseArea { id: worksMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { desktop.openFilesApp(); contextMenu.visible = false; } }
            }

            // Separator
            Rectangle { width: parent.width; height: 1; color: Qt.rgba(1, 1, 1, 0.06) }

            // ── Wallpaper Section (expandable/collapsible) ──
            Rectangle {
                width: parent.width; height: Math.round(32 * root.sf); radius: root.radiusSm
                color: wpHeaderMa.containsMouse ? Qt.rgba(0.70, 0.53, 1.0, 0.10) : "transparent"
                Row {
                    anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left; anchors.right: parent.right
                    anchors.leftMargin: Math.round(8 * root.sf); anchors.rightMargin: Math.round(8 * root.sf); spacing: Math.round(6 * root.sf)
                    Text { text: desktop.wpExpanded ? "▾" : "▸"; font.pixelSize: Math.round(10 * root.sf); color: root.textMuted; anchors.verticalCenter: parent.verticalCenter }
                    Canvas {
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf); anchors.verticalCenter: parent.verticalCenter
                        property real s: root.sf
                        onPaint: { var ctx = getContext("2d"); ctx.clearRect(0,0,width,height); ctx.save(); ctx.scale(s,s);
                            ctx.strokeStyle = "#D9A86A"; ctx.lineWidth = 1.2; ctx.strokeRect(1, 1, 12, 12);
                            ctx.fillStyle = "#D9A86A";
                            ctx.beginPath(); ctx.moveTo(3,10); ctx.lineTo(5,6); ctx.lineTo(7,8); ctx.lineTo(9,4); ctx.lineTo(11,10); ctx.closePath(); ctx.fill();
                            ctx.restore(); }
                        onSChanged: requestPaint()
                    }
                    Text { text: "Change Wallpaper"; font.pixelSize: Math.round(12 * root.sf); color: root.textPrimary; anchors.verticalCenter: parent.verticalCenter }
                }
                MouseArea { id: wpHeaderMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: desktop.wpExpanded = !desktop.wpExpanded }
            }

            // Wallpaper list (shown when expanded)
            Repeater {
                model: desktop.wpExpanded ? desktop.wallpaperList : []

                delegate: Rectangle {
                    width: parent.width
                    height: Math.round(36 * root.sf)
                    radius: root.radiusSm
                    color: wpItemMouse.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.10) :
                           desktop.currentWallpaper === modelData.id ? Qt.rgba(0.937, 0.643, 0.361, 0.06) :
                           "transparent"

                    Row {
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.left: parent.left
                        anchors.leftMargin: Math.round(10 * root.sf)
                        anchors.right: parent.right
                        anchors.rightMargin: Math.round(10 * root.sf)
                        spacing: Math.round(10 * root.sf)

                        // Thumbnail preview
                        Rectangle {
                            width: Math.round(26 * root.sf); height: Math.round(26 * root.sf)
                            radius: Math.round(4 * root.sf)
                            color: modelData.file === "" ? Qt.darker(Tokens.bgVoid, 1.1) : "transparent"
                            border.color: desktop.currentWallpaper === modelData.id ? Tokens.accentBase : Qt.rgba(1, 1, 1, 0.15)
                            border.width: desktop.currentWallpaper === modelData.id ? 1.5 : 1
                            clip: true
                            anchors.verticalCenter: parent.verticalCenter

                            // Default gradient thumbnail
                            Rectangle {
                                anchors.fill: parent
                                anchors.margins: 1
                                radius: Math.round(3 * root.sf)
                                visible: modelData.file === ""
                                gradient: Gradient {
                                    orientation: Gradient.Vertical
                                    GradientStop { position: 0.0; color: Qt.lighter(Tokens.bgVoid, 1.05) }
                                    GradientStop { position: 0.4; color: Tokens.bgVoid }
                                    GradientStop { position: 1.0; color: Qt.darker(Tokens.bgVoid, 1.15) }
                                }
                                Rectangle {
                                    width: parent.width * 0.7; height: parent.height * 0.5
                                    x: parent.width * 0.1; y: parent.height * 0.15
                                    radius: width / 2; opacity: 0.4
                                    gradient: Gradient {
                                        GradientStop { position: 0.0; color: Qt.rgba(Tokens.infoBase.r, Tokens.infoBase.g, Tokens.infoBase.b, 0.8) }
                                        GradientStop { position: 1.0; color: "transparent" }
                                    }
                                }
                            }

                            Image {
                                anchors.fill: parent
                                anchors.margins: 1
                                source: modelData.file !== "" ? modelData.file : ""
                                fillMode: Image.PreserveAspectCrop
                                visible: modelData.file !== ""
                                asynchronous: true
                                sourceSize.width: Math.round(52 * root.sf)
                                sourceSize.height: Math.round(52 * root.sf)
                            }
                        }

                        Text {
                            text: modelData.name
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(13 * root.sf)
                            color: desktop.currentWallpaper === modelData.id ? Tokens.accentBase : Tokens.textPrimary
                            font.weight: desktop.currentWallpaper === modelData.id ? Font.Medium : Font.Normal
                            anchors.verticalCenter: parent.verticalCenter
                        }

                        Item { width: 1; height: 1 }

                        // Active indicator dot
                        Rectangle {
                            width: Math.round(6 * root.sf); height: Math.round(6 * root.sf)
                            radius: Math.round(3 * root.sf)
                            color: Tokens.accentBase
                            visible: desktop.currentWallpaper === modelData.id
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    MouseArea {
                        id: wpItemMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            desktop.currentWallpaper = modelData.id;
                            contextMenu.visible = false;
                            // Wallpaper se aplica en esta sesión (sin persistencia D-Bus todavía).
                        }
                    }
                }
            }

            // Separator
            Rectangle {
                width: parent.width
                height: 1
                color: Qt.rgba(1, 1, 1, 0.06)
            }

            // ── Display Settings Section (expandable) ──
            Rectangle {
                width: parent.width; height: Math.round(32 * root.sf); radius: root.radiusSm
                color: dispHeaderMa.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.08) : "transparent"
                Row {
                    anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left; anchors.right: parent.right
                    anchors.leftMargin: Math.round(8 * root.sf); anchors.rightMargin: Math.round(8 * root.sf); spacing: Math.round(6 * root.sf)
                    Text { text: desktop.displayExpanded ? "▾" : "▸"; font.pixelSize: Math.round(10 * root.sf); color: root.textMuted; anchors.verticalCenter: parent.verticalCenter }
                    Canvas {
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf); anchors.verticalCenter: parent.verticalCenter
                        property real s: root.sf
                        onPaint: { var ctx = getContext("2d"); ctx.clearRect(0,0,width,height); ctx.save(); ctx.scale(s,s);
                            ctx.strokeStyle = "#EFA45C"; ctx.lineWidth = 1.2;
                            ctx.strokeRect(1, 2, 12, 8);
                            ctx.beginPath(); ctx.moveTo(4, 10); ctx.lineTo(10, 10); ctx.lineTo(10, 12); ctx.lineTo(4, 12); ctx.closePath(); ctx.stroke();
                            ctx.beginPath(); ctx.moveTo(3, 12); ctx.lineTo(11, 12); ctx.stroke();
                            ctx.restore(); }
                        onSChanged: requestPaint()
                    }
                    Text { text: "Display Settings"; font.pixelSize: Math.round(12 * root.sf); color: root.textPrimary; anchors.verticalCenter: parent.verticalCenter }
                }
                MouseArea { id: dispHeaderMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: desktop.displayExpanded = !desktop.displayExpanded }
            }

            // Display options (shown when expanded)
            Column {
                width: parent.width; spacing: 1; visible: desktop.displayExpanded

                // Resolution label
                Text { text: "Resolution"; font.pixelSize: Math.round(9 * root.sf); color: root.textMuted; leftPadding: Math.round(30 * root.sf); topPadding: Math.round(4 * root.sf) }

                // Resolution options
                Repeater {
                    model: [
                        { label: "1920 × 1080", res: "1920x1080", tag: "Full HD" },
                        { label: "1680 × 1050", res: "1680x1050", tag: "WSXGA+" },
                        { label: "1600 × 900",  res: "1600x900",  tag: "HD+" },
                        { label: "1440 × 900",  res: "1440x900",  tag: "WXGA+" },
                        { label: "1366 × 768",  res: "1366x768",  tag: "WXGA" },
                        { label: "1280 × 720",  res: "1280x720",  tag: "HD" },
                        { label: "1024 × 768",  res: "1024x768",  tag: "XGA" }
                    ]

                    delegate: Rectangle {
                        width: parent.width; height: Math.round(26 * root.sf); radius: root.radiusSm
                        color: resCtxMa.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.08) : "transparent"
                        Row {
                            anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left; anchors.right: parent.right
                            anchors.leftMargin: Math.round(30 * root.sf); anchors.rightMargin: Math.round(8 * root.sf); spacing: Math.round(6 * root.sf)
                            Text { text: "●"; font.pixelSize: Math.round(8 * root.sf); color: Tokens.accentBase; visible: false /* TODO: check current */ }
                            Text { text: modelData.label; font.pixelSize: Math.round(11 * root.sf); color: root.textSecondary }
                            Item { width: Math.round(4 * root.sf); height: 1 }
                            Text { text: modelData.tag; font.pixelSize: Math.round(8 * root.sf); color: Qt.rgba(1,1,1,0.25) }
                        }
                        MouseArea {
                            id: resCtxMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                var parts = modelData.res.split("x");
                                var w = parseInt(parts[0]); var h = parseInt(parts[1]);
                                var ok = sysManager.setDisplayResolution(w, h);
                                if (ok) {
                                    root.showToast("Resolution set to " + modelData.label, "success");
                                } else {
                                    // Mode doesn't exist — add it via cvt + xrandr --newmode/--addmode
                                    var modeName = w + "x" + h + "_60.00";
                                    try {
                                        var runFn = (typeof sysManager.runCommandQuick === "function") ? "runCommandQuick" : "runCommand";
                                        var cvtResult = JSON.parse(sysManager[runFn](
                                            "cvt " + w + " " + h + " 60 2>/dev/null | grep Modeline | sed 's/Modeline //'", "/"
                                        ));
                                        var modeline = (cvtResult.stdout || "").trim();
                                        if (modeline) {
                                            sysManager[runFn](
                                                "xrandr --newmode " + modeline + " 2>/dev/null; " +
                                                "xrandr --addmode XWAYLAND0 '" + modeName + "' 2>/dev/null; " +
                                                "xrandr --output XWAYLAND0 --mode '" + modeName + "' 2>/dev/null", "/"
                                            );
                                            root.showToast("Resolution set to " + modelData.label + " (mode added)", "success");
                                        } else {
                                            root.showToast("Cannot change resolution (mode not supported)", "error");
                                        }
                                    } catch(e) {
                                        root.showToast("Cannot change resolution (may need to add mode first)", "error");
                                    }
                                }
                                contextMenu.visible = false;
                            }
                        }
                    }
                }

                // Separator
                Rectangle { width: parent.width; height: 1; color: Qt.rgba(1,1,1,0.04); visible: desktop.displayExpanded }

                // Scaling label
                Text { text: "UI Scaling"; font.pixelSize: Math.round(9 * root.sf); color: root.textMuted; leftPadding: Math.round(30 * root.sf); topPadding: Math.round(4 * root.sf) }

                // Scaling options
                Repeater {
                    model: [
                        { label: "Compact", scale: 0.75 },
                        { label: "Default", scale: 1.0 },
                        { label: "Comfortable", scale: 1.15 },
                        { label: "Large", scale: 1.35 },
                        { label: "Extra Large", scale: 1.6 }
                    ]

                    delegate: Rectangle {
                        width: parent.width; height: Math.round(26 * root.sf); radius: root.radiusSm
                        color: dscaleMa.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.08) : "transparent"
                        Row {
                            anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left
                            anchors.leftMargin: Math.round(30 * root.sf); spacing: Math.round(8 * root.sf)
                            Text { text: Math.abs(root.userScale - modelData.scale) < 0.01 ? "●" : "○"; font.pixelSize: Math.round(10 * root.sf); color: Math.abs(root.userScale - modelData.scale) < 0.01 ? Tokens.accentBase : root.textMuted }
                            Text { text: modelData.label; font.pixelSize: Math.round(11 * root.sf); color: Math.abs(root.userScale - modelData.scale) < 0.01 ? Tokens.accentBase : root.textSecondary; font.bold: Math.abs(root.userScale - modelData.scale) < 0.01 }
                        }
                        MouseArea {
                            id: dscaleMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                root.userScale = modelData.scale;
                                root.showToast("Display: " + modelData.label, "success");
                                contextMenu.visible = false;
                            }
                        }
                    }
                }

                // Separator
                Rectangle { width: parent.width; height: 1; color: Qt.rgba(1,1,1,0.04) }

                // Open Display Settings (full)
                Rectangle {
                    width: parent.width; height: Math.round(28 * root.sf); radius: root.radiusSm
                    color: openDispMa.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.08) : "transparent"
                    Row {
                        anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left
                        anchors.leftMargin: Math.round(30 * root.sf); spacing: Math.round(6 * root.sf)
                        Text { text: "Open Display Settings..."; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); color: Tokens.accentBase }
                    }
                    MouseArea {
                        id: openDispMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            root.settingsOpenTab = "display";
                            root.openAppWindow("settings", "Settings", "\uf013");
                            contextMenu.visible = false;
                        }
                    }
                }
            }

            // Separator
            Rectangle {
                width: parent.width
                height: 1
                color: Qt.rgba(1, 1, 1, 0.06)
            }

            // Close option
            Rectangle {
                width: parent.width
                height: Math.round(30 * root.sf)
                radius: root.radiusSm
                color: closeMenuMouse.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.08) : "transparent"

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: parent.left
                    anchors.leftMargin: Math.round(10 * root.sf)
                    text: "✕  Close"
                    font.pixelSize: Math.round(12 * root.sf)
                    color: root.textSecondary
                }

                MouseArea {
                    id: closeMenuMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: contextMenu.visible = false
                }
            }
        }
    }

    // ── Works Folder Desktop Icon ──
    Rectangle {
        id: worksIcon
        x: Math.round(24 * root.sf); y: Math.round(60 * root.sf)
        width: Math.round(72 * root.sf); height: Math.round(78 * root.sf)
        radius: root.radiusMd; z: 10
        color: worksIconMa.containsMouse ? Qt.rgba(0.937, 0.643, 0.361, 0.10) : "transparent"

        Column {
            anchors.centerIn: parent; spacing: Math.round(6 * root.sf)
            Canvas {
                width: Math.round(36 * root.sf); height: Math.round(36 * root.sf)
                anchors.horizontalCenter: parent.horizontalCenter
                property real s: root.sf
                onPaint: {
                    var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                    ctx.save(); ctx.scale(s, s);
                    // Folder shape
                    ctx.fillStyle = "#EFA45C";
                    ctx.beginPath();
                    ctx.moveTo(2, 10); ctx.lineTo(2, 30); ctx.lineTo(34, 30);
                    ctx.lineTo(34, 12); ctx.lineTo(18, 12); ctx.lineTo(15, 8);
                    ctx.lineTo(2, 8); ctx.closePath(); ctx.fill();
                    // Folder tab
                    ctx.fillStyle = Tokens.accentHover;
                    ctx.beginPath();
                    ctx.moveTo(2, 8); ctx.lineTo(15, 8); ctx.lineTo(18, 12);
                    ctx.lineTo(2, 12); ctx.closePath(); ctx.fill();
                    // Folder front
                    ctx.fillStyle = "#C98A4E";
                    ctx.beginPath();
                    ctx.moveTo(2, 14); ctx.lineTo(34, 14); ctx.lineTo(34, 30);
                    ctx.lineTo(2, 30); ctx.closePath(); ctx.fill();
                    ctx.restore();
                }
                onSChanged: requestPaint()
            }
            Text {
                text: "Works"; font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium
                color: Tokens.textPrimary; anchors.horizontalCenter: parent.horizontalCenter
                style: Text.Outline; styleColor: Qt.rgba(0, 0, 0, 0.6)
            }
        }

        MouseArea {
            id: worksIconMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
            onDoubleClicked: desktop.openFilesApp()
        }
    }

    // ── Marca Lumen sutil — wide-tracked, low-opacity, premium feel ──
    Text {
        anchors.centerIn: parent
        anchors.verticalCenterOffset: Math.round(-18 * root.sf)
        text: "L u m e n S O"
        font.pixelSize: Math.round(13 * root.sf)
        font.weight: Font.Light
        font.letterSpacing: Math.round(10 * root.sf)
        color: Tokens.accentBase
        opacity: currentWallpaper === "default" ? 0.08 : 0.06

        // Slow breathe: opacity gently pulses ±0.02 around base, 12 s period.
        // Gated on !reduceMotion: en reposo repinta la GPU 24/7 (caro en decks/SBC
        // sin GPU y en batería). Con reduceMotion (perfil deck/terminal) se apaga.
        SequentialAnimation on opacity {
            running: currentWallpaper === "default" && !Tokens.reduceMotion
            loops: Animation.Infinite
            NumberAnimation {
                to: currentWallpaper === "default" ? 0.10 : 0.07
                duration: 6000
                easing.type: Easing.InOutSine
            }
            NumberAnimation {
                to: currentWallpaper === "default" ? 0.06 : 0.05
                duration: 6000
                easing.type: Easing.InOutSine
            }
        }
    }

    // ── Top Bar ──
    TopBar {
        id: topBar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        z: 20
        securityState: root.securityShieldState
        autoModeOn: root.autoModeOn
    }

    // ── Window Area (full height — dock/chatbar float on top) ──
    Item {
        id: winArea
        anchors.top: topBar.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
    }

    // ── App Windows ──
    Repeater {
        model: root.openWindows
        delegate: AppWindow {
            windowTitle: modelData.title
            windowIcon: modelData.icon
            appId: modelData.appId
            windowArea: winArea
            nativeCmd: modelData.cmd || ""
            nativeSearchName: modelData.searchName || ""
            shellSurface: modelData.surface || null
            toplevelObj: modelData.toplevel || null
            initialX: parent.width / 2 - Math.round(350 * root.sf) + index * Math.round(30 * root.sf)
            initialY: Math.round(80 * root.sf) + index * Math.round(30 * root.sf)
        }
    }

    // Chime de WhaleOS eliminado: el asset y su helper HTTP no existen en
    // LumenSO. Si queremos sonido de notificación, va por libcanberra/GSound
    // con un asset propio horneado — no se simula.
    function playChime() {}

    // ── Siri-like Orb Animation ──
    Item {
        id: siriGlow
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: appDock.top
        anchors.bottomMargin: Math.round(-10 * root.sf)
        width: Math.round(200 * root.sf)
        height: Math.round(200 * root.sf)
        visible: chatBarItem.chatExpanded
        opacity: (chatBarItem.isSending || chatBarItem.isStreaming) ? 1.0 : 0.45

        property real phase: 0
        property bool active: chatBarItem.isSending || chatBarItem.isStreaming
        property bool wasActive: false

        // Play chime when processing starts
        onActiveChanged: {
            if (active && !wasActive) { playChime(); }
            wasActive = active;
        }

        // PERF: Replaced heavy Canvas orb + 150ms timer + pulse animation
        // with a simple static radial glow rectangle
        Rectangle {
            anchors.centerIn: parent
            width: Math.round(100 * root.sf); height: width; radius: width / 2
            color: siriGlow.active ? Qt.rgba(0.937, 0.643, 0.361, 0.15) : Qt.rgba(0.45, 0.32, 0.20, 0.08)
        }
        Rectangle {
            anchors.centerIn: parent
            width: Math.round(50 * root.sf); height: width; radius: width / 2
            color: siriGlow.active ? Qt.rgba(0.96, 0.74, 0.5, 0.3) : Qt.rgba(0.6, 0.45, 0.28, 0.12)
        }
    }

    // ── Dock ──
    AppDock {
        id: appDock
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Math.round(90 * root.sf)
        z: 5
    }

    // ── Chat Bar (overlay, expands upward — z:20 floats above dock so dock stays in place) ──
    // Width is responsive: on compact screens (narrow / portrait) fills almost full width;
    // on regular screens caps at 620 logical units; on ultrawide keeps a pleasant max.
    ChatBar {
        id: chatBarItem
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: chatBarItem.chatFullScreen ? 0 : Math.round(24 * root.sf)
        width: {
            if (chatBarItem.chatFullScreen) return parent.width;
            var margin = Math.round(16 * root.sf);
            var maxW = Math.round(680 * root.sf);
            // On compact screens use almost all available width (minus small margin each side)
            if (parent.width < Tokens.bpCompact * root.sf)
                return parent.width - margin * 2;
            return Math.min(parent.width - margin * 2, maxW);
        }
        z: 20
    }

    // Wallpaper y os-config sin persistencia D-Bus todavía — usa el valor por defecto.
    Component.onCompleted: { }
}
