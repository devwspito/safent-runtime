import QtQuick
import QtQuick.Layouts
import "."

// NewMessagesFab — "↓ nuevos mensajes" floating action button.
// Appears when the user has scrolled up while streaming is in progress.
// Fades + slides in <200ms (instant in reduced-motion mode).
// Tab-reachable via KeyNavigation.
//
// Usage: anchor above the composer, set visible externally.
// onTap: emits tapped() signal so the parent can call positionViewAtEnd().
Rectangle {
    id: fab

    signal tapped()

    width: fabRow.width + Theme.sp3
    height: 32
    radius: 16
    color: Theme.alpha(Theme.accent, 0.92)
    border.color: Theme.alpha(Theme.accentBright, 0.35)
    border.width: 1

    // Keyboard focus support
    focus: true
    Keys.onReturnPressed: fab.tapped()
    Keys.onEnterPressed: fab.tapped()

    // Accessible role
    Accessible.role: Accessible.Button
    Accessible.name: "Ir a nuevos mensajes"
    Accessible.onPressAction: fab.tapped()

    // Entry animation — slide up + fade, VNC-safe (instant when reduceMotion)
    opacity: 0
    y: 0

    ParallelAnimation {
        id: enterAnim
        running: false
        loops: 1
        NumberAnimation {
            target: fab; property: "opacity"
            from: 0; to: 1
            duration: Theme.reduceMotion ? 0 : 180
            easing.type: Easing.OutCubic
        }
        NumberAnimation {
            target: fab; property: "y"
            from: 8; to: 0
            duration: Theme.reduceMotion ? 0 : 180
            easing.type: Easing.OutCubic
        }
    }

    ParallelAnimation {
        id: exitAnim
        running: false
        loops: 1
        NumberAnimation {
            target: fab; property: "opacity"
            from: 1; to: 0
            duration: Theme.reduceMotion ? 0 : 140
            easing.type: Easing.InCubic
        }
        NumberAnimation {
            target: fab; property: "y"
            from: 0; to: 8
            duration: Theme.reduceMotion ? 0 : 140
            easing.type: Easing.InCubic
        }
    }

    function show() {
        exitAnim.stop()
        enterAnim.restart()
    }

    function hide() {
        enterAnim.stop()
        exitAnim.restart()
    }

    RowLayout {
        id: fabRow
        anchors.centerIn: parent
        spacing: 5

        Text {
            text: "↓"
            color: "#FFFFFF"
            font.family: Theme.font
            font.pixelSize: Theme.tsCaption + 1
            font.weight: Font.DemiBold
        }

        Text {
            text: "Nuevos mensajes"
            color: "#FFFFFF"
            font.family: Theme.font
            font.pixelSize: Theme.tsCaption + 1
            font.weight: Font.Medium
        }
    }

    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: fab.tapped()
    }
}
