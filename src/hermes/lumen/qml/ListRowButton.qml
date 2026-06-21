import QtQuick
import QtQuick.Layouts
import "."

// ListRowButton — focusable selectable row for provider lists, settings rows.
// Meets WCAG 2.1 AA: keyboard operable, visible focus ring, ≥44px hit height.
//
// API:
//   property string accessibleName — screen-reader label (required)
//   property bool   selected       — selected/active state; default false
//   inherited bool  enabled        — true by default (Item.enabled)
//   property int    rowHeight      — default Theme.rowMd (64)
//   property color  hoverFill      — hover background; default surface2
//   property color  selectedFill   — selected background; default accent @ 0.12
//   default property alias content — child items placed inside the RowLayout
//   signal  clicked()

Item {
    id: root

    property string accessibleName: ""
    property bool   selected:       false
    property int    rowHeight:      Theme.rowMd
    property color  hoverFill:      Theme.surface2
    property color  selectedFill:   Theme.alpha(Theme.accent, 0.12)

    default property alias content: innerRow.data

    signal clicked()

    Accessible.role:      Accessible.Button
    Accessible.name:      root.accessibleName
    Accessible.onPressAction: { if (root.enabled) root.clicked() }

    implicitHeight: root.rowHeight
    implicitWidth:  200

    activeFocusOnTab: root.enabled

    Keys.onReturnPressed: { if (root.enabled) root.clicked() }
    Keys.onSpacePressed:  { if (root.enabled) root.clicked() }

    // Focus ring — drawn outside the row bounds
    Rectangle {
        visible:      root.activeFocus && root.enabled
        anchors {
            fill:    rowBg
            margins: -2
        }
        radius:       rowBg.radius + 2
        color:        "transparent"
        border.color: Theme.focusRing
        border.width: 2
    }

    Rectangle {
        id: rowBg
        anchors.fill: parent
        radius:       Theme.rSm

        color: {
            if (!root.enabled)    return "transparent"
            if (root.selected)    return root.selectedFill
            if (ma.containsMouse) return root.hoverFill
            return "transparent"
        }

        border.color: root.selected ? Theme.alpha(Theme.accentBright, 0.22) : "transparent"
        border.width: root.selected ? 1 : 0

        Behavior on color { ColorAnimation { duration: 120 } }

        RowLayout {
            id: innerRow
            anchors {
                fill:        parent
                leftMargin:  Theme.sp2
                rightMargin: Theme.sp2
            }
            spacing: Theme.sp2
        }
    }

    MouseArea {
        id: ma
        anchors.fill: parent
        cursorShape:  root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        hoverEnabled: true
        onClicked:    { if (root.enabled) root.clicked() }
    }
}
