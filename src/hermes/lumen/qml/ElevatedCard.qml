import QtQuick
import "."

// ElevatedCard — card surface with static shadow, 1px border, and top hairline.
// Replaces hand-rolled shadow stacks across the shell.
//
// Usage:
//   ElevatedCard {
//       width: parent.width; height: contentItem.height + Theme.sp3
//       radius: Theme.rLg          // optional override
//       shadowOffsetY: 3           // optional: use Theme.elevRaised.offsetY / elevFloating.offsetY
//       shadowOpacity: 0.20        // optional: use Theme.elevRaised.opacity / elevFloating.opacity
//       // place content inside 'content' property or as children
//   }
//
// Properties:
//   radius        int   — corner radius; default Theme.rLg
//   fill          color — card background; default Theme.card
//   showBorder    bool  — show 1px border hairline; default true
//   showHighlight bool  — show inner top hairline highlight; default true
//   shadowOffsetY int   — vertical shadow offset in px; default Theme.elevRaised.offsetY (2)
//   shadowOpacity real  — shadow opacity [0,1]; default Theme.elevRaised.opacity (0.14)
//   shadowInsetX  int   — horizontal shadow inset on each side; default 2
//   shadowOutsetX int   — horizontal shadow outset on right; default 2

Item {
    id: root

    property int  radius:        Theme.rLg
    property color fill:         Theme.card
    property bool showBorder:    true
    property bool showHighlight: true
    property int  shadowOffsetY: Theme.elevRaised.offsetY
    property real shadowOpacity: Theme.elevRaised.opacity
    property int  shadowInsetX:  2
    property int  shadowOutsetX: 2

    // Static shadow underlay — plain Rectangle offset below the card
    Rectangle {
        anchors {
            left:        parent.left
            right:       parent.right
            top:         parent.top
            leftMargin:  root.shadowInsetX
            rightMargin: -root.shadowOutsetX
            topMargin:   root.shadowOffsetY
        }
        height:  parent.height
        radius:  root.radius
        color:   "#000000"
        opacity: root.shadowOpacity
        z:       -1
    }

    // Card surface
    Rectangle {
        id: surface
        anchors.fill: parent
        radius:       root.radius
        color:        root.fill
        border.color: root.showBorder ? Theme.line : "transparent"
        border.width: root.showBorder ? 1 : 0

        // Inner top hairline highlight — conveys a light source from above
        Rectangle {
            visible: root.showHighlight
            anchors {
                top:         parent.top
                left:        parent.left
                right:       parent.right
                topMargin:   1
                leftMargin:  1
                rightMargin: 1
            }
            height: 1
            radius: root.radius - 1
            color:  Theme.highlightTopColor
            opacity: Theme.highlightTopOpacity
        }
    }
}
