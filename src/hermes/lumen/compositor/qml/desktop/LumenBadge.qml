import QtQuick
import "." // Tokens singleton requires explicit local import

// Sereno design system badge / counter dot.
//
// Usage:
//   LumenBadge { sf: root.sf; text: "3" }            // counter
//   LumenBadge { sf: root.sf; dot: true }             // simple dot
//   LumenBadge { sf: root.sf; text: "!"; tone: "danger" }

Item {
    id: badge

    // ── Public API ──
    property real   sf:   1.0
    property string text: ""
    property bool   dot:  false
    property string tone: "accent"  // "accent" | "danger" | "neutral"

    // ── Color mapping ──
    readonly property color _bg: {
        switch (tone) {
            case "danger":  return Tokens.dangerBase
            case "neutral": return Tokens.bgElevated
            default:        return Tokens.accentBase
        }
    }
    readonly property color _fg: {
        switch (tone) {
            case "neutral": return Tokens.textSecondary
            default:        return Tokens.textOnAccent
        }
    }

    // ── Sizing ──
    readonly property int _dotSize:  Math.round(8 * sf)
    readonly property int _minSize:  Math.round(18 * sf)
    readonly property int _padH:     Math.round(5 * sf)

    implicitWidth: {
        if (dot) return _dotSize
        var txtW = labelMeasure.contentWidth + _padH * 2
        return Math.max(_minSize, txtW)
    }
    implicitHeight: dot ? _dotSize : _minSize

    // ── Background pill ──
    Rectangle {
        anchors.fill: parent
        radius:       height / 2
        color:        badge._bg
    }

    // ── Label (hidden for dot mode) ──
    Text {
        id: labelMeasure
        anchors.centerIn: parent
        text:             badge.dot ? "" : badge.text
        font.pixelSize:   Math.round(10 * badge.sf)
        font.weight:      Font.DemiBold
        color:            badge._fg
        visible:          !badge.dot
        font.letterSpacing: 0.3
    }
}
