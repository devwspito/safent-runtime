import QtQuick

// WheelScroll — drop inside any Flickable or ListView to add mouse-wheel and
// touchpad scrolling. Set `target` to the enclosing Flickable/ListView id.
//
// Usage:
//   Flickable {
//       id: myFlick
//       WheelScroll { target: myFlick }
//   }
WheelHandler {
    property var target: null

    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
    // Grab exclusively so the event doesn't also trigger kinetic flick drag
    grabPermissions: PointerHandler.CanTakeOverFromAnything

    onWheel: function(ev) {
        if (!target) return
        var delta = ev.angleDelta.y !== 0 ? ev.angleDelta.y : ev.pixelDelta.y
        var maxY = Math.max(0, target.contentHeight - target.height)
        target.contentY = Math.max(0, Math.min(maxY, target.contentY - delta))
        ev.accepted = true
    }
}
