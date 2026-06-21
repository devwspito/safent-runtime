import QtQuick
import Qt5Compat.GraphicalEffects
import "." // Tokens singleton requires explicit local import

// Sereno design system tooltip.
//
// Usage — attach to any Item via the static `show` / `hide` helpers,
// OR instantiate directly and drive `visible`:
//
//   LumenTooltip {
//       id: tip
//       sf: root.sf
//       text: "Guardar cambios"
//       // Position manually relative to the target, or use anchors.
//   }
//
//   MouseArea {
//       hoverEnabled: true
//       onEntered: tip.scheduleShow()
//       onExited:  tip.cancelShow()
//   }

Item {
    id: tip

    // ── Public API ──
    property real   sf:   1.0
    property string text: ""

    // Convenience: show after a short hover delay
    function scheduleShow() { delayTimer.restart() }
    function cancelShow()   { delayTimer.stop(); tip.visible = false }

    // ── Sizing (intrinsic to text) ──
    implicitWidth:  Math.min(
                        label.implicitWidth + Math.round(Tokens.spMd * sf) * 2,
                        Math.round(220 * sf))
    implicitHeight: label.implicitHeight + Math.round(Tokens.spSm * sf) * 2

    visible: false
    z: 9999   // always on top

    // ── Hover delay ──
    Timer {
        id: delayTimer
        interval: 500
        repeat:   false
        onTriggered: { if (!Tokens.reduceMotion) tip.visible = true; else tip.visible = true }
    }

    // ── Fade in / out ──
    opacity: tip.visible ? 1.0 : 0.0
    Behavior on opacity {
        enabled: !Tokens.reduceMotion
        NumberAnimation {
            duration: tip.visible ? Tokens.durFast : Tokens.durInstant
            easing.type: tip.visible ? Easing.OutCubic : Easing.InCubic
        }
    }

    // ── Shadow ──
    LumenShadow {
        anchors.fill: bg
        source:       bg
        elevation:    "raised"
    }

    // ── Panel ──
    Rectangle {
        id: bg
        anchors.fill: parent
        radius:       Math.round(Tokens.radiusSm * tip.sf)
        color:        Tokens.bgElevated
        border.width: 1
        border.color: Tokens.borderDefault
    }

    // ── Caption text ──
    Text {
        id: label
        anchors {
            fill:         bg
            leftMargin:   Math.round(Tokens.spMd * tip.sf)
            rightMargin:  Math.round(Tokens.spMd * tip.sf)
            topMargin:    Math.round(Tokens.spSm * tip.sf)
            bottomMargin: Math.round(Tokens.spSm * tip.sf)
        }
        text:             tip.text
        font.pixelSize:   Math.round(11 * tip.sf)
        font.weight:      Font.Medium
        color:            Tokens.textSecondary
        wrapMode:         Text.WordWrap
        lineHeight:       1.4
    }
}
