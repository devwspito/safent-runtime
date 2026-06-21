import QtQuick
import QtQuick.Layouts
import "."

// Lumen — Navegador. Premium browser chrome.
// Design: real Lucide line-icons, neutral dark palette, 8pt grid.
// No emoji. No MultiEffect. No RotationAnimator/loops:Infinite.
Item {
    id: browserView
    property var shell: null

    property int  activeTab:     0
    property bool showStartPage: true
    property string currentUrl:  "https://github.com"

    property var tabs: [
        { title: "Inicio — Lumen", url: "",                   secure: false },
        { title: "GitHub",         url: "https://github.com", secure: true  }
    ]

    // ── outer card ─────────────────────────────────────────────────────────
    Rectangle {
        anchors { fill: parent; margins: Theme.sp2 }
        radius: Theme.rLg
        color: Theme.surface
        border.color: Theme.line; border.width: 1
        clip: true

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            // ── tab strip ──────────────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true; height: 38
                color: Theme.surface

                // Bottom hairline
                Rectangle {
                    anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
                    height: 1; color: Theme.line
                }

                RowLayout {
                    anchors { fill: parent; leftMargin: Theme.sp1; rightMargin: Theme.sp1 }
                    spacing: 2

                    Repeater {
                        model: browserView.tabs
                        delegate: Item {
                            id: tabItem
                            property bool isActive: index === browserView.activeTab
                            height: 30
                            implicitWidth: Math.min(tabLabel.width + 44, 200)

                            Rectangle {
                                anchors { fill: parent; bottomMargin: 0 }
                                radius: 6
                                color: tabItem.isActive ? Theme.card : "transparent"
                                border.color: tabItem.isActive ? Theme.line : "transparent"
                                border.width: 1

                                RowLayout {
                                    anchors { fill: parent; leftMargin: 10; rightMargin: 8 }
                                    spacing: 6

                                    // Globe icon for tab site indicator
                                    Image {
                                        width: 13; height: 13
                                        source: Theme.dimIcon("icons/globe-dim.svg")
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                        opacity: tabItem.isActive ? 0.9 : 0.5
                                    }

                                    Text {
                                        id: tabLabel
                                        text: modelData.title
                                        font.family: Theme.font
                                        font.pixelSize: Theme.tsMicro
                                        color: tabItem.isActive ? Theme.ink : Theme.ink3
                                        elide: Text.ElideRight
                                        maximumLineCount: 1
                                        Layout.fillWidth: true
                                    }

                                    // Close button — x icon
                                    Item {
                                        width: 16; height: 16
                                        opacity: closeHover.containsMouse ? 1.0 : 0.0
                                        Behavior on opacity { NumberAnimation { duration: 100 } }

                                        Image {
                                            anchors.centerIn: parent
                                            width: 12; height: 12
                                            source: Theme.dimIcon("icons/x-dim.svg")
                                            fillMode: Image.PreserveAspectFit
                                            smooth: true; mipmap: true
                                        }
                                        MouseArea {
                                            id: closeHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                        }
                                    }
                                }
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    browserView.activeTab = index
                                    browserView.showStartPage = (index === 0)
                                    browserView.currentUrl = browserView.tabs[index].url
                                }
                            }
                        }
                    }

                    // New tab button — Lucide plus
                    Item {
                        width: 30; height: 30

                        Rectangle {
                            anchors.centerIn: parent
                            width: 26; height: 26; radius: Theme.rSm
                            color: newTabHover.containsMouse ? Theme.card : "transparent"
                            Behavior on color { ColorAnimation { duration: 100 } }
                        }

                        Image {
                            anchors.centerIn: parent
                            width: 14; height: 14
                            source: Theme.dimIcon("icons/plus-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }

                        MouseArea {
                            id: newTabHover
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                        }
                    }

                    Item { Layout.fillWidth: true }
                }
            }

            // ── toolbar ────────────────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true; height: 46
                color: Theme.bg0

                // Bottom hairline
                Rectangle {
                    anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
                    height: 1; color: Theme.line
                }

                RowLayout {
                    anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                    spacing: 4

                    // Back button
                    Item {
                        width: 30; height: 30

                        Rectangle {
                            anchors.fill: parent; radius: Theme.rSm
                            color: backHover.containsMouse ? Theme.card : "transparent"
                            Behavior on color { ColorAnimation { duration: 100 } }
                        }

                        Image {
                            anchors.centerIn: parent
                            width: 16; height: 16
                            source: Theme.dimIcon("icons/arrow-left-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }

                        MouseArea {
                            id: backHover; anchors.fill: parent
                            hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        }
                    }

                    // Forward button
                    Item {
                        width: 30; height: 30

                        Rectangle {
                            anchors.fill: parent; radius: Theme.rSm
                            color: fwdHover.containsMouse ? Theme.card : "transparent"
                            Behavior on color { ColorAnimation { duration: 100 } }
                        }

                        Image {
                            anchors.centerIn: parent
                            width: 16; height: 16
                            source: Theme.dimIcon("icons/arrow-right-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                            opacity: 0.35
                        }

                        MouseArea {
                            id: fwdHover; anchors.fill: parent
                            hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        }
                    }

                    // Reload button
                    Item {
                        width: 30; height: 30

                        Rectangle {
                            anchors.fill: parent; radius: Theme.rSm
                            color: reloadHover.containsMouse ? Theme.card : "transparent"
                            Behavior on color { ColorAnimation { duration: 100 } }
                        }

                        Image {
                            anchors.centerIn: parent
                            width: 16; height: 16
                            source: Theme.dimIcon("icons/rotate-cw-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }

                        MouseArea {
                            id: reloadHover; anchors.fill: parent
                            hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        }
                    }

                    // Address bar
                    Rectangle {
                        Layout.fillWidth: true; height: 32; radius: Theme.rMd
                        color: Theme.card2
                        border.color: urlInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.40) : Theme.line
                        border.width: 1
                        Behavior on border.color { ColorAnimation { duration: 150 } }

                        RowLayout {
                            anchors { fill: parent; leftMargin: 10; rightMargin: 10 }
                            spacing: 8

                            // Lock / globe icon
                            Image {
                                width: 13; height: 13
                                source: browserView.showStartPage
                                    ? "icons/globe-dim.svg"
                                    : "icons/lock-dim.svg"
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                                opacity: browserView.showStartPage ? 0.7 : 1.0
                            }

                            TextInput {
                                id: urlInput
                                Layout.fillWidth: true
                                verticalAlignment: Text.AlignVCenter
                                color: Theme.ink
                                font.family: Theme.font
                                font.pixelSize: Theme.tsCaption + 1
                                clip: true
                                text: browserView.showStartPage ? "" : browserView.currentUrl
                                onAccepted: {
                                    var t = text.trim()
                                    if (t.length > 0) {
                                        browserView.currentUrl = t.indexOf("://") === -1 ? "https://" + t : t
                                        browserView.showStartPage = false
                                        browserView.tabs[browserView.activeTab].url = browserView.currentUrl
                                        focus = false
                                    }
                                }
                            }

                            Text {
                                visible: urlInput.text.length === 0 && !urlInput.activeFocus
                                text: "Busca o escribe una dirección"
                                color: Theme.ink4
                                font.family: Theme.font
                                font.pixelSize: Theme.tsCaption + 1
                            }
                        }
                    }

                    // Bookmark button
                    Item {
                        width: 30; height: 30

                        Rectangle {
                            anchors.fill: parent; radius: Theme.rSm
                            color: bookmarkHover.containsMouse ? Theme.card : "transparent"
                            Behavior on color { ColorAnimation { duration: 100 } }
                        }

                        Image {
                            anchors.centerIn: parent
                            width: 15; height: 15
                            source: Theme.dimIcon("icons/bookmark-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }

                        MouseArea {
                            id: bookmarkHover; anchors.fill: parent
                            hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        }
                    }
                }
            }

            // ── page area ──────────────────────────────────────────────────
            Item {
                Layout.fillWidth: true; Layout.fillHeight: true

                // START PAGE
                Item {
                    anchors.fill: parent
                    visible: browserView.showStartPage

                    // Neutral base with single subtle center glow
                    Rectangle {
                        anchors.fill: parent
                        color: Theme.surface
                        Behavior on color { ColorAnimation { duration: 200 } }
                    }

                    ColumnLayout {
                        anchors.centerIn: parent
                        spacing: 0
                        width: 480

                        // Lumen mark — Lucide SVG, accent-tinted square
                        Item {
                            Layout.alignment: Qt.AlignHCenter
                            width: 48; height: 48

                            Rectangle {
                                anchors.fill: parent; radius: Theme.rMd
                                color: Theme.alpha(Theme.accent, 0.18)
                                border.color: Theme.alpha(Theme.accentBright, 0.22); border.width: 1
                            }

                            Image {
                                anchors.centerIn: parent
                                width: 24; height: 24
                                source: Theme.dimIcon("icons/globe-dim.svg")
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                        }

                        Item { height: Theme.sp2; width: 1 }

                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            text: "¿Adónde quieres ir?"
                            color: Theme.ink
                            font.family: Theme.font
                            font.pixelSize: 26
                            font.weight: Font.Light
                            font.letterSpacing: -0.3
                        }

                        Item { height: Theme.sp3; width: 1 }

                        // Search bar
                        Rectangle {
                            Layout.alignment: Qt.AlignHCenter
                            width: 460; height: 46; radius: Theme.rMd
                            color: Theme.card2
                            border.color: startSearch.activeFocus ? Theme.alpha(Theme.accentBright, 0.40) : Theme.line
                            border.width: 1
                            Behavior on border.color { ColorAnimation { duration: 150 } }

                            RowLayout {
                                anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                                spacing: Theme.sp1

                                Image {
                                    width: 16; height: 16
                                    source: Theme.dimIcon("icons/search-dim.svg")
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true; mipmap: true
                                }

                                TextInput {
                                    id: startSearch
                                    Layout.fillWidth: true
                                    verticalAlignment: Text.AlignVCenter
                                    color: Theme.ink
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsBody
                                    clip: true
                                    onAccepted: {
                                        var t = text.trim()
                                        if (t.length > 0) {
                                            browserView.currentUrl = t.indexOf("://") === -1
                                                ? "https://www.google.com/search?q=" + encodeURIComponent(t)
                                                : t
                                            browserView.showStartPage = false
                                            text = ""
                                        }
                                    }
                                }

                                Text {
                                    visible: startSearch.text.length === 0 && !startSearch.activeFocus
                                    text: "Busca o escribe una dirección"
                                    color: Theme.ink4
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsBody
                                }
                            }
                        }

                        Item { height: Theme.sp4; width: 1 }

                        Text {
                            Layout.alignment: Qt.AlignHCenter
                            text: "ACCESOS RÁPIDOS"
                            color: Theme.ink4
                            font.family: Theme.font
                            font.pixelSize: Theme.tsMicro
                            font.weight: Font.Medium
                            font.letterSpacing: 1.2
                        }

                        Item { height: Theme.sp2; width: 1 }

                        // Quick access tiles — Lucide icons, no emoji
                        Row {
                            Layout.alignment: Qt.AlignHCenter
                            spacing: Theme.sp1

                            Repeater {
                                model: [
                                    { label: "Correo",    icon: "icons/mail-dim.svg",     url: "https://mail.google.com" },
                                    { label: "Maps",      icon: "icons/globe-dim.svg",    url: "https://maps.google.com" },
                                    { label: "Video",     icon: "icons/video-dim.svg",    url: "https://youtube.com" },
                                    { label: "Archivos",  icon: "icons/file-text-dim.svg",url: "https://wikipedia.org" },
                                    { label: "GitHub",    icon: "icons/folder-dim.svg",   url: "https://github.com" }
                                ]

                                Item {
                                    width: 76; height: 80

                                    // Static shadow
                                    Rectangle {
                                        anchors { left: parent.left; right: parent.right; top: parent.top }
                                        anchors.topMargin: 2; anchors.leftMargin: 1; anchors.rightMargin: -1
                                        height: parent.height; radius: Theme.rLg
                                        color: "#000000"; opacity: 0.16
                                    }

                                    Rectangle {
                                        anchors.fill: parent; radius: Theme.rLg
                                        color: shortcutHover.containsMouse ? Theme.surface2 : Theme.card
                                        border.color: shortcutHover.containsMouse ? Theme.line2 : Theme.line
                                        border.width: 1
                                        Behavior on color { ColorAnimation { duration: 120 } }

                                        ColumnLayout {
                                            anchors.centerIn: parent
                                            spacing: 8

                                            Item {
                                                Layout.alignment: Qt.AlignHCenter
                                                width: 36; height: 36

                                                Rectangle {
                                                    anchors.fill: parent; radius: Theme.rSm
                                                    color: Theme.alpha(Theme.accent, 0.14)
                                                    border.color: Theme.alpha(Theme.accentBright, 0.16); border.width: 1
                                                }

                                                Image {
                                                    anchors.centerIn: parent
                                                    width: 18; height: 18
                                                    source: Theme.dimIcon(modelData.icon)
                                                    fillMode: Image.PreserveAspectFit
                                                    smooth: true; mipmap: true
                                                }
                                            }

                                            Text {
                                                Layout.alignment: Qt.AlignHCenter
                                                text: modelData.label
                                                color: Theme.ink3
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsMicro
                                            }
                                        }

                                        MouseArea {
                                            id: shortcutHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                browserView.currentUrl = modelData.url
                                                browserView.showStartPage = false
                                                browserView.tabs[browserView.activeTab].url = modelData.url
                                                browserView.tabs[browserView.activeTab].title = modelData.label
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // MOCK PAGE — neutral dark, no purple tint
                Item {
                    anchors.fill: parent
                    visible: !browserView.showStartPage

                    Rectangle {
                        anchors.fill: parent; color: Theme.bg0

                        ColumnLayout {
                            anchors { top: parent.top; left: parent.left; right: parent.right }
                            spacing: 0

                            // Mock site nav
                            Rectangle {
                                Layout.fillWidth: true; height: 48
                                color: Theme.surface
                                border.color: Theme.line; border.width: 0
                                Rectangle {
                                    anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
                                    height: 1; color: Theme.line
                                }

                                RowLayout {
                                    anchors { fill: parent; leftMargin: Theme.sp4; rightMargin: Theme.sp4 }
                                    spacing: Theme.sp3

                                    // Site identity mark
                                    Rectangle {
                                        width: 24; height: 24; radius: 6
                                        color: Theme.alpha(Theme.accent, 0.18)
                                        border.color: Theme.alpha(Theme.accentBright, 0.20); border.width: 1
                                        Image {
                                            anchors.centerIn: parent
                                            width: 14; height: 14
                                            source: Theme.dimIcon("icons/globe-dim.svg")
                                            fillMode: Image.PreserveAspectFit
                                            smooth: true; mipmap: true
                                        }
                                    }

                                    Text {
                                        text: browserView.tabs[browserView.activeTab].title
                                        color: Theme.ink2
                                        font.family: Theme.font
                                        font.pixelSize: Theme.tsCaption + 1
                                        font.weight: Font.DemiBold
                                    }

                                    Item { Layout.fillWidth: true }

                                    Repeater {
                                        model: ["Explorar", "Mercado", "Blog"]
                                        Text {
                                            text: modelData
                                            color: Theme.ink3
                                            font.family: Theme.font
                                            font.pixelSize: Theme.tsCaption + 1
                                        }
                                    }

                                    Rectangle {
                                        width: 72; height: 28; radius: Theme.rSm
                                        color: Theme.accent
                                        Text {
                                            anchors.centerIn: parent
                                            text: "Entrar"
                                            color: "#FFFFFF"
                                            font.family: Theme.font
                                            font.pixelSize: Theme.tsCaption
                                            font.weight: Font.Medium
                                        }
                                    }
                                }
                            }

                            // Mock hero
                            Item {
                                Layout.fillWidth: true; height: 200

                                Rectangle {
                                    anchors.fill: parent
                                    color: Theme.bg0
                                    Behavior on color { ColorAnimation { duration: 200 } }
                                }

                                ColumnLayout {
                                    anchors.centerIn: parent
                                    spacing: Theme.sp1 + 4

                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: browserView.tabs[browserView.activeTab].title
                                        color: Theme.ink
                                        font.family: Theme.font
                                        font.pixelSize: 28
                                        font.weight: Font.Light
                                        font.letterSpacing: -0.3
                                    }

                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: browserView.currentUrl
                                        color: Theme.ink3
                                        font.family: Theme.mono
                                        font.pixelSize: Theme.tsMicro
                                    }

                                    Rectangle {
                                        Layout.alignment: Qt.AlignHCenter
                                        width: 130; height: 34; radius: Theme.rSm
                                        color: Theme.accent
                                        Text {
                                            anchors.centerIn: parent
                                            text: "Comenzar"
                                            color: "#FFFFFF"
                                            font.family: Theme.font
                                            font.pixelSize: Theme.tsCaption + 1
                                            font.weight: Font.Medium
                                        }
                                    }
                                }
                            }

                            // Mock content cards — Lucide icons, no emoji
                            Row {
                                Layout.alignment: Qt.AlignHCenter
                                spacing: Theme.sp2
                                topPadding: Theme.sp3

                                Repeater {
                                    model: [
                                        { icon: "icons/zap-dim.svg",       title: "Rendimiento" },
                                        { icon: "icons/shield-check-dim.svg", title: "Seguridad" },
                                        { icon: "icons/sparkles.svg",      title: "Diseño" }
                                    ]

                                    Rectangle {
                                        width: 210; height: 100; radius: Theme.rLg
                                        color: Theme.card; border.color: Theme.line; border.width: 1

                                        // Top hairline highlight
                                        Rectangle {
                                            anchors { top: parent.top; left: parent.left; right: parent.right }
                                            anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                                            height: 1; radius: Theme.rLg - 1
                                            color: "#FFFFFF"; opacity: 0.04
                                        }

                                        ColumnLayout {
                                            anchors { fill: parent; margins: Theme.sp2 }
                                            spacing: Theme.sp1

                                            Rectangle {
                                                width: 32; height: 32; radius: Theme.rSm
                                                color: Theme.alpha(Theme.accent, 0.14)
                                                border.color: Theme.alpha(Theme.accentBright, 0.16); border.width: 1

                                                Image {
                                                    anchors.centerIn: parent
                                                    width: 16; height: 16
                                                    source: Theme.dimIcon(modelData.icon)
                                                    fillMode: Image.PreserveAspectFit
                                                    smooth: true; mipmap: true
                                                }
                                            }

                                            Text {
                                                text: modelData.title
                                                color: Theme.ink
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsCaption + 1
                                                font.weight: Font.Medium
                                            }

                                            Text {
                                                text: "Contenido de demostración."
                                                color: Theme.ink3
                                                font.family: Theme.font
                                                font.pixelSize: Theme.tsMicro
                                                wrapMode: Text.WordWrap
                                                Layout.fillWidth: true
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
