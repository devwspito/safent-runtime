import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import "../../qml"

// ChatAppWindow — standalone capability app: Chat con el agente.
//
// Loads the existing ChatView.qml via Loader.
// Uses Enqueue (D-Bus) + /run/hermes/tasks.sock stream — exactly the same
// pipeline as the overlay and the kiosk.  Never calls run_cycle.
//
// Context properties:
//   backend    — AppBackend (send, agentChunk, agentDone, agentError)
//   qmlBaseDir — absolute path to lumen/qml/

Window {
    id: appWindow
    title: "Chat — Hermes"
    minimumWidth: 480; minimumHeight: 540
    width: 720; height: 720
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
        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/ChatView.qml")
        asynchronous: false

        onStatusChanged: {
            if (status === Loader.Error) {
                console.error("[chat-app] ChatView.qml failed to load:", source)
            }
            if (status === Loader.Ready && item) {
                item.shell = _shellStub
                item.forceActiveFocus()
            }
        }
    }

    QtObject {
        id: _shellStub
        property string pendingMessage: ""
        function go(_idx) { /* no-op in standalone app */ }
    }
}
