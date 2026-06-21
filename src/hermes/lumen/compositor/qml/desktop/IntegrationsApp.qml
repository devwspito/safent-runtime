import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// ── IntegrationsApp (Composio) ────────────────────────────────────────────
// SO-NATIVO: consume los verbos D-Bus del daemon (GetComposioStatus /
// SetComposioApiKey / ListComposioApps / ListComposioConnections /
// ConnectComposioApp). El daemon consume Composio Cloud DINÁMICAMENTE —
// cero catálogo hardcodeado, cero HTTP nuestro. Conectar apps depende del
// usuario (OAuth Connect Link → navegador).
Rectangle {
    id: app
    anchors.fill: parent
    color: Tokens.bgSurface
    radius: Math.round(Tokens.radiusLg * sf)
    readonly property real sf: root.sf
    readonly property bool _compact: app.width < Tokens.bpCompact * root.sf

    property bool configured: false
    property bool loading: true
    property bool busy: false
    property string note: ""
    property var connections_: []
    property var catalog: []
    property bool showCatalog: false
    property var keyField: null

    property string renamingId: ""
    property bool renamingBusy: false

    function load() {
        loading = true;
        hermes.call("cmp-status", "get_composio_status", "{}");
    }
    function saveKey() {
        if (busy || !keyField || keyField.text.trim().length === 0) return;
        busy = true; note = "Guardando…";
        hermes.call("cmp-setkey", "set_composio_api_key", JSON.stringify({ api_key: keyField.text.trim() }));
    }
    function connectApp(slug) {
        busy = true; note = "Generando enlace OAuth para " + slug + "…";
        hermes.call("cmp-connect", "connect_composio_app", JSON.stringify({ toolkit_slug: slug }));
    }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "cmp-status") {
                try {
                    var st = JSON.parse(jsonStr || "{}");
                    app.configured = ok && st.configured === true;
                } catch (e) { app.configured = false; }
                if (app.configured) {
                    hermes.call("cmp-conns", "list_composio_connections", "{}");
                    hermes.call("cmp-apps", "list_composio_apps", "{}");
                } else { app.loading = false; }
            } else if (reqId === "cmp-conns") {
                app.loading = false;
                try { app.connections_ = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) { app.connections_ = []; }
            } else if (reqId === "cmp-apps") {
                try { app.catalog = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) { app.catalog = []; }
            } else if (reqId === "cmp-setkey") {
                app.busy = false;
                var r = {}; try { r = JSON.parse(jsonStr || "{}"); } catch (e) {}
                if (ok && r.ok) {
                    app.note = "";
                    if (app.keyField) app.keyField.text = "";
                    root.showToast("Composio conectado", "success");
                    app.load();
                } else { app.note = "✕ " + (r.error || jsonStr); }
            } else if (reqId === "cmp-connect") {
                app.busy = false;
                var c = {}; try { c = JSON.parse(jsonStr || "{}"); } catch (e) {}
                var url = c.redirect_url || c.redirectUrl || "";
                if (ok && url) {
                    app.note = "Abre el navegador para autorizar; al volver, pulsa Refrescar.";
                    root.launchNative("chromium-browser --ozone-platform=wayland --no-sandbox --password-store=basic " + url, "chromium");
                } else { app.note = "✕ " + (c.error || "no se pudo iniciar la conexión"); }
            } else if (reqId === "cmp-alias") {
                app.renamingBusy = false;
                if (ok) {
                    app.renamingId = "";
                    root.showToast("Alias guardado", "success");
                    hermes.call("cmp-conns", "list_composio_connections", "{}");
                } else {
                    var ae = {}; try { ae = JSON.parse(jsonStr || "{}"); } catch (e) {}
                    app.note = "✕ " + (ae.error || "No se pudo guardar el alias");
                }
            }
        }
    }
    Component.onCompleted: load()

    // ── Layout ────────────────────────────────────────────────────────────
    Column {
        anchors.fill: parent
        anchors.margins: Math.round(Tokens.spXl * sf)
        spacing: Math.round(Tokens.spMd * sf)

        // ── Header ──
        Row {
            width: parent.width

            Column {
                width: parent.width - refreshBtn.width - Math.round(Tokens.spMd * sf)
                spacing: Math.round(Tokens.spXs * sf)

                Text {
                    text: "Integraciones"
                    color: Tokens.textPrimary
                    font.family: Tokens.fontDisplay
                    font.pixelSize: Math.round(18 * sf)
                    font.weight: Font.DemiBold
                }
                Text {
                    text: "Tus apps conectadas vía Composio (Gmail, Notion, Slack…). Hermes solo usa las que TÚ conectes."
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(12 * sf)
                    width: parent.width
                    wrapMode: Text.WordWrap
                }
            }

            LumenButton {
                id: refreshBtn
                sf: app.sf
                label: "⟳ Refrescar"
                variant: "secondary"
                implicitWidth: Math.round(100 * sf)
                implicitHeight: Math.round(34 * sf)
                anchors.verticalCenter: parent.verticalCenter
                onClicked: app.load()
            }
        }

        // ── Sin configurar: solicitar API key ──
        Rectangle {
            width: parent.width
            visible: !app.loading && !app.configured
            height: visible ? keyCol.implicitHeight + Math.round(Tokens.spXl * sf) : 0
            radius: Math.round(Tokens.radiusMd * sf)
            color: Tokens.bgCard
            border.width: 1
            border.color: Tokens.borderSubtle

            Column {
                id: keyCol
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spLg * sf)
                spacing: Math.round(Tokens.spMd * sf)

                Text {
                    text: "Conecta tu cuenta de Composio"
                    color: Tokens.textPrimary
                    font.family: Tokens.fontDisplay
                    font.pixelSize: Math.round(14 * sf)
                    font.weight: Font.DemiBold
                }

                Text {
                    text: "Composio da a Hermes acceso a tus apps (Gmail, Notion, Slack, GitHub, Calendar…). Es gratis para empezar. Sigue estos pasos:"
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    width: parent.width
                    wrapMode: Text.WordWrap
                }

                Column {
                    width: parent.width
                    spacing: Math.round(Tokens.spXs * sf)

                    Text {
                        text: "1.  Crea una cuenta gratis (o entra) en composio.dev."
                        color: Tokens.textSecondary
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        width: parent.width
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        text: "2.  En el panel: Settings → API Keys → \"Generate new key\"."
                        color: Tokens.textSecondary
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        width: parent.width
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        text: "3.  Copia la clave (empieza por \"ak_\") y pégala aquí abajo."
                        color: Tokens.textSecondary
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        width: parent.width
                        wrapMode: Text.WordWrap
                    }
                }

                // Open composio.dev button
                Rectangle {
                    width: Math.round(220 * sf)
                    height: Math.round(34 * sf)
                    radius: Math.round(Tokens.radiusSm * sf)
                    color: openMa.containsMouse ? Tokens.accentGhost : "transparent"
                    border.width: 1
                    border.color: openMa.containsMouse ? Tokens.accentBase : Tokens.borderDefault

                    Behavior on color {
                        enabled: !Tokens.reduceMotion
                        ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                    }

                    Row {
                        anchors.centerIn: parent
                        spacing: Math.round(Tokens.spXs * sf)

                        Text {
                            text: "↗"
                            color: Tokens.accentBase
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(13 * sf)
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                            text: "Abrir composio.dev y sacar la key"
                            color: Tokens.accentBase
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * sf)
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    MouseArea {
                        id: openMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.launchNative(
                            "chromium-browser --ozone-platform=wayland --no-sandbox --password-store=basic https://app.composio.dev/developers",
                            "chromium")
                    }
                }

                Text {
                    text: "La clave se guarda cifrada en el daemon (vault); Hermes solo usa las apps que TÚ conectes."
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(10 * sf)
                    width: parent.width
                    wrapMode: Text.WordWrap
                }

                // API key input (password — never shown in clear)
                LumenInput {
                    id: keyInp
                    sf: app.sf
                    width: parent.width
                    placeholder: "ak_…"
                    password: true
                    Component.onCompleted: app.keyField = keyInp
                }

                Row {
                    width: parent.width
                    spacing: Math.round(Tokens.spMd * sf)

                    Text {
                        text: app.note
                        color: app.note.indexOf("✕") === 0 ? Tokens.dangerBase : Tokens.textSecondary
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - kBtn.width - Math.round(Tokens.spMd * sf)
                        elide: Text.ElideRight
                    }

                    LumenButton {
                        id: kBtn
                        sf: app.sf
                        label: app.busy ? "…" : "Conectar"
                        variant: "primary"
                        loading: app.busy
                        implicitWidth: Math.round(110 * sf)
                        onClicked: app.saveKey()
                    }
                }
            }
        }

        // ── Cargando ──
        Text {
            visible: app.loading
            text: "Cargando…"
            color: Tokens.textMuted
            font.family: Tokens.fontBody
            font.pixelSize: Math.round(13 * sf)
        }

        // ── Configurado: tab bar conectadas / catálogo ──
        Row {
            visible: app.configured
            width: parent.width
            spacing: Math.round(Tokens.spSm * sf)

            Rectangle {
                width: tabLabelA.width + Math.round(Tokens.spXl * sf)
                height: Math.round(32 * sf)
                radius: Math.round(Tokens.radiusSm * sf)
                color: !app.showCatalog ? Tokens.accentSubtle : Tokens.bgElevated
                border.width: 1
                border.color: !app.showCatalog ? Tokens.accentBase : Tokens.borderDefault

                Behavior on color {
                    enabled: !Tokens.reduceMotion
                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                }

                Text {
                    id: tabLabelA
                    anchors.centerIn: parent
                    text: "Conectadas (" + app.connections_.length + ")"
                    color: !app.showCatalog ? Tokens.accentBase : Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: app.showCatalog = false
                }
            }

            Rectangle {
                width: tabLabelB.width + Math.round(Tokens.spXl * sf)
                height: Math.round(32 * sf)
                radius: Math.round(Tokens.radiusSm * sf)
                color: app.showCatalog ? Tokens.accentSubtle : Tokens.bgElevated
                border.width: 1
                border.color: app.showCatalog ? Tokens.accentBase : Tokens.borderDefault

                Behavior on color {
                    enabled: !Tokens.reduceMotion
                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                }

                Text {
                    id: tabLabelB
                    anchors.centerIn: parent
                    text: "Conectar nueva (" + app.catalog.length + ")"
                    color: app.showCatalog ? Tokens.accentBase : Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: app.showCatalog = true
                }
            }

            // Inline note alongside tab bar
            Text {
                text: app.note
                color: Tokens.textSecondary
                font.family: Tokens.fontBody
                font.pixelSize: Math.round(10 * sf)
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - Math.round(320 * sf)
                elide: Text.ElideRight
                wrapMode: Text.NoWrap
            }
        }

        // ── Estado vacío en "Conectadas" ──
        Text {
            visible: app.configured && !app.showCatalog && app.connections_.length === 0 && !app.loading
            text: "Aún no has conectado ninguna app. Ve a \"Conectar nueva\" y autoriza la que quieras — depende de ti."
            color: Tokens.textMuted
            font.family: Tokens.fontBody
            font.pixelSize: Math.round(12 * sf)
            width: parent.width
            wrapMode: Text.WordWrap
        }

        // ── Lista principal (conectadas / catálogo) ──
        ListView {
            id: integrationsList
            visible: app.configured
            width: parent.width
            height: app.height - y - Math.round(Tokens.spXl * sf)
            spacing: Math.round(Tokens.spXs * sf)
            clip: true
            model: app.showCatalog ? app.catalog : app.connections_

            ScrollBar.vertical: LumenScrollBar { sf: app.sf }

            WheelHandler {
                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                onWheel: (event) => {
                    integrationsList.contentY = Math.max(0, Math.min(
                        Math.max(0, integrationsList.contentHeight - integrationsList.height),
                        integrationsList.contentY - event.angleDelta.y));
                }
            }

            delegate: Column {
                width: ListView.view.width
                spacing: 0

                property bool isRenaming: !app.showCatalog && modelData.id && (app.renamingId === modelData.id)

                // ── Main row ──
                Rectangle {
                    width: parent.width
                    height: Math.round(54 * sf)
                    radius: Math.round(Tokens.radiusMd * sf)
                    color: Tokens.bgCard
                    border.width: 1
                    border.color: Tokens.borderSubtle

                    // Responsive row: avatar + info(fill) + actions
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: Math.round(Tokens.spLg * sf)
                        anchors.rightMargin: Math.round(Tokens.spMd * sf)
                        spacing: Math.round(Tokens.spMd * sf)

                        // Avatar initial
                        Rectangle {
                            Layout.alignment: Qt.AlignVCenter
                            width: Math.round(32 * sf)
                            height: width
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: Tokens.bgElevated
                            border.width: 1
                            border.color: Tokens.borderDefault

                            Text {
                                anchors.centerIn: parent
                                text: ((modelData.name || modelData.toolkit_slug || modelData.slug || "?") + "").charAt(0).toUpperCase()
                                color: Tokens.accentBase
                                font.family: Tokens.fontDisplay
                                font.pixelSize: Math.round(14 * sf)
                                font.weight: Font.Bold
                            }
                        }

                        // Info column — fills remaining space
                        Column {
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            spacing: Math.round(2 * sf)

                            Text {
                                text: app.showCatalog
                                      ? (modelData.name || modelData.toolkit_slug || modelData.slug || "app")
                                      : ((modelData.alias && modelData.alias.length > 0)
                                         ? modelData.alias
                                         : (modelData.toolkit_slug + " · " + (modelData.id ? modelData.id.slice(0, 6) : "?")))
                                color: Tokens.textPrimary
                                font.family: Tokens.fontDisplay
                                font.pixelSize: Math.round(13 * sf)
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                                width: parent.width
                            }

                            Text {
                                text: app.showCatalog
                                      ? (modelData.slug || "")
                                      : ("estado: " + (modelData.status || "activa"))
                                color: Tokens.textMuted
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(10 * sf)
                                elide: Text.ElideRight
                                width: parent.width
                            }
                        }

                        // Catalog: Conectar button
                        LumenButton {
                            visible: app.showCatalog
                            Layout.alignment: Qt.AlignVCenter
                            sf: app.sf
                            label: "Conectar"
                            variant: "primary"
                            implicitWidth: Math.round(90 * sf)
                            implicitHeight: Math.round(30 * sf)
                            onClicked: app.connectApp(modelData.slug || modelData.toolkit_slug || modelData.name)
                        }

                        // Connections: chip + Renombrar button
                        Row {
                            visible: !app.showCatalog
                            Layout.alignment: Qt.AlignVCenter
                            spacing: Math.round(Tokens.spXs * sf)

                            LumenChip {
                                sf: app.sf
                                text: "✓ activa"
                                tone: "success"
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            Rectangle {
                                property bool _isRenaming: parent.parent.parent.parent.parent.isRenaming
                                width: Math.round(88 * sf)
                                height: Math.round(26 * sf)
                                radius: Math.round(Tokens.radiusSm * sf)
                                color: _isRenaming ? Tokens.accentSubtle : "transparent"
                                border.width: 1
                                border.color: _isRenaming ? Tokens.accentBase : Tokens.borderDefault
                                anchors.verticalCenter: parent.verticalCenter

                                Behavior on color {
                                    enabled: !Tokens.reduceMotion
                                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                }

                                Text {
                                    anchors.centerIn: parent
                                    text: parent._isRenaming ? "Cancelar" : "Renombrar"
                                    color: parent._isRenaming ? Tokens.accentBase : Tokens.textSecondary
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(10 * sf)
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        app.renamingId = (app.renamingId === modelData.id) ? "" : modelData.id;
                                    }
                                }
                            }
                        }
                    }
                }

                // ── Inline rename row ──
                Rectangle {
                    width: parent.width
                    height: parent.isRenaming ? Math.round(46 * sf) : 0
                    visible: parent.isRenaming
                    color: Tokens.bgCard
                    border.width: 1
                    border.color: Tokens.accentBase
                    radius: Math.round(Tokens.radiusSm * sf)

                    Row {
                        anchors.fill: parent
                        anchors.leftMargin: Math.round(Tokens.spMd * sf)
                        anchors.rightMargin: Math.round(Tokens.spMd * sf)
                        anchors.topMargin: Math.round(Tokens.spSm * sf)
                        anchors.bottomMargin: Math.round(Tokens.spSm * sf)
                        spacing: Math.round(Tokens.spSm * sf)

                        // Alias text field
                        Rectangle {
                            height: parent.height
                            width: parent.width - aliasConfirmBtn.width - Math.round(Tokens.spSm * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: Tokens.bgElevated
                            border.width: 1
                            border.color: aliasInput.activeFocus ? Tokens.accentBase : Tokens.borderDefault

                            Behavior on border.color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }

                            TextInput {
                                id: aliasInput
                                anchors.fill: parent
                                anchors.leftMargin: Math.round(Tokens.spSm * sf)
                                anchors.rightMargin: Math.round(Tokens.spSm * sf)
                                verticalAlignment: TextInput.AlignVCenter
                                color: Tokens.textPrimary
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(12 * sf)
                                clip: true
                                selectByMouse: true
                                text: modelData.alias || ""
                                onAccepted: {
                                    if (!app.renamingBusy && text.trim().length > 0) {
                                        app.renamingBusy = true;
                                        hermes.call("cmp-alias", "set_composio_connection_alias",
                                                    JSON.stringify({ connection_id: modelData.id, alias: text.trim() }));
                                    }
                                }

                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: aliasInput.text.length === 0
                                    text: "p.ej. Gmail · ventas"
                                    color: Tokens.textMuted
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(12 * sf)
                                }
                            }
                        }

                        LumenButton {
                            id: aliasConfirmBtn
                            sf: app.sf
                            label: app.renamingBusy ? "…" : "Guardar"
                            variant: "primary"
                            loading: app.renamingBusy
                            implicitWidth: Math.round(76 * sf)
                            implicitHeight: parent.height
                            anchors.verticalCenter: parent.verticalCenter
                            onClicked: {
                                var t = aliasInput.text.trim();
                                if (t.length === 0) return;
                                app.renamingBusy = true;
                                hermes.call("cmp-alias", "set_composio_connection_alias",
                                            JSON.stringify({ connection_id: modelData.id, alias: t }));
                            }
                        }
                    }
                }
            }
        }
    }
}
