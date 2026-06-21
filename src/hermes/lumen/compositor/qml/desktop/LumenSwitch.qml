import QtQuick
import "." // Tokens singleton requires explicit local import

// Sereno design system toggle switch.
//
// Usage:
//   LumenSwitch {
//       sf: root.sf
//       checked: myModel.enabled
//       onToggled: function(v) { myModel.enabled = v }
//   }

Item {
    id: sw

    // ── Public API ──
    property real sf:      1.0
    property bool checked: false

    signal toggled(bool v)

    // ── Sizing (macOS-style proportions) ──
    readonly property int trackW: Math.round(44 * sf)
    readonly property int trackH: Math.round(26 * sf)
    readonly property int thumbD: Math.round(20 * sf)
    readonly property int thumbPad: Math.round(3 * sf)

    implicitWidth:  trackW
    implicitHeight: Math.max(trackH, Math.round(32 * sf))  // min touch target

    // ── Track ──
    Rectangle {
        id: track
        width:            sw.trackW
        height:           sw.trackH
        anchors.verticalCenter: parent.verticalCenter
        radius:           sw.trackH / 2   // pill
        color:            sw.checked ? Tokens.accentBase : Tokens.bgElevated
        border.width:     1
        border.color:     sw.checked ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.6)
                                     : Tokens.borderDefault

        Behavior on color {
            enabled: !Tokens.reduceMotion
            ColorAnimation { duration: Tokens.durBase; easing.type: Easing.InOutCubic }
        }
        Behavior on border.color {
            enabled: !Tokens.reduceMotion
            ColorAnimation { duration: Tokens.durBase; easing.type: Easing.InOutCubic }
        }

        // ── Thumb ──
        Rectangle {
            id: thumb
            width:  sw.thumbD
            height: sw.thumbD
            radius: sw.thumbD / 2
            color:  Tokens.textPrimary
            anchors.verticalCenter: parent.verticalCenter
            x: sw.checked
               ? sw.trackW - sw.thumbD - sw.thumbPad
               : sw.thumbPad

            Behavior on x {
                enabled: !Tokens.reduceMotion
                NumberAnimation { duration: Tokens.durBase; easing.type: Easing.InOutCubic }
            }

            // Subtle drop shadow on thumb
            layer.enabled: true
            layer.effect: null  // avoid importing GraphicalEffects just for thumb — a border suffices
            border.width: 1
            border.color: Qt.rgba(0, 0, 0, 0.20)
        }
    }

    // ── Focus ring ──
    Rectangle {
        anchors.fill:    track
        anchors.margins: -Math.round(2 * sw.sf)
        radius:          (sw.trackH / 2) + Math.round(2 * sw.sf)
        color:           "transparent"
        border.width:    sw.activeFocus ? Math.round(2 * sw.sf) : 0
        border.color:    Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.45)
        visible:         sw.activeFocus
    }

    // ── Interaction ──
    MouseArea {
        anchors.fill: parent
        cursorShape:  Qt.PointingHandCursor
        onClicked: {
            sw.checked = !sw.checked
            sw.toggled(sw.checked)
        }
    }

    Keys.onReturnPressed: { sw.checked = !sw.checked; sw.toggled(sw.checked) }
    Keys.onSpacePressed:  { sw.checked = !sw.checked; sw.toggled(sw.checked) }
    activeFocusOnTab: true
}
