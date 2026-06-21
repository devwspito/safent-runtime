import QtQuick
import QtQuick.Layouts
import "."

// Disclosure — collapsible section ("Avanzado") for onboarding.
// Replaces Adw.ExpanderRow (not available in QML).
// Height animates 200ms OutCubic — same Behavior pattern as the rest of the shell.
// VNC-safe: no GPU effects, pure height animation.
//
// Usage:
//   Disclosure {
//       label: "Avanzado"
//       Layout.fillWidth: true
//       contentComponent: someComponent
//   }
Item {
    id: disclosure

    property string label: "Avanzado"
    // Component to instantiate inside the expanded region
    property Component contentComponent: null
    // Exposed so parent can read it if needed
    property bool expanded: false

    // Height: header (44) + animated body height
    implicitHeight: headerRow.height + (expanded ? bodyLoader.height : 0)

    clip: true

    Behavior on implicitHeight {
        NumberAnimation {
            duration: Theme.reduceMotion ? 0 : 200
            easing.type: Easing.OutCubic
        }
    }

    // ── Header row ─────────────────────────────────────────────────────────
    RowLayout {
        id: headerRow
        width: parent.width
        spacing: 6

        // Chevron — rotates 90° when expanded
        Image {
            width: 14; height: 14
            source: Theme.dimIcon("icons/chevron-right-dim.svg")
            fillMode: Image.PreserveAspectFit
            smooth: true; mipmap: true

            transform: Rotation {
                origin.x: 7; origin.y: 7
                angle: disclosure.expanded ? 90 : 0
                Behavior on angle {
                    NumberAnimation {
                        duration: Theme.reduceMotion ? 0 : 200
                        easing.type: Easing.OutCubic
                    }
                }
            }
        }

        Text {
            text: disclosure.label
            color: Theme.accentBright
            font.family: Theme.font
            font.pixelSize: Theme.tsCaption + 1
            font.weight: Font.Medium
        }

        // Extends tap target across the full row
        MouseArea {
            Layout.fillWidth: true
            height: 44
            cursorShape: Qt.PointingHandCursor
            // Vertically centers the tap zone over the row text
            anchors.verticalCenter: parent.verticalCenter
            onClicked: disclosure.expanded = !disclosure.expanded
        }
    }

    // Invisible tap target that also covers the icon + label
    MouseArea {
        anchors { left: parent.left; right: parent.right; top: parent.top }
        height: headerRow.height
        cursorShape: Qt.PointingHandCursor
        // Let the Layout's MouseArea be the authoritative handler;
        // this just widens the hit area without consuming events from children.
        onClicked: disclosure.expanded = !disclosure.expanded
    }

    // ── Body — loaded lazily when first expanded ───────────────────────────
    Loader {
        id: bodyLoader
        anchors { left: parent.left; right: parent.right; top: headerRow.bottom }
        // Activates on first expand, stays alive after (no re-create)
        active: disclosure.expanded || (item !== null)
        sourceComponent: disclosure.contentComponent
        // Height is the loaded item's implicitHeight, or 0 when collapsed/loading
        height: (item && disclosure.expanded) ? item.implicitHeight : 0
        // Prevent height animation glitch when collapsing
        clip: true
    }
}
