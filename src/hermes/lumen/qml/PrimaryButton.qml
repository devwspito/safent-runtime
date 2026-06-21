import QtQuick
import QtQuick.Layouts
import "."

// PrimaryButton — full-width accent CTA with keyboard support and WCAG AA states.
//
// API:
//   property string label       — button label text (required; also Accessible.name)
//   inherited bool  enabled     — true by default (Item.enabled); false = disabled fill + ink2 label
//   property int    iconWidth   — right-side icon size; 0 = no icon (default 0)
//   property string iconSource  — path to right-side icon; empty = no icon
//   signal  clicked()           — emitted on mouse click or Return/Space key
//
// States:
//   default  — accent fill, white label
//   hover    — accentBright fill
//   pressed  — scale 0.98
//   disabled — disabledFill, ink2 label, not focusable via tab (set enabled: false)
//   focused  — 2px focusRing outline outside border

Item {
    id: root

    property string label:      ""
    property int    iconWidth:  0
    property string iconSource: ""

    signal clicked()

    // Accessible.name mirrors label so screen readers announce it
    Accessible.role:     Accessible.Button
    Accessible.name:     root.label
    Accessible.onPressAction: { if (root.enabled) root.clicked() }

    // Height = ctrlLg (52); width fills parent by convention
    implicitHeight: Theme.ctrlLg
    implicitWidth:  200

    activeFocusOnTab: root.enabled

    Keys.onReturnPressed: { if (root.enabled) root.clicked() }
    Keys.onSpacePressed:  { if (root.enabled) root.clicked() }

    // Focus ring — 2px solid outside the button bounds
    Rectangle {
        visible:       root.activeFocus && root.enabled
        anchors {
            fill:    btnSurface
            margins: -3
        }
        radius:        btnSurface.radius + 3
        color:         "transparent"
        border.color:  Theme.focusRing
        border.width:  2
    }

    // Button surface
    Rectangle {
        id: btnSurface
        anchors.fill: parent
        radius:       Theme.rLg

        color: {
            if (!root.enabled)             return Theme.disabledFill
            if (pressArea.containsPress)   return Theme.alpha(Theme.accentBright, 0.85)
            if (hoverArea.containsMouse)   return Theme.accentBright
            return Theme.accent
        }

        Behavior on color { ColorAnimation { duration: 120 } }

        // Scale feedback on press
        transform: Scale {
            origin.x: btnSurface.width / 2
            origin.y: btnSurface.height / 2
            xScale:   pressArea.containsPress && root.enabled ? 0.98 : 1.0
            yScale:   pressArea.containsPress && root.enabled ? 0.98 : 1.0

            Behavior on xScale { NumberAnimation { duration: 80; easing.type: Easing.OutQuad } }
            Behavior on yScale { NumberAnimation { duration: 80; easing.type: Easing.OutQuad } }
        }

        RowLayout {
            anchors.centerIn: parent
            spacing: 8

            Text {
                text:             root.label
                color:            root.enabled ? "#FFFFFF" : Theme.ink2
                font.family:      Theme.font
                font.pixelSize:   Theme.tsButton
                font.weight:      Font.DemiBold
            }

            Image {
                visible:          root.iconSource.length > 0 && root.iconWidth > 0
                width:            root.iconWidth
                height:           root.iconWidth
                source:           root.iconSource
                fillMode:         Image.PreserveAspectFit
                smooth:           true
                mipmap:           true
                opacity:          root.enabled ? 1.0 : 0.4
            }
        }
    }

    // Hover detection layer (separate from press to get containsMouse)
    MouseArea {
        id: hoverArea
        anchors.fill:    parent
        hoverEnabled:    true
        enabled:         false
        // pointer-events only — actual press handled by pressArea
    }

    MouseArea {
        id: pressArea
        anchors.fill:    parent
        cursorShape:     root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        hoverEnabled:    true
        onClicked:       { if (root.enabled) root.clicked() }

        // Feed hover state to hoverArea equivalent via containsMouse
    }
}
