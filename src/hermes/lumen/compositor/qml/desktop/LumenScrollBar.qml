import QtQuick
import "." // Tokens singleton requires explicit local import

// Sereno design system scroll bar (macOS-style, thin, auto-hide).
//
// Usage — attach to a Flickable or ListView:
//   ListView {
//       id: myList
//       ScrollBar.vertical: LumenScrollBar { sf: root.sf }
//   }
//
// Or manually:
//   LumenScrollBar {
//       sf: root.sf
//       flickable: myFlickable
//   }

import QtQuick.Controls as QC

QC.ScrollBar {
    id: bar

    // ── Public API ──
    property real sf: 1.0

    // ── Geometry ──
    readonly property int _trackW: Math.round(6 * sf)
    readonly property int _minThumbLen: Math.round(28 * sf)

    // Auto-hide: visible only while scrolling or hovering
    opacity: (bar.active || hoverArea.containsMouse) ? 1.0 : 0.0
    Behavior on opacity {
        enabled: !Tokens.reduceMotion
        NumberAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
    }

    // ── Custom visual ──
    contentItem: Rectangle {
        id: thumb
        implicitWidth:  bar._trackW
        implicitHeight: bar._minThumbLen
        radius:         width / 2
        color:          hoverArea.containsMouse ? Tokens.textMuted : Tokens.borderStrong

        Behavior on color {
            enabled: !Tokens.reduceMotion
            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
        }
    }

    background: Item {}   // no visible track — macOS-style

    // ── Hover detection on the thumb ──
    MouseArea {
        id: hoverArea
        anchors.fill: parent
        hoverEnabled: true
        propagateComposedEvents: true
        onPressed: function(mouse) { mouse.accepted = false }
    }

    // Right-side positioning when used as ScrollBar.vertical
    // (caller controls anchors; this gives a sensible default width)
    implicitWidth: _trackW + Math.round(4 * sf)  // slight right margin
}
