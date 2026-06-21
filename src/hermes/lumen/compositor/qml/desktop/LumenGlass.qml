import QtQuick
import Qt5Compat.GraphicalEffects
import "." // Tokens es un singleton de qmldir: requiere import explícito del dir local

// Frosted-glass panel. Blurs only a static backdrop snapshot — NOT per-frame content.
// Usage:
//   LumenGlass {
//       anchors.fill: parent
//       intensity: "panel"    // "panel" | "card" | "overlay"
//   }
//
// PERF rules enforced here:
//   - Blur target is `behindSource` (the item BEHIND this glass, not anything on top).
//   - Do NOT nest LumenGlass inside another LumenGlass (glass-over-glass doubles the blur cost).
//     A card sitting on a glass panel uses "card" tint on a plain Rectangle, no 2nd blur.
//   - Modal backdrops use flat dim (Tokens.bgVoid @ 0.60) — no blur.
//   - Maximum ~2 blurred surfaces concurrent on screen.

Rectangle {
    id: glass

    // "panel"  — top bar, dock, side panels
    // "card"   — floating card/window titlebar on a glass parent
    // "overlay" — dropdowns, popovers
    property string intensity: "panel"

    // Source item to blur. Default: the parent of this glass item.
    // Override when the parent IS the thing you want to appear behind the glass.
    property Item behindSource: parent ? parent.parent : null

    color: "transparent"

    // Backdrop capture (what's behind this item)
    ShaderEffectSource {
        id: backdropCapture
        anchors.fill: parent
        sourceItem: glass.behindSource
        sourceRect: Qt.rect(glass.mapToItem(glass.behindSource, 0, 0).x,
                            glass.mapToItem(glass.behindSource, 0, 0).y,
                            glass.width, glass.height)
        live: false    // static snapshot — update on show, not per-frame
        visible: false
    }

    // Re-capture when glass becomes visible or changes size (keeps it cheap)
    onVisibleChanged: if (visible) backdropCapture.scheduleUpdate()
    onWidthChanged:   if (visible) backdropCapture.scheduleUpdate()
    onHeightChanged:  if (visible) backdropCapture.scheduleUpdate()

    // Gaussian blur on the captured backdrop
    GaussianBlur {
        id: blurEffect
        anchors.fill: glass
        source: backdropCapture
        radius: {
            switch (glass.intensity) {
                case "overlay": return 26
                case "card":    return 20
                default:        return 28   // panel
            }
        }
        samples: blurEffect.radius * 2 + 1
        deviation: blurEffect.radius / 3.0
        cached: true
    }

    // Warm tint + amber cast layer (the Sereno signature)
    Rectangle {
        anchors.fill: parent
        color: {
            switch (glass.intensity) {
                case "overlay": return Qt.rgba(32/255, 34/255, 43/255, 0.80)
                case "card":    return Qt.rgba(32/255, 34/255, 43/255, 0.70)
                default:        return Qt.rgba(20/255, 21/255, 27/255, 0.62)   // panel
            }
        }
    }

    // ~1.5% warm amber cast — the signature warmth
    Rectangle {
        anchors.fill: parent
        color: Qt.rgba(240/255, 168/255, 90/255, 0.015)
    }

    // Top highlight edge (1px white @ 0.06)
    Rectangle {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 1
        color: Qt.rgba(1, 1, 1, 0.06)
    }

    // Hairline border
    Rectangle {
        anchors.fill: parent
        radius: parent.radius
        color: "transparent"
        border.width: 1
        border.color: Qt.rgba(1, 1, 1, 0.07)
    }
}
