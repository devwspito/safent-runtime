import QtQuick
import QtQuick.Controls
import "." // Tokens singleton — mandatory for Tokens.X access

// ── ProviderGate ──────────────────────────────────────────────────────────
// Gate fundamental al arrancar: si no hay provider activo (nativo via
// config.yaml O del vault legacy), pide UNO. CRÍTICO: cablea el catálogo
// NATIVO de hermes_cli (PROVIDER_REGISTRY, 37+ providers, incl. SUSCRIPCIONES
// con OAuth: Nous Portal device-code, OpenAI Codex/ChatGPT device-code, xAI
// SuperGrok loopback PKCE) — NO el catálogo Vault viejo de 7 kinds.
//
// Camino:
//   - api_key  →  configure_native_provider({provider_id, api_key, model})
//                 → escribe HERMES_HOME/.env (OPENAI_API_KEY, etc.) +
//                   config.yaml model.{provider,default}
//   - oauth    →  start_provider_oauth({provider_id})
//                 → device-code (Nous/Codex) o loopback PKCE (xAI),
//                   el daemon escribe config.yaml al aprobar.
//
// La key viaja SOLO por el bus al daemon (SecretsVault); nunca a HTTP/log.
Item {
    id: gate
    property var ui                       // root (sf + showToast + loggedIn)
    property bool ready: false
    property bool hasProvider: false
    property bool busy: false
    property bool dismissed: false
    property string errorMsg: ""
    property string activeAlias: ""

    anchors.fill: parent
    // Solo tras iniciar sesión: nunca debe taparse el wizard de onboarding ni el
    // LoginScreen (regresión del nuevo gate de arranque).
    visible: ready && !hasProvider && !dismissed && (ui ? ui.loggedIn : false)
    z: 100000

    readonly property real sf: ui ? ui.sf : 1.0

    // ── Catálogo NATIVO real (rellenado por list_native_providers) ─────────
    // Cada entrada: { id, name, auth_type, description?, env_vars }
    // auth_type ∈ {api_key, oauth_device_code, oauth_external, oauth_minimax}
    property var nativeCatalog: []
    property string filterText: ""

    // Sugerencias de modelo por provider id (editable después).
    readonly property var defaultModels: ({
        "openai-api": "gpt-5.4-nano", "anthropic-api": "claude-sonnet-4-6",
        "anthropic-oauth": "claude-sonnet-4-6", "claude-max": "claude-sonnet-4-6",
        "gemini": "gemini-2.0-flash", "deepseek": "deepseek-chat",
        "groq": "llama-3.3-70b-versatile", "mistral": "mistral-large-latest",
        "openrouter": "openai/gpt-5.4-nano", "copilot": "gpt-4o",
        "moonshot": "kimi-k2", "xai-api": "grok-4", "xai-oauth": "grok-4",
        "ollama": "llama3.1", "together": "", "cerebras": "",
        "nous": "Hermes-4-405B", "openai-codex": "gpt-5",
        "lmstudio": "", "openai_compatible": ""
    })

    // Estado de paneles
    property string mode: "list"   // list | apikey | oauth
    property var pick: ({})

    // ── OAuth (Nous device-code, Codex device-code, xAI loopback) ──
    property string oauthState: "idle"  // idle | starting | pending | approved | error
    property string oauthUserCode: ""
    property string oauthUrl: ""
    property string oauthSession: ""
    property string oauthError: ""

    Timer {
        id: oauthPoll
        interval: 3000; repeat: true; running: false
        onTriggered: hermes.call("gate-oauth-status", "get_provider_oauth_status",
                                 JSON.stringify({ session_id: gate.oauthSession }))
    }

    function check() {
        hermes.call("gate-getactive", "get_active_provider", "{}");
        hermes.call("gate-getnative", "get_native_active", "{}");
        hermes.call("gate-listnative", "list_native_providers", "{}");
    }

    function pickProvider(p) {
        errorMsg = ""; gate.pick = p;
        if (p.auth_type === "api_key") {
            mode = "apikey";
            keyField.text = "";
            modelField.text = defaultModels[p.id] || "";
            return;
        }
        // OAuth: Nous/Codex (device-code), xAI (loopback PKCE), Claude Max (TBD)
        if (p.id === "nous" || p.id === "openai-codex" || p.id === "xai-oauth"
            || p.id === "anthropic-oauth" || p.id === "claude-max") {
            startOauth(p);
            return;
        }
        errorMsg = p.name + " usa " + p.auth_type + " — todavía no cableado en el gate (próximamente Gemini/Qwen).";
    }

    function startOauth(p) {
        mode = "oauth";
        oauthState = "starting"; oauthError = ""; oauthUserCode = ""; oauthUrl = "";
        hermes.call("gate-oauth-start", "start_provider_oauth",
                    JSON.stringify({ provider_id: p.id }));
    }

    function cancelOauth() { oauthPoll.stop(); oauthState = "idle"; oauthSession = ""; mode = "list"; }

    function configureApiKey() {
        if (busy || keyField.text.trim().length === 0) {
            errorMsg = "Pega la API key";
            return;
        }
        busy = true; errorMsg = "";
        hermes.call("gate-native-cfg", "configure_native_provider", JSON.stringify({ draft_json: {
            provider_id: gate.pick.id,
            api_key: keyField.text.trim(),
            model: modelField.text.trim(),
            base_url: ""
        }}));
    }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "gate-getactive") {
                try {
                    var p = JSON.parse(jsonStr || "{}");
                    if (ok && p && p.provider_id !== undefined) {
                        gate.hasProvider = true;
                        gate.activeAlias = p.alias || "";
                    }
                } catch (e) {}
            } else if (reqId === "gate-getnative") {
                try {
                    var n = JSON.parse(jsonStr || "{}");
                    if (ok && n && n.provider_id) {
                        gate.hasProvider = true;
                        gate.activeAlias = n.alias || n.provider_id;
                    }
                } catch (e) {}
                gate.ready = true;
            } else if (reqId === "gate-listnative") {
                try { gate.nativeCatalog = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { gate.nativeCatalog = []; }
            } else if (reqId === "gate-native-cfg") {
                gate.busy = false;
                var rc = {}; try { rc = JSON.parse(jsonStr || "{}"); } catch (e) {}
                if (ok && rc.ok) {
                    gate.hasProvider = true;
                    gate.activeAlias = gate.pick.name || gate.pick.id;
                    if (gate.ui && gate.ui.showToast) gate.ui.showToast("Conectado: " + gate.activeAlias, "success");
                } else {
                    gate.errorMsg = "✕ " + (rc.error || jsonStr);
                }
            } else if (reqId === "gate-oauth-start") {
                try {
                    var o = JSON.parse(jsonStr || "{}");
                    if (!ok || o.error) {
                        gate.oauthState = "error";
                        gate.oauthError = o.error || jsonStr;
                        return;
                    }
                    gate.oauthSession = o.session_id || "";
                    gate.oauthState = "pending";
                    if (o.flow === "loopback" && o.auth_url) {
                        gate.oauthUserCode = "";
                        gate.oauthUrl = o.auth_url;
                        if (typeof root !== "undefined" && root.launchNative)
                            root.launchNative("chromium-browser --ozone-platform=wayland --no-sandbox --password-store=basic " + o.auth_url, "chromium");
                    } else {
                        gate.oauthUserCode = o.user_code || "";
                        gate.oauthUrl = o.verification_url || "";
                    }
                    oauthPoll.start();
                } catch (e) {
                    gate.oauthState = "error";
                    gate.oauthError = String(e);
                }
            } else if (reqId === "gate-oauth-status") {
                try {
                    var st = JSON.parse(jsonStr || "{}");
                    if (st.status === "approved") {
                        oauthPoll.stop();
                        gate.oauthState = "approved";
                        gate.hasProvider = true;
                        gate.activeAlias = gate.pick.name || gate.pick.id;
                        if (gate.ui && gate.ui.showToast)
                            gate.ui.showToast(gate.activeAlias + " conectado", "success");
                    } else if (st.status === "error") {
                        oauthPoll.stop();
                        gate.oauthState = "error";
                        gate.oauthError = st.error_message || "falló la autorización";
                    } else if (st.status === "unknown") {
                        oauthPoll.stop();
                        gate.oauthState = "error";
                        gate.oauthError = "sesión expirada — reinicia el flujo";
                    }
                } catch (e) { /* sigue sondeando */ }
            }
        }
    }

    Component.onCompleted: check()

    // ── Lista filtrada ─────────────────────────────────────────────────────
    function filtered() {
        var q = (gate.filterText || "").toLowerCase().trim();
        if (q.length === 0) return gate.nativeCatalog;
        var out = [];
        for (var i = 0; i < gate.nativeCatalog.length; i++) {
            var p = gate.nativeCatalog[i];
            if ((p.id || "").toLowerCase().indexOf(q) >= 0
                || (p.name || "").toLowerCase().indexOf(q) >= 0
                || (p.auth_type || "").toLowerCase().indexOf(q) >= 0)
                out.push(p);
        }
        return out;
    }

    function isOauth(p) {
        return p && p.auth_type && p.auth_type.indexOf("oauth") === 0;
    }

    function authBadge(p) {
        if (!p) return "";
        if (p.auth_type === "api_key") return "API key";
        if (p.id === "nous") return "Suscripción Nous Portal";
        if (p.id === "openai-codex") return "Suscripción ChatGPT (Codex)";
        if (p.id === "xai-oauth") return "Suscripción xAI SuperGrok";
        if (p.id === "anthropic-oauth" || p.id === "claude-max") return "Suscripción Claude Max";
        if (p.auth_type === "oauth_device_code") return "OAuth código";
        if (p.auth_type === "oauth_external") return "OAuth navegador";
        return p.auth_type || "";
    }

    // ── Fondo atenuado ─────────────────────────────────────────────────────
    Rectangle {
        anchors.fill: parent
        color: Qt.rgba(Tokens.bgVoid.r, Tokens.bgVoid.g, Tokens.bgVoid.b, 0.82)
        MouseArea { anchors.fill: parent; hoverEnabled: true; onClicked: gate.dismissed = true }
    }

    // ── Tarjeta principal ──────────────────────────────────────────────────
    // LumenCard handles bgCard + borderSubtle + radiusLg + shadow.
    // We override width/height to the constrained modal size.
    LumenCard {
        id: gateCard
        sf: gate.sf
        elevated: true
        pad: Math.round(24 * gate.sf)

        anchors.centerIn: parent
        width:  Math.min(parent.width  - Math.round(48 * sf), Math.round(720 * sf))
        height: Math.min(parent.height - Math.round(48 * sf), Math.round(620 * sf))

        // Stop backdrop click from dismissing when clicking inside the card
        MouseArea { anchors.fill: parent; hoverEnabled: true }

        Flickable {
            id: gateFlick
            anchors.fill: parent
            contentHeight: gateInnerCol.implicitHeight + Math.round(8 * gate.sf)
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
                width: Math.round(6 * gate.sf)
            }

            WheelHandler {
                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                onWheel: function(event) {
                    var f = gateFlick;
                    f.contentY = Math.max(0, Math.min(
                        Math.max(0, f.contentHeight - f.height),
                        f.contentY - event.angleDelta.y
                    ));
                }
            }

            Column {
                id: gateInnerCol
                width: parent.width
                spacing: Math.round(14 * gate.sf)

                // ── Header ──
                Row {
                    width: parent.width
                    spacing: Math.round(10 * gate.sf)

                    Rectangle {
                        width: Math.round(34 * gate.sf); height: width; radius: width / 2
                        color: Tokens.accentSubtle
                        anchors.verticalCenter: parent.verticalCenter

                        Text {
                            anchors.centerIn: parent
                            text: "✦"
                            font.family:    Tokens.fontDisplay
                            font.pixelSize: Math.round(18 * gate.sf)
                            color: Tokens.accentBase
                        }
                    }

                    Column {
                        width: parent.width - Math.round(54 * gate.sf)
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Math.round(2 * gate.sf)

                        Text {
                            text: "Conecta el cerebro de Hermes"
                            font.family:    Tokens.fontDisplay
                            font.pixelSize: Math.round(18 * gate.sf)
                            font.weight:    Font.DemiBold
                            color: Tokens.textPrimary
                        }
                        Text {
                            text: "Elige un proveedor — Hermes soporta "
                                + gate.nativeCatalog.length
                                + " nativos, incluyendo suscripciones (Nous, ChatGPT/Codex, Claude Max, xAI SuperGrok)."
                            font.family:    Tokens.fontBody
                            font.pixelSize: Math.round(12 * gate.sf)
                            color: Tokens.textSecondary
                            width: parent.width
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                // ─── MODE: LIST ─────────────────────────────────────────────
                Column {
                    width: parent.width
                    spacing: Math.round(10 * gate.sf)
                    visible: gate.mode === "list"

                    // Buscador
                    LumenInput {
                        id: searchInput
                        sf: gate.sf
                        width: parent.width
                        placeholder: "Busca: nous, codex, xai, claude, openai, gemini…"
                        onAccepted: {}
                    }
                    // Binding drives filterText from the alias property.
                    // A Binding item is used because onTextChanged on a property alias
                    // inside a child instantiation block is not valid QML syntax.
                    Binding {
                        target: gate
                        property: "filterText"
                        value: searchInput.text
                    }

                    // Sección destacada: SUSCRIPCIONES (OAuth)
                    Text {
                        text: "Suscripciones (OAuth — sin API key)"
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(11 * gate.sf)
                        font.weight:    Font.DemiBold
                        color: Tokens.textMuted
                        visible: gate.filterText.length === 0
                    }

                    Flow {
                        width: parent.width
                        spacing: Math.round(8 * gate.sf)
                        visible: gate.filterText.length === 0

                        Repeater {
                            model: gate.nativeCatalog
                            delegate: Loader {
                                active: gate.isOauth(modelData)
                                sourceComponent: providerChip
                                property var pchip: modelData
                                onLoaded: { item.pchip = pchip; }
                            }
                        }
                    }

                    // Lista scrollable de TODOS los providers (filtrada)
                    Text {
                        text: gate.filterText.length === 0
                            ? "Todos los proveedores (" + gate.nativeCatalog.length + ")"
                            : "Resultados (" + gate.filtered().length + ")"
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(11 * gate.sf)
                        font.weight:    Font.DemiBold
                        color: Tokens.textMuted
                    }

                    Rectangle {
                        width: parent.width
                        height: Math.round(280 * gate.sf)
                        radius: Math.round(Tokens.radiusMd * gate.sf)
                        color: Tokens.bgElevated
                        border.width: 1
                        border.color: Tokens.borderDefault
                        clip: true

                        ListView {
                            id: gateProvidersList
                            anchors.fill: parent
                            anchors.margins: Math.round(6 * gate.sf)
                            clip: true
                            spacing: Math.round(4 * gate.sf)
                            model: gate.filtered()
                            ScrollBar.vertical: ScrollBar {
                                policy: ScrollBar.AsNeeded
                                width: Math.round(6 * gate.sf)
                            }

                            WheelHandler {
                                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                                onWheel: function(event) {
                                    var f = gateProvidersList;
                                    f.contentY = Math.max(0, Math.min(
                                        Math.max(0, f.contentHeight - f.height),
                                        f.contentY - event.angleDelta.y
                                    ));
                                }
                            }

                            delegate: Rectangle {
                                width: ListView.view.width
                                height: Math.round(48 * gate.sf)
                                radius: Math.round(Tokens.radiusSm * gate.sf)
                                color: rowMA.containsMouse ? Tokens.accentGhost : "transparent"

                                Behavior on color {
                                    enabled: !Tokens.reduceMotion
                                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                }

                                Row {
                                    anchors.fill: parent
                                    anchors.leftMargin:  Math.round(12 * gate.sf)
                                    anchors.rightMargin: Math.round(12 * gate.sf)
                                    spacing: Math.round(10 * gate.sf)

                                    Column {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: parent.width - Math.round(140 * gate.sf)
                                        spacing: Math.round(2 * gate.sf)

                                        Text {
                                            text: modelData.name || modelData.id || ""
                                            font.family:    Tokens.fontBody
                                            font.pixelSize: Math.round(13 * gate.sf)
                                            font.weight:    Font.DemiBold
                                            color: Tokens.textPrimary
                                            elide: Text.ElideRight
                                            width: parent.width
                                        }
                                        Text {
                                            text: (modelData.id || "") + " · " + gate.authBadge(modelData)
                                            font.family:    Tokens.fontBody
                                            font.pixelSize: Math.round(10 * gate.sf)
                                            color: Tokens.textMuted
                                            elide: Text.ElideRight
                                            width: parent.width
                                        }
                                    }

                                    // Action button per row
                                    Rectangle {
                                        width:  Math.round(120 * gate.sf)
                                        height: Math.round(30 * gate.sf)
                                        radius: Math.round(Tokens.radiusSm * gate.sf)
                                        anchors.verticalCenter: parent.verticalCenter
                                        color: gate.isOauth(modelData)
                                            ? Tokens.accentBase
                                            : Tokens.bgCard
                                        border.width: gate.isOauth(modelData) ? 0 : 1
                                        border.color: Tokens.borderDefault

                                        Text {
                                            anchors.centerIn: parent
                                            text: gate.isOauth(modelData) ? "Conectar con OAuth" : "Usar API key"
                                            font.family:    Tokens.fontBody
                                            font.pixelSize: Math.round(11 * gate.sf)
                                            font.weight:    Font.DemiBold
                                            color: gate.isOauth(modelData) ? Tokens.textOnAccent : Tokens.textPrimary
                                        }
                                    }
                                }

                                MouseArea {
                                    id: rowMA
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: gate.pickProvider(modelData)
                                }
                            }
                        }
                    }

                    // Más tarde
                    Row {
                        width: parent.width
                        spacing: Math.round(10 * gate.sf)
                        Item { width: parent.width - laterBtn.width - Math.round(12 * gate.sf); height: 1 }
                        LumenButton {
                            id: laterBtn
                            sf: gate.sf
                            variant: "secondary"
                            label: "Más tarde"
                            onClicked: gate.dismissed = true
                        }
                    }
                }

                // ─── MODE: APIKEY ────────────────────────────────────────────
                Column {
                    width: parent.width
                    spacing: Math.round(10 * gate.sf)
                    visible: gate.mode === "apikey"

                    // Back + title row
                    Row {
                        width: parent.width
                        spacing: Math.round(8 * gate.sf)

                        LumenButton {
                            sf: gate.sf
                            variant: "ghost"
                            label: "‹ Volver"
                            onClicked: { gate.mode = "list"; gate.errorMsg = ""; }
                        }

                        Text {
                            // FIX: added || "" fallback — was emitting [undefined] to QString warning
                            // when gate.pick was {} (initial state, .name and .id both undefined).
                            text: (gate.pick.name || gate.pick.id || "")
                            font.family:    Tokens.fontDisplay
                            font.pixelSize: Math.round(15 * gate.sf)
                            font.weight:    Font.DemiBold
                            color: Tokens.textPrimary
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    Text {
                        text: "Pega tu API key — Hermes la guardará en "
                            + (gate.pick.env_vars && gate.pick.env_vars.length > 0
                               ? gate.pick.env_vars[0]
                               : "el secrets vault") + "."
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(11 * gate.sf)
                        color: Tokens.textSecondary
                        width: parent.width
                        wrapMode: Text.WordWrap
                    }

                    // API key
                    Column {
                        width: parent.width
                        spacing: Math.round(5 * gate.sf)

                        Text {
                            text: "Clave API"
                            font.family:    Tokens.fontBody
                            font.pixelSize: Math.round(11 * gate.sf)
                            color: Tokens.textMuted
                        }

                        LumenInput {
                            id: keyField
                            sf: gate.sf
                            width: parent.width
                            placeholder: "sk-…"
                            password: true
                        }
                    }

                    // Modelo
                    Column {
                        width: parent.width
                        spacing: Math.round(5 * gate.sf)

                        Text {
                            text: "Modelo (opcional)"
                            font.family:    Tokens.fontBody
                            font.pixelSize: Math.round(11 * gate.sf)
                            color: Tokens.textMuted
                        }

                        LumenInput {
                            id: modelField
                            sf: gate.sf
                            width: parent.width
                            placeholder: ""
                        }
                    }

                    // Error
                    Text {
                        visible: gate.errorMsg.length > 0
                        width: parent.width
                        text: gate.errorMsg
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(12 * gate.sf)
                        color: Tokens.dangerBase
                        wrapMode: Text.WordWrap
                    }

                    // Actions
                    Row {
                        width: parent.width
                        spacing: Math.round(10 * gate.sf)
                        Item { width: parent.width - connectBtn.width - Math.round(12 * gate.sf); height: 1 }
                        LumenButton {
                            id: connectBtn
                            sf: gate.sf
                            variant: "primary"
                            loading: gate.busy
                            label: gate.busy ? "Conectando…" : "Conectar"
                            onClicked: gate.configureApiKey()
                        }
                    }
                }

                // ─── MODE: OAUTH ─────────────────────────────────────────────
                Column {
                    width: parent.width
                    spacing: Math.round(10 * gate.sf)
                    visible: gate.mode === "oauth"

                    // Back + title row
                    Row {
                        width: parent.width
                        spacing: Math.round(8 * gate.sf)

                        LumenButton {
                            sf: gate.sf
                            variant: "ghost"
                            label: "‹ Volver"
                            onClicked: gate.cancelOauth()
                        }

                        Text {
                            // FIX: added || "" fallback — was emitting [undefined] to QString warning
                            // when gate.pick was {} (initial state, .name and .id both undefined).
                            text: (gate.pick.name || gate.pick.id || "")
                            font.family:    Tokens.fontDisplay
                            font.pixelSize: Math.round(15 * gate.sf)
                            font.weight:    Font.DemiBold
                            color: Tokens.textPrimary
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    // Estado: starting
                    Text {
                        visible: gate.oauthState === "starting"
                        text: "Iniciando el flujo OAuth…"
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(12 * gate.sf)
                        color: Tokens.textSecondary
                    }

                    // Estado: pending (device-code o loopback)
                    Column {
                        visible: gate.oauthState === "pending"
                        width: parent.width
                        spacing: Math.round(8 * gate.sf)

                        // Device-code (Nous, Codex)
                        Column {
                            visible: gate.oauthUserCode.length > 0
                            width: parent.width
                            spacing: Math.round(6 * gate.sf)

                            Text {
                                text: "1.  Abre " + gate.oauthUrl + " en tu navegador"
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(12 * gate.sf)
                                color: Tokens.textPrimary
                                width: parent.width; wrapMode: Text.WordWrap
                            }
                            Text {
                                text: "2.  Introduce este código:"
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(12 * gate.sf)
                                color: Tokens.textPrimary
                            }

                            Rectangle {
                                width: parent.width; height: Math.round(54 * gate.sf)
                                radius: Math.round(Tokens.radiusMd * gate.sf)
                                color: Tokens.bgElevated
                                border.width: 1; border.color: Tokens.accentBase

                                Text {
                                    anchors.centerIn: parent
                                    text: gate.oauthUserCode
                                    font.family:      Tokens.fontDisplay
                                    font.pixelSize:   Math.round(22 * gate.sf)
                                    font.weight:      Font.DemiBold
                                    font.letterSpacing: 4
                                    color: Tokens.accentBase
                                }
                            }

                            LumenButton {
                                sf: gate.sf
                                variant: "primary"
                                label: "Abrir navegador"
                                onClicked: {
                                    if (typeof root !== "undefined" && root.launchNative && gate.oauthUrl)
                                        root.launchNative(
                                            "chromium-browser --ozone-platform=wayland --no-sandbox --password-store=basic " + gate.oauthUrl,
                                            "chromium"
                                        );
                                }
                            }
                        }

                        // Loopback (xAI) — el navegador ya se abrió
                        Column {
                            visible: gate.oauthUserCode.length === 0 && gate.oauthUrl.length > 0
                            width: parent.width
                            spacing: Math.round(6 * gate.sf)

                            Text {
                                text: "Hemos abierto tu navegador en la página de autorización de "
                                    + (gate.pick.name || "el proveedor") + "."
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(12 * gate.sf)
                                color: Tokens.textPrimary
                                width: parent.width; wrapMode: Text.WordWrap
                            }
                            Text {
                                text: "Termina el flujo allí — Hermes detectará la aprobación automáticamente."
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(11 * gate.sf)
                                color: Tokens.textSecondary
                                width: parent.width; wrapMode: Text.WordWrap
                            }
                        }

                        Text {
                            text: "Esperando aprobación…"
                            font.family:    Tokens.fontBody
                            font.pixelSize: Math.round(11 * gate.sf)
                            color: Tokens.textMuted
                        }
                    }

                    // Estado: error
                    Text {
                        visible: gate.oauthState === "error"
                        text: "✕ " + gate.oauthError
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(12 * gate.sf)
                        color: Tokens.dangerBase
                        width: parent.width; wrapMode: Text.WordWrap
                    }
                }
            } // gateInnerCol
        } // Flickable
    } // LumenCard

    // ── Chip reutilizable para la sección de suscripciones ────────────────
    Component {
        id: providerChip
        Rectangle {
            property var pchip: ({})
            height: Math.round(36 * gate.sf)
            width:  chipLbl.implicitWidth + Math.round(28 * gate.sf)
            radius: Math.round(Tokens.radiusSm * gate.sf)
            color: chipMa.containsMouse ? Tokens.accentHover : Tokens.accentBase

            Behavior on color {
                enabled: !Tokens.reduceMotion
                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
            }

            Text {
                id: chipLbl
                anchors.centerIn: parent
                text: pchip.name || pchip.id || ""
                font.family:    Tokens.fontBody
                font.pixelSize: Math.round(12 * gate.sf)
                font.weight:    Font.DemiBold
                color: Tokens.textOnAccent
            }
            MouseArea {
                id: chipMa
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: gate.pickProvider(pchip)
            }
        }
    }
}
