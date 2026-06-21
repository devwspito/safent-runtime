import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import "../../qml"

// TasksWindow — standalone capability app: Cola de Tareas del agente.
//
// Loads the existing TasksView.qml via Loader (no reimplementation).
// Data: ListRecentTasks + ListPending via backend (Runtime1Client → D-Bus).
// Mutations: ApproveAction / RejectAction via backend.approveAction/rejectAction.
//
// Context properties injected by __main__.py:
//   backend     — AppBackend (connected, loading, daemonError, listLoaded signal)
//   qmlBaseDir  — absolute path to lumen/qml/

Window {
    id: appWindow
    title: "Tareas — Hermes"
    minimumWidth: 720; minimumHeight: 480
    width: 960; height: 640
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
        // Load via absolute URI so no relative-import issues arise.
        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/TasksView.qml")
        asynchronous: false

        onStatusChanged: {
            if (status === Loader.Error) {
                console.error("[tasks-app] TasksView.qml failed to load:", source)
            }
            if (status === Loader.Ready && item) {
                // shell is null — TasksView uses it only for the "Ver resultado"
                // navigation link (goes to chat view in the kiosk). In a standalone
                // app that link is a no-op; inject a stub that silently drops go().
                item.shell = _shellStub
            }
        }
    }

    // Minimal shell stub for TasksView.qml's `shell.go(N)` calls.
    QtObject {
        id: _shellStub
        function go(_idx) { /* no-op in standalone app */ }
    }
}
