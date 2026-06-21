import QtQuick
import Qt5Compat.GraphicalEffects
import "." // Tokens singleton requires explicit local import

// Sereno design system modal overlay.
//
// Usage:
//   LumenModal {
//       id: confirmModal
//       sf: root.sf
//       open: showingConfirm
//       onClosed: showingConfirm = false
//
//       Column {
//           spacing: Tokens.spLg * sf
//           Text { text: "¿Confirmar?" }
//           LumenButton { label: "Sí"; onClicked: confirmModal.closed() }
//       }
//   }

Item {
    id: modal

    // ── Public API ──
    property real sf:   1.0
    property bool open: false

    signal closed()

    // Content slot
    default property alias content: panel.content

    // ── Anchor to fill parent (caller places modal inside the root Item) ──
    // z: Tokens.zModal guarantees the modal sits above every sibling regardless of
    // declaration order. Callers must NOT set a competing z on the modal itself.
    anchors.fill: parent
    z: Tokens.zModal
    visible: scrimOpacity > 0 || panelScale < 1.0

    // ── Scrim ──
    readonly property real scrimOpacity: open ? 0.55 : 0.0

    Rectangle {
        anchors.fill: parent
        color:        Tokens.bgVoid
        opacity:      modal.scrimOpacity

        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation {
                duration: modal.open ? Tokens.durModal : Tokens.durFast
                easing.type: modal.open ? Easing.OutCubic : Easing.InCubic
            }
        }

        // Tap scrim to dismiss
        MouseArea {
            anchors.fill: parent
            onClicked:    modal.closed()
        }
    }

    // ── Panel enter/exit state ──
    readonly property real panelScale:   open ? 1.0 : 0.94
    readonly property real panelOpacity: open ? 1.0 : 0.0

    // ── Drop shadow behind panel ──
    LumenShadow {
        id: panelShadow
        anchors.fill: panel
        source:       panel
        elevation:    "modal"
        opacity:      modal.panelOpacity

        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation {
                duration: modal.open ? Tokens.durModal : Tokens.durFast
                easing.type: modal.open ? Easing.OutCubic : Easing.InCubic
            }
        }
    }

    // ── Panel card ──
    LumenCard {
        id: panel
        sf:       modal.sf
        pad:      Tokens.spXl
        elevated: false  // shadow handled by panelShadow above
        anchors.centerIn: parent
        // Sensible default width; caller can override via content sizing
        width:  Math.min(Math.round(480 * modal.sf), parent.width - Math.round(Tokens.spXxxl * modal.sf) * 2)

        // Override border to be slightly more visible on the dark scrim
        Rectangle {
            anchors.fill: parent
            radius:       Math.round(Tokens.radiusLg * modal.sf)
            color:        "transparent"
            border.width: 1
            border.color: Tokens.borderDefault
        }

        scale:   modal.panelScale
        opacity: modal.panelOpacity

        Behavior on scale {
            enabled: !Tokens.reduceMotion
            NumberAnimation {
                duration: modal.open ? Tokens.durModal : Tokens.durFast
                easing.type: modal.open ? Easing.OutCubic : Easing.InCubic
            }
        }
        Behavior on opacity {
            enabled: !Tokens.reduceMotion
            NumberAnimation {
                duration: modal.open ? Tokens.durModal : Tokens.durFast
                easing.type: modal.open ? Easing.OutCubic : Easing.InCubic
            }
        }

        // Intercept clicks so they don't bleed through to the scrim dismiss
        MouseArea {
            anchors.fill: parent
            onClicked:    { /* absorb */ }
        }
    }
}
