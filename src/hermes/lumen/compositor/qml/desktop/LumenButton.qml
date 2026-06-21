import QtQuick
import QtQuick.Layouts
import "." // Tokens singleton requires explicit local import

// Sereno design system button.
//
// Usage:
//   LumenButton {
//       sf: root.sf
//       label: "Continuar"
//       variant: "primary"   // "primary" | "secondary" | "ghost" | "danger"
//       onClicked: doSomething()
//   }

Item {
    id: btn

    // ── Public API ──
    property real   sf:      1.0
    property string label:   ""
    property string variant: "primary"  // "primary" | "secondary" | "ghost" | "danger"
    property bool   enabled: true
    property bool   loading: false

    signal clicked()

    // ── Sizing ──
    implicitWidth:  Math.round(120 * sf)
    implicitHeight: Math.round(36 * sf)

    // ── State resolution ──
    readonly property bool _hovered:  ma.containsMouse && btn.enabled && !btn.loading
    readonly property bool _pressed:  ma.pressed       && btn.enabled && !btn.loading
    readonly property bool _disabled: !btn.enabled || btn.loading

    // ── Color maps ──
    readonly property color _bgColor: {
        if (_disabled) return Qt.rgba(
            Tokens.bgElevated.r,
            Tokens.bgElevated.g,
            Tokens.bgElevated.b, 0.55)
        if (_pressed) {
            switch (variant) {
                case "danger":    return Qt.darker(Tokens.dangerBase, 1.15)
                case "secondary": return Tokens.borderDefault
                case "ghost":     return Tokens.accentGhost
                default:          return Tokens.accentPressed
            }
        }
        if (_hovered) {
            switch (variant) {
                case "danger":    return Qt.lighter(Tokens.dangerBase, 1.12)
                case "secondary": return Tokens.borderSubtle
                case "ghost":     return Tokens.accentGhost
                default:          return Tokens.accentHover
            }
        }
        switch (variant) {
            case "danger":    return Tokens.dangerBase
            case "secondary": return Tokens.bgElevated
            case "ghost":     return "transparent"
            default:          return Tokens.accentBase
        }
    }

    readonly property color _labelColor: {
        if (_disabled) return Tokens.textDisabled
        switch (variant) {
            case "danger":    return Tokens.textPrimary
            case "secondary": return Tokens.textPrimary
            case "ghost":     return _pressed ? Tokens.accentPressed : Tokens.accentBase
            default:          return Tokens.textOnAccent
        }
    }

    readonly property color _borderColor: {
        if (_disabled) return Tokens.borderSubtle
        switch (variant) {
            case "secondary": return _hovered ? Tokens.borderStrong : Tokens.borderDefault
            default:          return "transparent"
        }
    }

    // ── Background rectangle ──
    Rectangle {
        id: bg
        anchors.fill: parent
        radius:       Math.round(Tokens.radiusMd * btn.sf)
        color:        btn._bgColor
        border.color: btn._borderColor
        border.width: (btn.variant === "secondary") ? 1 : 0

        Behavior on color {
            enabled: !Tokens.reduceMotion
            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
        }
    }

    // ── Scale spring ──
    scale: _pressed ? 0.97 : 1.0
    Behavior on scale {
        enabled: !Tokens.reduceMotion
        NumberAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
    }

    // ── Focus ring ──
    Rectangle {
        anchors.fill: parent
        anchors.margins: -Math.round(2 * btn.sf)
        radius: Math.round((Tokens.radiusMd + 2) * btn.sf)
        color: "transparent"
        border.width: btn.activeFocus ? Math.round(2 * btn.sf) : 0
        border.color: Tokens.accentBase
        visible: btn.activeFocus
    }

    // ── Content row ──
    Row {
        anchors.centerIn: parent
        spacing: Math.round(Tokens.spSm * btn.sf)

        // Spinner (loading state)
        Item {
            width:  Math.round(14 * btn.sf)
            height: Math.round(14 * btn.sf)
            visible: btn.loading

            Rectangle {
                anchors.fill: parent
                radius: width / 2
                color: "transparent"
                border.width: Math.round(2 * btn.sf)
                border.color: btn._labelColor

                Rectangle {
                    width:  Math.round(14 * btn.sf)
                    height: Math.round(14 * btn.sf)
                    anchors.centerIn: parent
                    radius: width / 2
                    color: "transparent"
                    border.width: Math.round(2 * btn.sf)
                    border.color: btn._bgColor
                    // Masks the bottom quarter to make it look like a spinning arc
                    anchors.bottomMargin: -Math.round(2 * btn.sf)
                }

                RotationAnimation on rotation {
                    running: btn.loading && !Tokens.reduceMotion
                    from: 0; to: 360
                    duration: 900
                    loops: Animation.Infinite
                }
            }
        }

        Text {
            text: btn.label
            font.pixelSize: Math.round(13 * btn.sf)
            font.weight:    Font.Medium
            color:          btn._labelColor
            verticalAlignment: Text.AlignVCenter

            Behavior on color {
                enabled: !Tokens.reduceMotion
                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
            }
        }
    }

    // ── Mouse area (minimum touch target 32px) ──
    MouseArea {
        id: ma
        anchors.fill: parent
        anchors.margins: -Math.max(0, Math.round((32 * btn.sf - btn.height) / 2))
        hoverEnabled: true
        cursorShape:  btn.enabled && !btn.loading ? Qt.PointingHandCursor : Qt.ArrowCursor
        enabled:      btn.enabled && !btn.loading
        onClicked:    btn.clicked()
    }

    // Keyboard activation
    Keys.onReturnPressed:  if (btn.enabled && !btn.loading) btn.clicked()
    Keys.onSpacePressed:   if (btn.enabled && !btn.loading) btn.clicked()
    activeFocusOnTab: true
}
