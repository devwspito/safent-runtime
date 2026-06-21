import QtQuick
import QtQuick.Window
import "../../qml"

// IntegrationsWindow — standalone capability app: Integraciones (proveedores IA).
//
// Loads the existing ConnectAIView.qml via Loader.
// Data: ListProviders() + GetActiveProvider() polled every 15 s.
// Mutations (governance only):
//   AddProvider(kind, alias, model, key)   — registers a new LLM provider
//   TestProvider(provider_id)              — validates connection to provider
//   SetActiveProvider(provider_id)         — activates a provider
//
// All mutations are governance (LLM provider config), not agent effectors.
// They go via Runtime1Client → org.hermes.Runtime1.  No broker. No HTTP.
//
// Context properties:
//   backend    — AppBackend (providersChanged, providerTestResult, activeProviderChanged)
//   qmlBaseDir — absolute path to lumen/qml/

Window {
    id: appWindow
    title: "Integraciones — Hermes"
    minimumWidth: 600; minimumHeight: 540
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
        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/ConnectAIView.qml")
        asynchronous: false

        onStatusChanged: {
            if (status === Loader.Error) {
                console.error("[integrations-app] ConnectAIView.qml failed to load:", source)
            }
            if (status === Loader.Ready && item) {
                item.shell = _shellStub
            }
        }
    }

    QtObject {
        id: _shellStub
        function go(_idx) { /* no-op in standalone app */ }
    }
}
