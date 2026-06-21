import QtQuick
import Qt5Compat.GraphicalEffects

// Elevation shadow. Four presets, near-black (not tinted).
// Wrap the item you want to shadow:
//
//   LumenShadow {
//       elevation: "modal"
//       anchors.fill: windowRect
//       source: windowRect
//   }
//
// Presets     vertOffset  radius  samples   opacity
//  raised         2px       12      25       0.32
//  floating       6px       24      49       0.40
//  modal         12px       40      65       0.48
//  overlay       18px       56      65       0.55
//
// radius is FIXED per elevation (never derived from width).
// samples = min(2*radius+1, 65) — hard cap avoids GPU catástrofe on large items.

DropShadow {
    id: shadow

    // "raised" | "floating" | "modal" | "overlay"
    property string elevation: "floating"

    horizontalOffset: 0
    verticalOffset: {
        switch (elevation) {
            case "overlay":  return 18
            case "modal":    return 12
            case "floating": return 6
            default:         return 2   // raised
        }
    }
    radius: {
        switch (elevation) {
            case "overlay":  return 56
            case "modal":    return 40
            case "floating": return 24
            default:         return 12  // raised
        }
    }
    samples: Math.min(2 * shadow.radius + 1, 65)
    color: {
        var alpha = 0.32
        switch (elevation) {
            case "overlay":  alpha = 0.55; break
            case "modal":    alpha = 0.48; break
            case "floating": alpha = 0.40; break
        }
        return Qt.rgba(0, 0, 0, alpha)
    }
    spread: 0
    cached: true
}
