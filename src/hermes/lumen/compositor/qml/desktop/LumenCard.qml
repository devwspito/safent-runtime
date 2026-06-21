import QtQuick
import Qt5Compat.GraphicalEffects
import "." // Tokens singleton requires explicit local import

// Sereno design system card container.
//
// Usage:
//   LumenCard {
//       sf: root.sf
//       pad: Tokens.spXl
//       elevated: true      // adds drop shadow
//       width: 320; height: 200
//
//       Text { text: "Card content" }
//   }

Item {
    id: card

    // ── Public API ──
    property real sf:       1.0
    property int  pad:      Tokens.spXl
    property bool elevated: false

    // default content slot
    default property alias content: contentItem.data

    implicitWidth:  Math.round(240 * sf)
    implicitHeight: Math.round(120 * sf)

    // ── Shadow (only when elevated=true) ──
    LumenShadow {
        id: cardShadow
        anchors.fill: bg
        source:       bg
        elevation:    "raised"
        visible:      card.elevated
    }

    // ── Card rectangle ──
    Rectangle {
        id: bg
        anchors.fill: parent
        radius:       Math.round(Tokens.radiusLg * card.sf)
        color:        Tokens.bgCard
        border.width: 1
        border.color: Tokens.borderSubtle
    }

    // ── Padding container (content slot) ──
    Item {
        id: contentItem
        anchors {
            fill:         bg
            margins:      Math.round(card.pad * card.sf)
        }
    }
}
