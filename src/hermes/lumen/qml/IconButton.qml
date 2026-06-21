import QtQuick
import "."

// IconButton — icon-only button (mic, eye, chevron, dock-tile style).
// Hit area is always ≥44×44 even when the glyph is smaller.
//
// API:
//   property string iconSource     — SVG path (required)
//   property int    iconSize       — rendered glyph size; default 18
//   property string accessibleName — screen-reader label (required for a11y)
//   inherited bool  enabled        — true by default (Item.enabled)
//   property bool   checked        — toggleable state (optional); false by default
//   property int    buttonSize     — overall hit area; default ctrlMd (44)
//   property int    buttonRadius   — corner radius; default rSm (8)
//   property color  hoverFill      — background on hover; default surface2 @ 0.9
//   property color  activeFill     — background when pressed; default card2
//   signal  clicked()

Item {
    id: root

    property string iconSource:     ""
    property int    iconSize:       18
    property string accessibleName: ""
    property bool   checked:        false
    property int    buttonSize:     Theme.ctrlMd
    property int    buttonRadius:   Theme.rSm
    property color  hoverFill:      Theme.alpha(Theme.surface2, 0.90)
    property color  activeFill:     Theme.card2

    signal clicked()

    Accessible.role:        Accessible.Button
    Accessible.name:        root.accessibleName
    Accessible.checkable:   root.checked !== undefined
    Accessible.checked:     root.checked
    Accessible.onPressAction: { if (root.enabled) root.clicked() }

    // The outer item always occupies the full hit area
    implicitWidth:  root.buttonSize
    implicitHeight: root.buttonSize

    activeFocusOnTab: root.enabled

    Keys.onReturnPressed: { if (root.enabled) root.clicked() }
    Keys.onSpacePressed:  { if (root.enabled) root.clicked() }

    // Focus ring
    Rectangle {
        visible:      root.activeFocus && root.enabled
        anchors {
            fill:    btnBg
            margins: -3
        }
        radius:       btnBg.radius + 3
        color:        "transparent"
        border.color: Theme.focusRing
        border.width: 2
    }

    Rectangle {
        id: btnBg
        anchors.centerIn: parent
        // Visual button can be smaller than hit area — min ctrlSm (36)
        width:   Math.max(Theme.ctrlSm, root.buttonSize)
        height:  Math.max(Theme.ctrlSm, root.buttonSize)
        radius:  root.buttonRadius

        color: {
            if (!root.enabled)             return "transparent"
            if (root.checked)              return Theme.alpha(Theme.accent, 0.18)
            if (ma.containsPress)          return root.activeFill
            if (ma.containsMouse)          return root.hoverFill
            return "transparent"
        }

        border.color: root.checked ? Theme.alpha(Theme.accentBright, 0.28) : "transparent"
        border.width: root.checked ? 1 : 0

        Behavior on color { ColorAnimation { duration: 100 } }

        Image {
            anchors.centerIn: parent
            width:    root.iconSize
            height:   root.iconSize
            source:   root.iconSource
            fillMode: Image.PreserveAspectFit
            smooth:   true
            mipmap:   true
            opacity:  root.enabled ? 1.0 : 0.38
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
