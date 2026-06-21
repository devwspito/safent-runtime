import QtQuick
import QtQuick.Layouts
import "."

// ChatActionCard — inline contextual prompt inside the message thread.
// Used for: no-model unserviced, turn error, future HITL/approval surfaces.
// Intentionally NOT a bubble — it spans width and uses a distinct border style.
//
// Properties:
//   copyText  — main explanatory line
//   ctaText   — label for the single action button
//   ctaSignal — function to call when CTA is tapped (passed as JS function)
//
// Design: accent border (not warn) to distinguish from permission-required cards.
Rectangle {
    id: actionCard

    property string copyText: ""
    property string ctaText: ""
    property var ctaAction: null

    width: parent ? parent.width : 0
    height: cardContent.height + Theme.sp3
    radius: Theme.rMd
    color: Theme.alpha(Theme.accent, 0.06)
    border.color: Theme.alpha(Theme.accentBright, 0.28)
    border.width: 1

    Column {
        id: cardContent
        anchors {
            left: parent.left; right: parent.right; top: parent.top
            margins: Theme.sp2; topMargin: Theme.sp1 + 2
        }
        spacing: Theme.sp1 + 2

        RowLayout {
            width: parent.width
            spacing: Theme.sp1

            // Info icon tile
            Rectangle {
                width: 28; height: 28; radius: Theme.rSm
                color: Theme.alpha(Theme.accent, 0.14)
                border.color: Theme.alpha(Theme.accentBright, 0.18); border.width: 1

                Image {
                    anchors.centerIn: parent
                    width: 14; height: 14
                    source: Theme.dimIcon("icons/info-dim.svg")
                    fillMode: Image.PreserveAspectFit
                    smooth: true; mipmap: true
                }
            }

            Text {
                text: actionCard.copyText
                color: Theme.ink2
                font.family: Theme.font
                font.pixelSize: Theme.tsCaption + 1
                lineHeight: 1.45
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }

        // CTA button — only rendered when ctaText is provided
        Rectangle {
            visible: actionCard.ctaText.length > 0
            width: parent.width; height: 32; radius: Theme.rSm
            color: Theme.alpha(Theme.accent, 0.16)
            border.color: Theme.alpha(Theme.accentBright, 0.32); border.width: 1

            Behavior on color { ColorAnimation { duration: 120 } }

            Text {
                anchors.centerIn: parent
                text: actionCard.ctaText
                color: Theme.accentBright
                font.family: Theme.font
                font.pixelSize: Theme.tsCaption + 1
                font.weight: Font.Medium
            }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: { if (actionCard.ctaAction) actionCard.ctaAction() }
            }
        }
    }
}
