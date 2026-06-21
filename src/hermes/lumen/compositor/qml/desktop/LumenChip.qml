import QtQuick
import "." // Tokens singleton requires explicit local import

// Sereno design system chip / badge label.
//
// Usage:
//   LumenChip {
//       sf: root.sf
//       text: "Activo"
//       tone: "success"   // "neutral" | "success" | "warn" | "danger" | "info"
//   }

Item {
    id: chip

    // ── Public API ──
    property real   sf:   1.0
    property string text: ""
    property string tone: "neutral"  // "neutral" | "success" | "warn" | "danger" | "info"

    // ── Tone → color mapping ──
    readonly property color _bg: {
        switch (tone) {
            case "success": return Tokens.successSubtle
            case "warn":    return Tokens.warnSubtle
            case "danger":  return Tokens.dangerSubtle
            case "info":    return Tokens.infoSubtle
            default:        return Qt.rgba(Tokens.borderDefault.r, Tokens.borderDefault.g, Tokens.borderDefault.b, 0.40)
        }
    }
    readonly property color _fg: {
        switch (tone) {
            case "success": return Tokens.successBase
            case "warn":    return Tokens.warnBase
            case "danger":  return Tokens.dangerBase
            case "info":    return Tokens.infoBase
            default:        return Tokens.textSecondary
        }
    }
    readonly property color _border: {
        switch (tone) {
            case "success": return Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.25)
            case "warn":    return Qt.rgba(Tokens.warnBase.r,    Tokens.warnBase.g,    Tokens.warnBase.b,    0.25)
            case "danger":  return Qt.rgba(Tokens.dangerBase.r,  Tokens.dangerBase.g,  Tokens.dangerBase.b,  0.25)
            case "info":    return Qt.rgba(Tokens.infoBase.r,    Tokens.infoBase.g,    Tokens.infoBase.b,    0.25)
            default:        return Tokens.borderSubtle
        }
    }

    // ── Sizing (intrinsic to text) ──
    implicitWidth:  label.implicitWidth + Math.round(Tokens.spLg * sf) * 2
    implicitHeight: Math.round(22 * sf)

    // ── Background ──
    Rectangle {
        anchors.fill: parent
        radius:       height / 2   // pill
        color:        chip._bg
        border.width: 1
        border.color: chip._border
    }

    // ── Label ──
    Text {
        id: label
        anchors.centerIn: parent
        text:             chip.text
        font.pixelSize:   Math.round(11 * chip.sf)
        font.weight:      Font.Medium
        color:            chip._fg
        // Inter micro: 11px, SemiBold — matches token spec
        font.letterSpacing: 0.3
    }
}
