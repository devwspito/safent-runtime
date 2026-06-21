import QtQuick
import "."

// AccessibleSwitch — settings toggle meeting WCAG 2.1 AA.
// Height is ≥44px (ctrlMd) for touch target; the track is centered within.
//
// API:
//   property bool   checked     — toggle state; default false
//   property color  onColor     — track color when on; default Theme.ok
//   property color  offColor    — track color when off; default Theme.ink4
//   property string accessibleName — screen-reader label describing what is toggled
//   signal  toggled(bool checked) — emitted after state flips

Item {
    id: root

    property bool   checked:        false
    property color  onColor:        Theme.ok
    property color  offColor:       Theme.ink4
    property string accessibleName: ""

    signal toggled(bool checked)

    Accessible.role:      Accessible.CheckBox
    Accessible.name:      root.accessibleName
    Accessible.checkable: true
    Accessible.checked:   root.checked
    Accessible.onToggleAction: root._toggle()

    // Full hit area — always at least ctrlMd tall for touch/pointer fidelity
    implicitWidth:  44
    implicitHeight: Theme.ctrlMd

    activeFocusOnTab: true

    Keys.onSpacePressed: root._toggle()
    Keys.onReturnPressed: root._toggle()

    function _toggle() {
        root.checked = !root.checked
        root.toggled(root.checked)
    }

    // Focus ring around the track
    Rectangle {
        visible:      root.activeFocus
        anchors {
            fill:    track
            margins: -3
        }
        radius:       track.radius + 3
        color:        "transparent"
        border.color: Theme.focusRing
        border.width: 2
    }

    // Track
    Rectangle {
        id: track
        anchors.centerIn: parent
        width:  44
        height: 24
        radius: 12

        color: root.checked ? root.onColor : root.offColor
        Behavior on color { ColorAnimation { duration: 160 } }

        // Thumb
        Rectangle {
            id: thumb
            width:  18
            height: 18
            radius: 9
            color:  "#FFFFFF"

            anchors {
                verticalCenter: parent.verticalCenter
                left:           parent.left
                leftMargin:     root.checked ? 23 : 3
            }

            Behavior on anchors.leftMargin {
                NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
            }
        }
    }

    MouseArea {
        anchors.fill: parent
        cursorShape:  Qt.PointingHandCursor
        onClicked:    root._toggle()
    }
}
