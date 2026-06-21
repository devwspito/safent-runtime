import QtQuick
import QtQuick.Window
import "../../qml"

// SecurityWindow — standalone capability app: Seguridad y Auditoría.
//
// Loads the existing SecurityView.qml via Loader.
// Data: ListRecentTasks (last 12) polled every 6 s.
//       GetAuditChainHead for the chain integrity indicator.
// Mutations (governance only):
//   ApproveAction(proposal_id)         → HITL approve
//   RejectAction(proposal_id, reason)  → HITL reject
//
// Context properties:
//   backend    — AppBackend (listLoaded, approveAction, rejectAction)
//   qmlBaseDir — absolute path to lumen/qml/

Window {
    id: appWindow
    title: "Seguridad — Hermes"
    minimumWidth: 720; minimumHeight: 540
    width: 1100; height: 760
    visible: true
    color: Theme.bg0

    // Gradient bg — free on software/VNC render, adds depth.
    Rectangle {
        anchors.fill: parent; z: -1
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.bg0 }
            GradientStop { position: 1.0; color: Theme.bgBottom }
        }
    }

    Loader {
        id: viewLoader
        anchors.fill: parent
        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/SecurityView.qml")
        asynchronous: false

        onStatusChanged: {
            if (status === Loader.Error) {
                console.error("[security-app] SecurityView.qml failed to load:", source)
            }
            if (status === Loader.Ready && item) {
                item.shell = _shellStub
            }
        }
    }

    QtObject {
        id: _shellStub
        function go(_idx) { /* no-op */ }
    }
}
