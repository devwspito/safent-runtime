import QtQuick
import QtQuick.Layouts
import "." // Tokens singleton requires explicit local import

// Sereno design system list row.
//
// Usage:
//   LumenListItem {
//       sf: root.sf
//       selected: index === currentIndex
//       onClicked: currentIndex = index
//
//       // leading slot (icon, avatar…)
//       leading: Rectangle { width: 20; height: 20; color: "red" }
//
//       // main content (Label, Column of Texts…)
//       content: Text { text: model.name; color: Tokens.textPrimary }
//
//       // trailing slot (badge, chevron…)
//       trailing: LumenBadge { text: "3" }
//   }

Item {
    id: row

    // ── Public API ──
    property real sf:       1.0
    property bool selected: false

    // Content slots
    property Item leading:  null
    property Item content:  null
    property Item trailing: null

    signal clicked()

    // ── Sizing ──
    implicitWidth:  Math.round(200 * sf)
    implicitHeight: Math.round(40 * sf)

    // ── State ──
    readonly property bool _hovered: ma.containsMouse && !selected

    // ── Selection accent bar ──
    Rectangle {
        id: accentBar
        width:   Math.round(3 * row.sf)
        anchors.top:    parent.top
        anchors.bottom: parent.bottom
        anchors.left:   parent.left
        anchors.topMargin:    Math.round(6 * row.sf)
        anchors.bottomMargin: Math.round(6 * row.sf)
        radius:  width / 2
        color:   Tokens.accentBase
        visible: row.selected
        opacity: row.selected ? 1.0 : 0.0

        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation { duration: Tokens.durFast; easing.type: Easing.OutCubic }
        }
    }

    // ── Row background ──
    Rectangle {
        anchors.fill: parent
        radius:       Math.round(Tokens.radiusSm * row.sf)
        color: {
            if (row.selected) return Tokens.accentSubtle
            if (row._hovered) return Tokens.bgElevated
            return "transparent"
        }

        Behavior on color {
            enabled: !Tokens.reduceMotion
            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
        }
    }

    // ── Layout ──
    RowLayout {
        anchors {
            fill:         parent
            leftMargin:   Math.round((accentBar.visible ? 10 : 6) * row.sf)
            rightMargin:  Math.round(Tokens.spMd * row.sf)
            topMargin:    0
            bottomMargin: 0
        }
        spacing: Math.round(Tokens.spMd * row.sf)

        // Leading slot
        Loader {
            id: leadingLoader
            sourceComponent: row.leading ? leadingDelegate : null
            Layout.alignment: Qt.AlignVCenter
            property Item _src: row.leading
        }
        Component {
            id: leadingDelegate
            Item {
                width:  leadingLoader._src ? leadingLoader._src.width  : 0
                height: leadingLoader._src ? leadingLoader._src.height : 0
                Component.onCompleted: {
                    if (leadingLoader._src) leadingLoader._src.parent = this
                }
            }
        }

        // Main content (fills remaining space)
        Item {
            id: contentSlot
            Layout.fillWidth: true
            Layout.fillHeight: true
            Component.onCompleted: {
                if (row.content) row.content.parent = contentSlot
            }
            onChildrenChanged: {
                if (row.content && row.content.parent !== contentSlot)
                    row.content.parent = contentSlot
            }
        }

        // Trailing slot
        Loader {
            id: trailingLoader
            sourceComponent: row.trailing ? trailingDelegate : null
            Layout.alignment: Qt.AlignVCenter
            property Item _src: row.trailing
        }
        Component {
            id: trailingDelegate
            Item {
                width:  trailingLoader._src ? trailingLoader._src.width  : 0
                height: trailingLoader._src ? trailingLoader._src.height : 0
                Component.onCompleted: {
                    if (trailingLoader._src) trailingLoader._src.parent = this
                }
            }
        }
    }

    // ── Focus ring ──
    Rectangle {
        anchors.fill: parent
        radius:       Math.round(Tokens.radiusSm * row.sf)
        color:        "transparent"
        border.width: row.activeFocus ? 1 : 0
        border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.50)
        visible:      row.activeFocus
    }

    // ── Interaction ──
    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        cursorShape:  Qt.PointingHandCursor
        onClicked:    row.clicked()
    }

    Keys.onReturnPressed: row.clicked()
    Keys.onSpacePressed:  row.clicked()
    activeFocusOnTab: true
}
