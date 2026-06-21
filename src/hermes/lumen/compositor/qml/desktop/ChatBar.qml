import QtQuick
import QtQuick.Layouts
import "." // Tokens singleton — required for Tokens.X references

Rectangle {
    id: chatBar
    height: chatExpanded ? (chatFullScreen ? parent.height : Math.min(Math.round(450 * root.sf), parent.height - Math.round(70 * root.sf))) : Math.round(60 * root.sf)
    radius: root.radiusLg
    color: "transparent"
    clip: true
    z: 10

    // PERF: No height animation — instant snap to avoid continuous repainting

    property bool chatExpanded: false
    property bool chatFullScreen: false
    // Fade in content when expanding (cheap — only affects opacity, not layout)
    property real chatContentOpacity: chatExpanded ? 1.0 : 0.0
    property bool isSending: false
    property var messages: []
    property string selectedAgent: "main"
    property var agentList: [{ id: "main", name: "Hermes", description: "Default AI" }]
    property bool showAgentPicker: false
    // Streaming state
    property string streamingContent: ""
    property bool isStreaming: false
    property var streamingSteps: []
    property var activePlan: null
    property var commandSuggestions: ["/help", "/new", "/status", "/think", "/usage", "/compact"]
    property bool showSuggestions: false
    property var chatHistory: []
    property int historyIndex: -1

    // Real-time active model (polled from /api/providers)
    property string activeModel: ""
    property string activeProvider: ""

    // Multi-agent state
    property var liveAgentRuns: ({})
    property bool showFanOutModal: false
    property var fanOutChecked: ({})

    // Per-conversation work folder
    property string currentWorkFolder: ""

    // Conversation management
    property var conversations: []
    property string currentConversationId: ""
    property bool showConversationList: false

    // ── Cableado REAL al daemon Hermes (conversación + polling de la respuesta) ──
    property string chatConvId: ""
    property int __pollCount: 0
    // Nº de respuestas del asistente YA consumidas de esta conversación. El
    // poller solo acepta una respuesta NUEVA (count > consumidas) — antes
    // re-adjuntaba la respuesta del turno anterior como si fuera la actual
    // (se veía la misma frase duplicada y la respuesta real nunca aparecía).
    property int __assistantSeen: 0
    // Tracks whether we received at least one ChatDelta for the active conv.
    // Used to deduplicate: if streaming already showed the content, the poller
    // must not add a second assistant bubble when it commits the persisted reply.
    property bool __streamingReceived: false

    Timer {
        id: replyPollTimer
        // Lowered from 1500 ms to 250 ms: faster fallback commit when no D-Bus
        // streaming arrives (e.g. non-Nous engine, D-Bus unavailable, cold start).
        interval: 250; repeat: true; running: false
        onTriggered: {
            __pollCount++;
            if (__pollCount > 240) { stop(); isSending = false; isStreaming = false; return; }
            hermes.call("getconv-" + chatConvId, "get_conversation", JSON.stringify({ conversation_id: chatConvId }));
        }
    }
    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId.indexOf("getconv-") !== 0) return;
            if (!ok || !jsonStr) return;
            try {
                var conv = JSON.parse(jsonStr);
                var arr = conv.messages || conv.turns || (Array.isArray(conv) ? conv : []);
                var assist = [];
                for (var i = 0; i < arr.length; i++) {
                    var r = (arr[i].role || arr[i].author || "").toLowerCase();
                    var c = arr[i].content || arr[i].text || "";
                    if ((r === "assistant" || r === "agent" || r === "hermes") && c) assist.push(c);
                }
                if (assist.length > __assistantSeen) {
                    // The daemon persisted the final reply. Replace the streaming
                    // bubble with the committed message (single source of truth).
                    // If we showed streaming content, clear it — the committed
                    // message replaces it so we don't duplicate.
                    var finalContent = assist[assist.length - 1];
                    var cur = messages.slice();
                    cur.push({ role: "assistant", content: finalContent, agent: getAgentName() });
                    messages = cur;
                    __assistantSeen = assist.length;
                    streamingContent = "";
                    streamingSteps = [];
                    __streamingReceived = false;
                    replyPollTimer.stop();
                    isSending = false; isStreaming = false;
                }
            } catch(e) { /* sigue sondeando */ }
        }
    }

    // ── D-Bus streaming signals (spec streaming-dbus) ────────────────────────
    // ChatDelta: incremental token batch emitted by the LLM during generation.
    // ChatStreamEnd: generation complete — the daemon will shortly persist the
    // final reply; the poller will commit it on the next tick.
    // Both signals arrive on hermes (the D-Bus proxy in main.qml).
    Connections {
        target: hermes

        function onChatDelta(conversationId, seq, text) {
            // Filter to the active conversation only.
            if (conversationId !== chatConvId || !chatConvId) return;
            // First delta: mark streaming started.
            if (!__streamingReceived) {
                __streamingReceived = true;
                isStreaming = true;
            }
            streamingContent = streamingContent + text;
        }

        function onChatStreamEnd(conversationId) {
            // Filter to the active conversation only.
            if (conversationId !== chatConvId || !chatConvId) return;
            // Generation complete on the engine side. The daemon will persist
            // the reply asynchronously; we let the poller commit it. The
            // streaming bubble stays visible until the poller replaces it.
            // isStreaming stays true so the typing cursor remains until commit.
        }
    }

    // Generate a slug from the first message for a readable folder name
    function generateWorkFolder(firstMsg) {
        // Take first ~30 chars, lowercase, replace non-alphanum with underscore
        var slug = firstMsg.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").substring(0, 30);
        if (!slug) slug = "task";
        // Add timestamp for uniqueness
        var d = new Date();
        var ts = d.getFullYear() + "-" +
            ("0" + (d.getMonth() + 1)).slice(-2) + "-" +
            ("0" + d.getDate()).slice(-2) + "_" +
            ("0" + d.getHours()).slice(-2) +
            ("0" + d.getMinutes()).slice(-2);
        return slug + "_" + ts;
    }

    // Start a new conversation with a fresh work folder
    function startNewConversation() {
        currentWorkFolder = "";
        messages = [];
        streamingContent = "";
        streamingSteps = [];
        activePlan = null;
    }

    // ── Artifact detection ──────────────────────────────────────────────────────
    // Allowlisted workspace roots. Only paths under these prefixes are ever loaded.
    readonly property var _workspaceRoots: [
        "/var/lib/hermes/workspace/",
        "/var/lib/hermes/hermes-home/cache/"
    ]

    readonly property var _imageExts: ["png","jpg","jpeg","webp","gif","svg"]
    readonly property var _docExts: [
        "pdf","docx","doc","pptx","ppt","xlsx","xls",
        "odt","ods","odp","txt","csv","md","zip","tar","gz","7z","rar"
    ]

    // Returns true if path is under an allowed workspace root.
    function _isAllowedPath(p) {
        for (var i = 0; i < _workspaceRoots.length; i++) {
            if (p.indexOf(_workspaceRoots[i]) === 0) return true;
        }
        return false;
    }

    // Returns the lowercase extension of a path (without the dot), or "".
    function _extOf(p) {
        var dot = p.lastIndexOf(".");
        if (dot < 0) return "";
        return p.substring(dot + 1).toLowerCase().split(/[?#]/)[0];
    }

    // Returns "image", "document", or "" for a given path.
    function _classifyPath(p) {
        var ext = _extOf(p);
        if (_imageExts.indexOf(ext) >= 0) return "image";
        if (_docExts.indexOf(ext) >= 0) return "document";
        return "";
    }

    // Extract all validated artifact references from a message.
    // Returns an array of { path, kind } objects where kind is "image" or "document".
    // Accepts:
    //   ![alt](MEDIA:/var/lib/hermes/workspace/foo.png)
    //   MEDIA:/var/lib/hermes/workspace/foo.pdf
    //   /var/lib/hermes/workspace/foo.xlsx  (bare absolute path with known ext)
    // Only paths under _workspaceRoots with a known extension are returned.
    function extractArtifacts(text) {
        if (!text) return [];
        var artifacts = [];
        var seen = {};

        function addPath(p) {
            // Strip trailing punctuation that may have been concatenated
            p = p.replace(/[.,;:)>\]]+$/, "");
            if (!_isAllowedPath(p)) return;
            if (seen[p]) return;
            var kind = _classifyPath(p);
            if (!kind) return;
            seen[p] = true;
            artifacts.push({ path: p, kind: kind });
        }

        // Pattern 1: markdown image  ![anything](MEDIA:/path)
        var mdRe = /!\[[^\]]*\]\(MEDIA:(\/[^\s)]+)\)/g;
        var m;
        while ((m = mdRe.exec(text)) !== null) { addPath(m[1]); }

        // Pattern 2: bare MEDIA:/path token
        var mediaRe = /\bMEDIA:(\/[^\s),\]]+)/g;
        while ((m = mediaRe.exec(text)) !== null) { addPath(m[1]); }

        // Pattern 3: bare absolute path under allowed roots (e.g. /var/lib/hermes/workspace/foo.pdf)
        var bareRe = /(\/var\/lib\/hermes\/(?:workspace|hermes-home\/cache)\/[^\s),\]]+)/g;
        while ((m = bareRe.exec(text)) !== null) { addPath(m[1]); }

        return artifacts;
    }

    // Legacy helper — returns only image paths (used by the prior inline renderer).
    // Kept for back-compat; now delegates to extractArtifacts.
    function extractMediaPaths(text) {
        var all = extractArtifacts(text);
        var imgs = [];
        for (var i = 0; i < all.length; i++) {
            if (all[i].kind === "image") imgs.push(all[i].path);
        }
        return imgs;
    }

    // Strip all artifact tokens from text before markdown rendering.
    function stripMediaRefs(text) {
        if (!text) return text;
        // Remove markdown image tokens with MEDIA scheme
        var s = text.replace(/!\[[^\]]*\]\(MEDIA:\/[^)]+\)/g, "");
        // Remove bare MEDIA: tokens
        s = s.replace(/\bMEDIA:\/\S+/g, "");
        // Remove bare absolute workspace paths with known extensions
        s = s.replace(/\/var\/lib\/hermes\/(?:workspace|hermes-home\/cache)\/[^\s),\]]+/g, "");
        // Collapse runs of blank lines left behind
        s = s.replace(/\n{3,}/g, "\n\n");
        return s.trim();
    }

    // Returns a short display label for a document type.
    function _docTypeLabel(ext) {
        var map = {
            "pdf": "PDF", "docx": "Word", "doc": "Word",
            "pptx": "PowerPoint", "ppt": "PowerPoint",
            "xlsx": "Excel", "xls": "Excel",
            "odt": "Writer", "ods": "Calc", "odp": "Impress",
            "txt": "Text", "csv": "CSV", "md": "Markdown",
            "zip": "ZIP", "tar": "Archive", "gz": "Archive",
            "7z": "Archive", "rar": "Archive"
        };
        return map[ext] || ext.toUpperCase();
    }

    // Returns the accent color for a document type label.
    function _docTypeColor(ext) {
        var map = {
            "pdf": "#ef4444",
            "docx": "#3b82f6", "doc": "#3b82f6",
            "pptx": "#f97316", "ppt": "#f97316",
            "xlsx": "#22c55e", "xls": "#22c55e",
            "odt": "#60a5fa", "ods": "#34d399", "odp": "#fb923c",
            "txt": "#94a3b8", "csv": "#34d399", "md": "#a78bfa",
            "zip": "#f59e0b", "tar": "#f59e0b", "gz": "#f59e0b",
            "7z": "#f59e0b", "rar": "#f59e0b"
        };
        return map[ext] || "#94a3b8";
    }

    // Returns the basename of a path.
    function _basename(p) {
        var idx = p.lastIndexOf("/");
        return idx >= 0 ? p.substring(idx + 1) : p;
    }

    // Builds the gateway download URL for a workspace file.
    // The noVNC tunnel serves /files/<basename> relative to its own origin.
    // From the compositor (not a browser context) we cannot trigger a browser
    // download directly, so Descargar copies this URL to the clipboard instead.
    function _downloadUrl(p) {
        var name = _basename(p);
        return "/files/" + encodeURIComponent(name);
    }

    // Simple markdown to StyledText converter
    function mdToStyled(text) {
        if (!text) return "";
        var s = stripMediaRefs(text);
        // Escape HTML
        s = s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        // Code blocks: ```...``` -> monospace
        s = s.replace(/```[\s\S]*?```/g, function(m) {
            var code = m.replace(/```\w*\n?/g, "").replace(/```/g, "");
            return "<br><font color='#94a3b8' face='monospace'>" + code.replace(/\n/g, "<br>") + "</font><br>";
        });
        // Inline code
        s = s.replace(/`([^`]+)`/g, "<font color='#93c5fd' face='monospace'>$1</font>");
        // Bold
        s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
        // Italic
        s = s.replace(/\*([^*]+)\*/g, "<i>$1</i>");
        // Bullet points
        s = s.replace(/\n- /g, "\n• ");
        s = s.replace(/\n(\d+)\. /g, "\n$1. ");
        // Line breaks
        s = s.replace(/\n/g, "<br>");
        // Clean branding
        s = s.replace(/Hermes/g, "Hermes");
        s = s.replace(/AInux OS/g, "LumenSO");
        s = s.replace(/AInux/g, "LumenSO");
        return s;
    }

    Component.onCompleted: { loadAgents(); loadHistory(); loadActiveModel(); loadConversations(); }

    // Poll active model every 10s
    Timer {
        interval: 30000; running: true; repeat: true
        onTriggered: loadActiveModel()
    }

    // Cableado REAL al daemon (D-Bus). El provider activo y los agentes vienen
    // del daemon, no del :7777 muerto de WhaleOS.
    function loadActiveModel() { hermes.call("chat-provider", "get_active_provider", "{}"); }
    function loadAgents() { hermes.call("chat-agents", "list_agents", "{}"); }
    function loadHistory() { /* conversación fresca por chatConvId; sin historial legacy de WhaleOS */ }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "chat-provider") {
                try {
                    var p = JSON.parse(jsonStr || "{}");
                    if (p && p.provider_id !== undefined) { activeProvider = p.alias || p.kind || ""; activeModel = p.default_model || ""; }
                    else { activeProvider = ""; activeModel = ""; }
                } catch (e) { activeProvider = ""; activeModel = ""; }
            } else if (reqId === "chat-agents") {
                try {
                    var arr = JSON.parse(jsonStr || "[]");
                    var out = [];
                    for (var i = 0; i < arr.length; i++) out.push({ id: arr[i].agent_id, name: arr[i].name });
                    agentList = out;
                } catch (e) {}
            }
        }
    }

    function getAgentName() {
        for (var i = 0; i < agentList.length; i++) {
            if (agentList[i].id === selectedAgent) return agentList[i].name;
        }
        return "Hermes";
    }

    function getWorkingStatus() {
        // Dynamic status like dashboard: Running tool, Generating, Thinking (round N)
        for (var i = streamingSteps.length - 1; i >= 0; i--) {
            if (streamingSteps[i].type === "tool" && streamingSteps[i].status === "running") {
                var label = streamingSteps[i].name || "tool";
                return "Running: " + label;
            }
        }
        if (streamingContent.length > 0) return "Generating...";
        for (var j = 0; j < streamingSteps.length; j++) {
            if (streamingSteps[j].type === "thinking" && !streamingSteps[j].done) {
                if (streamingSteps[j].iteration > 1) return "Thinking (round " + streamingSteps[j].iteration + ")...";
                return "Thinking...";
            }
        }
        if (streamingSteps.length > 0) return "Processing results...";
        return "Thinking...";
    }

    // ── Glass background (only when expanded) ──
    Rectangle {
        anchors.fill: parent; radius: parent.radius
        visible: chatExpanded
        opacity: chatContentOpacity
        color: Qt.rgba(Tokens.bgSurface.r, Tokens.bgSurface.g, Tokens.bgSurface.b, 0.96)
        border.color: Tokens.borderSubtle; border.width: 1
    }
    // Inner top highlight for glass depth
    Rectangle {
        anchors.top: parent.top; anchors.topMargin: 1
        anchors.left: parent.left; anchors.leftMargin: Math.round(20 * root.sf)
        anchors.right: parent.right; anchors.rightMargin: Math.round(20 * root.sf)
        height: 1; radius: 1; visible: chatExpanded; opacity: chatContentOpacity
        color: Qt.rgba(Tokens.borderSubtle.r, Tokens.borderSubtle.g, Tokens.borderSubtle.b, 0.06)
    }

    // ── Chat Header (expanded) ──
    Rectangle {
        id: chatHeader
        anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right
        height: Math.round(52 * root.sf); visible: chatExpanded
        opacity: chatContentOpacity
        color: Qt.rgba(Tokens.bgElevated.r, Tokens.bgElevated.g, Tokens.bgElevated.b, 0.98)
        radius: Math.round(Tokens.radiusLg * root.sf)

        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: parent.radius; color: parent.color }
        // Accent gradient line at bottom of header
        Rectangle {
            anchors.bottom: parent.bottom; width: parent.width; height: 1
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: "transparent" }
                GradientStop { position: 0.2; color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.25) }
                GradientStop { position: 0.5; color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.35) }
                GradientStop { position: 0.8; color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.25) }
                GradientStop { position: 1.0; color: "transparent" }
            }
        }

        RowLayout {
            anchors.fill: parent; anchors.leftMargin: Math.round(16 * root.sf); anchors.rightMargin: Math.round(12 * root.sf); spacing: Math.round(10 * root.sf)

            Text { text: "◉"; font.pixelSize: Math.round(16 * root.sf); color: Tokens.accentBase; Layout.alignment: Qt.AlignVCenter }

            Text {
                text: getAgentName()
                font.family: Tokens.fontDisplay
                font.pixelSize: Math.round(14 * root.sf); font.weight: Font.Medium; color: Tokens.textPrimary
            }

            // Active model badge
            Rectangle {
                visible: activeModel.length > 0
                width: modelBadgeText.width + Math.round(12 * root.sf); height: Math.round(18 * root.sf); radius: Math.round(9 * root.sf)
                color: Tokens.accentSubtle
                Text {
                    id: modelBadgeText; anchors.centerIn: parent
                    text: activeModel
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(9 * root.sf); font.weight: Font.Medium; color: Tokens.accentBase
                }
            }

            Rectangle {
                width: agentSelectorRow.width + Math.round(12 * root.sf); height: Math.round(20 * root.sf); radius: Math.round(4 * root.sf)
                color: agentPickerMa.containsMouse ? Tokens.accentSubtle : Tokens.accentGhost
                visible: true

                Row { id: agentSelectorRow; anchors.centerIn: parent; spacing: Math.round(3 * root.sf)
                    Text { text: "▾"; font.pixelSize: Math.round(8 * root.sf); color: Tokens.accentBase; anchors.verticalCenter: parent.verticalCenter }
                    Text { text: "switch"; font.family: Tokens.fontBody; font.pixelSize: Math.round(9 * root.sf); color: Tokens.accentBase; anchors.verticalCenter: parent.verticalCenter }
                }

                MouseArea {
                    id: agentPickerMa; anchors.fill: parent; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: { if (!showAgentPicker) loadAgents(); showAgentPicker = !showAgentPicker; }
                }
            }

            Item { width: Math.round(8 * root.sf); height: 1 }  // spacer (Row plano; Layout.fillWidth no aplica)

            Text {
                text: messages.length + " msgs"
                font.pixelSize: Math.round(10 * root.sf); color: root.textMuted
                visible: messages.length > 0
            }

            // Stop button (visible when streaming)
            Rectangle {
                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf); radius: Math.round(6 * root.sf)
                color: stopMa.containsMouse ? Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.30) : Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.15)
                visible: isSending
                Text { anchors.centerIn: parent; text: "■"; font.pixelSize: Math.round(10 * root.sf); color: Tokens.dangerBase }
                MouseArea { id: stopMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { isSending = false; isStreaming = false; } }
            }

            // New Chat button
            Rectangle {
                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf); radius: Math.round(6 * root.sf)
                color: newChatMa.containsMouse ? Tokens.accentSubtle : "transparent"
                visible: !isSending

                Text { anchors.centerIn: parent; text: "+"; font.pixelSize: Math.round(16 * root.sf); font.weight: Font.Bold; color: Tokens.accentBase }

                MouseArea {
                    id: newChatMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: newChat()
                }
            }

            // Conversation history button
            Rectangle {
                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf); radius: Math.round(6 * root.sf)
                color: convListMa.containsMouse ? Qt.rgba(Tokens.borderSubtle.r, Tokens.borderSubtle.g, Tokens.borderSubtle.b, 0.08) : "transparent"
                visible: !isSending

                Text { anchors.centerIn: parent; text: "\u2261"; font.pixelSize: Math.round(14 * root.sf); color: showConversationList ? Tokens.accentBase : Tokens.textMuted }

                MouseArea {
                    id: convListMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: { loadConversations(); showConversationList = !showConversationList; showAgentPicker = false; }
                }
            }

            Rectangle {
                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf); radius: Math.round(6 * root.sf)
                color: fsMa.containsMouse ? Qt.rgba(Tokens.borderSubtle.r, Tokens.borderSubtle.g, Tokens.borderSubtle.b, 0.08) : "transparent"

                Text { anchors.centerIn: parent; text: chatFullScreen ? "▣" : "□"; font.pixelSize: Math.round(13 * root.sf); color: chatFullScreen ? Tokens.accentBase : Tokens.textMuted }

                MouseArea { id: fsMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { if (chatFullScreen) { chatFullScreen = false; chatExpanded = false; } else { chatFullScreen = true; } } }
            }

            Rectangle {
                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf); radius: Math.round(6 * root.sf)
                color: collMa.containsMouse ? Qt.rgba(Tokens.borderSubtle.r, Tokens.borderSubtle.g, Tokens.borderSubtle.b, 0.08) : "transparent"
                Text { anchors.centerIn: parent; text: "▾"; font.pixelSize: Math.round(12 * root.sf); color: root.textSecondary }
                MouseArea { id: collMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { chatExpanded = false; chatFullScreen = false; } }
            }
        }
    }

    // ── Agent Picker Dropdown ──
    Rectangle {
        id: agentPicker
        anchors.top: chatHeader.bottom; anchors.topMargin: Math.round(4 * root.sf)
        anchors.left: parent.left; anchors.leftMargin: Math.round(12 * root.sf)
        width: Math.round(220 * root.sf); height: apCol.height + Math.round(12 * root.sf)
        radius: root.radiusMd; z: 100
        color: Qt.rgba(Tokens.bgElevated.r, Tokens.bgElevated.g, Tokens.bgElevated.b, 0.97)
        border.color: Tokens.borderDefault; border.width: 1
        visible: showAgentPicker && chatExpanded

        Column {
            id: apCol; anchors.left: parent.left; anchors.right: parent.right
            anchors.top: parent.top; anchors.margins: Math.round(6 * root.sf); spacing: 2

            Text { text: "Select Agent"; font.pixelSize: Math.round(10 * root.sf); font.weight: Font.Bold; color: root.textMuted; leftPadding: Math.round(6 * root.sf); bottomPadding: Math.round(4 * root.sf) }

            Repeater {
                model: agentList

                Rectangle {
                    width: apCol.width; height: Math.round(32 * root.sf); radius: Math.round(4 * root.sf)
                    color: modelData.id === selectedAgent ? Tokens.accentSubtle :
                           apItemMa.containsMouse ? Qt.rgba(Tokens.borderSubtle.r, Tokens.borderSubtle.g, Tokens.borderSubtle.b, 0.08) : "transparent"

                    RowLayout {
                        anchors.fill: parent; anchors.leftMargin: Math.round(8 * root.sf); anchors.rightMargin: Math.round(8 * root.sf); spacing: Math.round(6 * root.sf)

                        Rectangle {
                            width: Math.round(6 * root.sf); height: Math.round(6 * root.sf); radius: width / 2
                            color: modelData.enabled !== false ? Tokens.successBase : Tokens.textMuted
                        }

                        Column { Layout.fillWidth: true; spacing: 0
                            Text { text: modelData.name || modelData.id; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium; color: Tokens.textPrimary }
                        }

                        Text {
                            visible: modelData.id === selectedAgent
                            text: "✓"; font.pixelSize: Math.round(11 * root.sf); color: Tokens.accentBase
                        }
                    }

                    MouseArea {
                        id: apItemMa; anchors.fill: parent; hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { selectedAgent = modelData.id; showAgentPicker = false; }
                    }
                }
            }

            // Divider
            Rectangle { width: parent.width - Math.round(12 * root.sf); height: 1; color: Tokens.borderSubtle; anchors.horizontalCenter: parent.horizontalCenter }

            // Fan-Out (Multi-Agent) option
            Rectangle {
                width: apCol.width; height: Math.round(32 * root.sf); radius: Math.round(4 * root.sf)
                color: fanoutMa.containsMouse ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.10) : "transparent"

                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: Math.round(8 * root.sf); anchors.rightMargin: Math.round(8 * root.sf); spacing: Math.round(6 * root.sf)
                    Canvas {
                        width: Math.round(14 * root.sf); height: Math.round(14 * root.sf)
                        property real s: root.sf
                        // Fork icon painted in accent color
                        property color iconColor: Tokens.accentBase
                        onPaint: {
                            var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                            ctx.save(); ctx.scale(s, s);
                            ctx.strokeStyle = Qt.rgba(iconColor.r, iconColor.g, iconColor.b, 0.85); ctx.lineWidth = 1.4; ctx.lineCap = "round";
                            ctx.beginPath(); ctx.moveTo(7, 0); ctx.lineTo(7, 6); ctx.stroke();
                            ctx.beginPath(); ctx.moveTo(7, 6); ctx.lineTo(3, 12); ctx.stroke();
                            ctx.beginPath(); ctx.moveTo(7, 6); ctx.lineTo(11, 12); ctx.stroke();
                            ctx.fillStyle = Qt.rgba(iconColor.r, iconColor.g, iconColor.b, 0.85);
                            ctx.beginPath(); ctx.arc(7, 1, 1.5, 0, Math.PI * 2); ctx.fill();
                            ctx.beginPath(); ctx.arc(3, 12, 1.5, 0, Math.PI * 2); ctx.fill();
                            ctx.beginPath(); ctx.arc(11, 12, 1.5, 0, Math.PI * 2); ctx.fill();
                            ctx.restore();
                        }
                        onSChanged: requestPaint()
                        onIconColorChanged: requestPaint()
                    }
                    Text { text: "Fan-Out (Multi-Agent)"; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium; color: Tokens.accentBase; Layout.fillWidth: true }
                }

                MouseArea {
                    id: fanoutMa; anchors.fill: parent; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: { showAgentPicker = false; showFanOutModal = true; }
                }
            }
        }
    }

    // ── Conversations Dropdown ──
    Rectangle {
        id: conversationDropdown
        anchors.top: chatHeader.bottom; anchors.topMargin: Math.round(4 * root.sf)
        anchors.right: parent.right; anchors.rightMargin: Math.round(12 * root.sf)
        width: Math.round(280 * root.sf); height: Math.min(convCol.height + Math.round(12 * root.sf), Math.round(350 * root.sf))
        radius: root.radiusMd; z: 100
        color: Qt.rgba(Tokens.bgElevated.r, Tokens.bgElevated.g, Tokens.bgElevated.b, 0.97)
        border.color: Tokens.borderSubtle; border.width: 1
        visible: chatExpanded && showConversationList
        clip: true

        Flickable {
            anchors.fill: parent; anchors.margins: Math.round(6 * root.sf)
            contentHeight: convCol.height; clip: true

            Column {
                id: convCol
                width: parent.width; spacing: Math.round(2 * root.sf)

                // Header
                Text {
                    text: "Conversations"
                    font.pixelSize: Math.round(11 * root.sf); font.weight: Font.DemiBold
                    color: root.textMuted
                    leftPadding: Math.round(6 * root.sf); topPadding: Math.round(4 * root.sf)
                    bottomPadding: Math.round(6 * root.sf)
                }

                Repeater {
                    model: conversations

                    delegate: Rectangle {
                        width: convCol.width; height: convItemCol.height + Math.round(10 * root.sf)
                        radius: Math.round(6 * root.sf)
                        color: modelData.id === currentConversationId ? Tokens.accentSubtle : convItemMa.containsMouse ? Qt.rgba(Tokens.borderSubtle.r, Tokens.borderSubtle.g, Tokens.borderSubtle.b, 0.06) : "transparent"

                        Column {
                            id: convItemCol
                            anchors.left: parent.left; anchors.right: convDeleteBtn.left
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.leftMargin: Math.round(8 * root.sf)
                            anchors.rightMargin: Math.round(4 * root.sf)
                            spacing: Math.round(2 * root.sf)

                            Text {
                                text: modelData.title || "New Chat"
                                font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium
                                color: modelData.id === currentConversationId ? Tokens.accentBase : Tokens.textPrimary
                                elide: Text.ElideRight; width: parent.width
                            }
                            Text {
                                text: (modelData.messageCount || 0) + " msgs"
                                font.pixelSize: Math.round(9 * root.sf)
                                color: root.textMuted
                            }
                        }

                        // Delete button
                        Rectangle {
                            id: convDeleteBtn
                            anchors.right: parent.right; anchors.rightMargin: Math.round(4 * root.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            width: Math.round(20 * root.sf); height: Math.round(20 * root.sf); radius: Math.round(4 * root.sf)
                            color: convDelMa.containsMouse ? Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.15) : "transparent"
                            visible: conversations.length > 1

                            Text { anchors.centerIn: parent; text: "x"; font.pixelSize: Math.round(10 * root.sf); color: convDelMa.containsMouse ? root.accentRed : root.textMuted }
                            MouseArea { id: convDelMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: deleteConv(modelData.id) }
                        }

                        MouseArea {
                            id: convItemMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; z: -1
                            onClicked: switchToConversation(modelData.id)
                        }
                    }
                }
            }
        }
    }

    // ── Multi-Agent Activity Panel ──
    Rectangle {
        id: multiAgentPanel
        anchors.top: chatHeader.bottom; anchors.topMargin: 1
        anchors.left: parent.left; anchors.right: parent.right
        anchors.leftMargin: Math.round(8 * root.sf); anchors.rightMargin: Math.round(8 * root.sf)
        height: maPanel.height + Math.round(12 * root.sf)
        radius: Math.round(8 * root.sf)
        color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.9)
        border.color: Tokens.borderSubtle; border.width: 1
        visible: chatExpanded && Object.keys(liveAgentRuns).length > 0

        Column {
            id: maPanel; anchors.left: parent.left; anchors.right: parent.right
            anchors.top: parent.top; anchors.margins: Math.round(6 * root.sf); spacing: Math.round(4 * root.sf)

            Row {
                spacing: Math.round(6 * root.sf)
                Text { text: "▸"; font.pixelSize: Math.round(11 * root.sf); color: root.accentBlue }
                Text { text: "Agent Activity"; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Bold; color: Tokens.textPrimary }
                Rectangle {
                    width: maBadge.width + Math.round(8 * root.sf); height: Math.round(16 * root.sf); radius: 8
                    color: Tokens.accentSubtle
                    Text { id: maBadge; anchors.centerIn: parent; text: Object.keys(liveAgentRuns).length.toString(); font.family: Tokens.fontBody; font.pixelSize: Math.round(9 * root.sf); font.weight: Font.Bold; color: Tokens.accentBase }
                }
            }

            Repeater {
                model: Object.keys(liveAgentRuns)

                Rectangle {
                    property var run: liveAgentRuns[modelData]
                    width: maPanel.width; height: maRunRow.height + Math.round(10 * root.sf); radius: Math.round(6 * root.sf)
                    color: Qt.rgba(Tokens.bgSunken.r, Tokens.bgSunken.g, Tokens.bgSunken.b, 0.8)
                    border.color: run.status === "running" ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.22) : Tokens.borderSubtle; border.width: 1

                    Row {
                        id: maRunRow; anchors.left: parent.left; anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.margins: Math.round(8 * root.sf); spacing: Math.round(6 * root.sf)

                        Rectangle {
                            width: Math.round(6 * root.sf); height: Math.round(6 * root.sf); radius: width / 2
                            anchors.verticalCenter: parent.verticalCenter
                            color: run.status === "running" ? Tokens.accentBase : run.status === "completed" ? Tokens.successBase : run.status === "error" ? Tokens.dangerBase : Tokens.textMuted
                        }
                        Column {
                            spacing: 1; width: parent.width - Math.round(80 * root.sf)
                            Text { text: run.agentId || "Agent"; font.family: Tokens.fontBody; font.pixelSize: Math.round(10 * root.sf); font.weight: Font.Bold; color: Tokens.textPrimary }
                            Text { text: (run.task || "...").substring(0, 60); font.family: Tokens.fontBody; font.pixelSize: Math.round(9 * root.sf); color: Tokens.textMuted; elide: Text.ElideRight; width: parent.width }
                        }
                        Text {
                            text: run.status || "pending"
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(9 * root.sf); font.weight: Font.Medium; anchors.verticalCenter: parent.verticalCenter
                            color: run.status === "running" ? Tokens.accentBase : run.status === "completed" ? Tokens.successBase : run.status === "error" ? Tokens.dangerBase : Tokens.textMuted
                        }
                    }
                }
            }
        }
    }

    // ── Messages Area ──
    Item {
        anchors.top: multiAgentPanel.visible ? multiAgentPanel.bottom : chatHeader.bottom; anchors.left: parent.left
        anchors.right: parent.right; anchors.bottom: inputArea.top
        anchors.margins: 1; visible: chatExpanded; clip: true

        // Empty state
        Column {
            anchors.centerIn: parent; spacing: Math.round(16 * root.sf)
            visible: messages.length === 0 && !isSending

            // Hermes orb mark — amber glow rings, no whale_logo dependency
            Item {
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.round(72 * root.sf); height: Math.round(72 * root.sf)

                Rectangle {
                    anchors.centerIn: parent
                    width: Math.round(72 * root.sf); height: width; radius: width / 2
                    color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.07)
                }
                Rectangle {
                    anchors.centerIn: parent
                    width: Math.round(54 * root.sf); height: width; radius: width / 2
                    color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.05)
                    border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.18)
                    border.width: 1
                }
                Text {
                    anchors.centerIn: parent
                    text: "◉"
                    font.pixelSize: Math.round(28 * root.sf)
                    color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.55)
                }
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "How can I help you today?"
                font.family: Tokens.fontDisplay
                font.pixelSize: Math.round(16 * root.sf); font.weight: Font.DemiBold
                color: Tokens.textSecondary
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Ask me anything — code, files, system tasks"
                font.family: Tokens.fontBody
                font.pixelSize: Math.round(11 * root.sf)
                color: Tokens.textMuted
            }

            // Quick suggestion chips
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Math.round(8 * root.sf)
                Repeater {
                    model: ["Write code", "Open terminal", "System info"]
                    Rectangle {
                        width: chipText.width + Math.round(16 * root.sf); height: Math.round(26 * root.sf)
                        radius: Math.round(13 * root.sf)
                        color: chipMa.containsMouse ? Tokens.accentSubtle : Qt.rgba(1, 1, 1, 0.04)
                        border.color: chipMa.containsMouse ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.35) : Tokens.borderSubtle
                        border.width: 1
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad } }
                        Text {
                            id: chipText; anchors.centerIn: parent
                            text: modelData
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(10 * root.sf)
                            color: chipMa.containsMouse ? Tokens.accentBase : Tokens.textMuted
                            Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        }
                        MouseArea {
                            id: chipMa; anchors.fill: parent; hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: { chatInput.text = modelData; chatInput.forceActiveFocus(); }
                        }
                    }
                }
            }
        }

        ListView {
            id: messageList
            anchors.fill: parent; anchors.leftMargin: Math.round(20 * root.sf); anchors.rightMargin: Math.round(20 * root.sf)
            anchors.topMargin: Math.round(16 * root.sf); anchors.bottomMargin: Math.round(12 * root.sf)
            model: messages; spacing: Math.round(20 * root.sf); cacheBuffer: 400

            delegate: Item {
                width: messageList.width
                height: delegateCol.childrenRect.height

                Column {
                    id: delegateCol
                    width: parent.width
                    spacing: Math.round(4 * root.sf)

                    // Tool calls display (above the message if present)
                    Column {
                        id: toolCol
                        width: parent.width - (modelData.role !== "user" ? Math.round(36 * root.sf) : 0) - parent.width * 0.05
                        x: modelData.role !== "user" ? Math.round(36 * root.sf) : 0
                        visible: modelData.toolCalls && modelData.toolCalls.length > 0
                        spacing: Math.round(6 * root.sf)

                        Repeater {
                            model: (modelData.toolCalls || [])
                            Rectangle {
                                width: toolCol.width; height: toolRow.height + Math.round(8 * root.sf)
                                radius: Math.round(6 * root.sf)
                                color: Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.06)
                                border.color: Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.15); border.width: 1

                                Row {
                                    id: toolRow; anchors.left: parent.left; anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.margins: Math.round(8 * root.sf); spacing: Math.round(6 * root.sf)

                                    Text { text: "⚡"; font.pixelSize: Math.round(9 * root.sf); color: Tokens.warnBase }
                                    Text {
                                        text: modelData.name || "tool"
                                        font.family: Tokens.fontMono
                                        font.pixelSize: Math.round(10 * root.sf); font.weight: Font.Medium; color: Tokens.successBase
                                    }
                                    Text {
                                        text: modelData.status === "success" ? "✓" : (modelData.status === "error" ? "✗" : "⋯")
                                        font.pixelSize: Math.round(10 * root.sf)
                                        color: modelData.status === "success" ? Tokens.successBase : (modelData.status === "error" ? Tokens.dangerBase : Tokens.textMuted)
                                    }
                                }
                            }
                        }
                    }

                    // Message row wrapper
                    Item {
                        width: parent.width
                        height: msgRow.height

                        Row {
                            id: msgRow
                            x: modelData.role === "user" ? (parent.width - msgRow.implicitWidth) : 0
                            spacing: Math.round(12 * root.sf)
                            layoutDirection: modelData.role === "user" ? Qt.RightToLeft : Qt.LeftToRight

                            // Avatar for assistant — Hermes orb mark
                            Rectangle {
                                visible: modelData.role !== "user"
                                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf)
                                radius: Math.round(8 * root.sf)
                                color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.10)

                                Text {
                                    anchors.centerIn: parent
                                    text: "◉"
                                    font.pixelSize: Math.round(14 * root.sf)
                                    color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.7)
                                }
                            }

                            Rectangle {
                                id: msgBubble
                                width: modelData.role === "user"
                                    ? Math.min(messageList.width * 0.68, userMsgMetrics.width + Math.round(36 * root.sf))
                                    : messageList.width - Math.round(48 * root.sf)
                                height: msgContentCol.height + Math.round(24 * root.sf)
                                radius: Math.round(16 * root.sf)

                                color: modelData.role === "user"
                                    ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.12)
                                    : Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.45)
                                border.color: modelData.role === "user"
                                    ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.18)
                                    : Tokens.borderSubtle
                                border.width: 1

                                TextMetrics {
                                    id: userMsgMetrics
                                    text: modelData.content || ""
                                    font.pixelSize: Math.round(13 * root.sf)
                                }

                                Column {
                                    id: msgContentCol
                                    anchors.left: parent.left; anchors.right: parent.right
                                    anchors.top: parent.top
                                    anchors.margins: modelData.role === "user" ? Math.round(14 * root.sf) : Math.round(12 * root.sf)
                                    spacing: Math.round(6 * root.sf)

                                    Row {
                                        spacing: Math.round(6 * root.sf)
                                        Text {
                                            text: modelData.role === "user" ? "" : (modelData.agent || getAgentName())
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * root.sf); font.weight: Font.DemiBold
                                            color: Tokens.successBase
                                            visible: modelData.role !== "user"
                                        }
                                        Text {
                                            visible: modelData.model && modelData.role !== "user"
                                            text: modelData.model || ""
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(9 * root.sf); color: Tokens.textMuted
                                        }
                                    }

                                    Text {
                                        width: parent.width
                                        text: modelData.role === "user" ? (modelData.content || "") : mdToStyled(modelData.content || "")
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(13.5 * root.sf)
                                        color: modelData.role === "user" ? Tokens.textPrimary : Tokens.textSecondary
                                        wrapMode: Text.Wrap; lineHeight: 1.6
                                        textFormat: modelData.role === "user" ? Text.PlainText : Text.StyledText
                                        // Hide when content was only MEDIA refs (stripped text is empty)
                                        visible: text.length > 0
                                    }

                                    // ── Artifact renderer: images inline, documents as cards ──
                                    Repeater {
                                        model: modelData.role === "assistant"
                                               ? extractArtifacts(modelData.content || "")
                                               : []

                                        delegate: Loader {
                                            width: msgContentCol.width
                                            // Height determined by the loaded item
                                            height: item ? item.height : 0

                                            sourceComponent: modelData.kind === "image"
                                                             ? inlineImageComponent
                                                             : documentCardComponent

                                            // Pass artifact data into the loaded component
                                            property string artifactPath: modelData.path
                                            property string artifactKind: modelData.kind
                                            property string artifactExt: _extOf(modelData.path)
                                            property string artifactName: _basename(modelData.path)
                                        }
                                    }

                                    // ── Inline image component ──
                                    Component {
                                        id: inlineImageComponent

                                        Item {
                                            width: parent ? parent.width : 0
                                            // Cap image height at 360 logical px; show 48px placeholder while loading
                                            height: mediaImg.status === Image.Ready
                                                    ? Math.min(
                                                        mediaImg.implicitHeight * (width / Math.max(mediaImg.implicitWidth, 1)),
                                                        Math.round(360 * root.sf)
                                                      )
                                                    : Math.round(48 * root.sf)

                                            // Rounded clip mask
                                            Rectangle {
                                                anchors.fill: parent
                                                radius: Math.round(10 * root.sf)
                                                color: "transparent"
                                                clip: true

                                                Image {
                                                    id: mediaImg
                                                    anchors.fill: parent
                                                    source: "file://" + artifactPath
                                                    fillMode: Image.PreserveAspectFit
                                                    smooth: true; mipmap: true
                                                    asynchronous: true

                                                    // Placeholder while loading or on error
                                                    Rectangle {
                                                        anchors.fill: parent
                                                        color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.7)
                                                        radius: Math.round(10 * root.sf)
                                                        visible: mediaImg.status !== Image.Ready

                                                        Column {
                                                            anchors.centerIn: parent
                                                            spacing: Math.round(4 * root.sf)

                                                            Text {
                                                                anchors.horizontalCenter: parent.horizontalCenter
                                                                text: mediaImg.status === Image.Error ? "⚠" : "⋯"
                                                                font.pixelSize: Math.round(18 * root.sf)
                                                                color: mediaImg.status === Image.Error ? Tokens.dangerBase : Tokens.accentBase
                                                            }
                                                            Text {
                                                                anchors.horizontalCenter: parent.horizontalCenter
                                                                text: mediaImg.status === Image.Error
                                                                      ? "imagen no disponible"
                                                                      : "cargando…"
                                                                font.family: Tokens.fontBody
                                                                font.pixelSize: Math.round(10 * root.sf)
                                                                color: Tokens.textMuted
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }

                                    // ── Document card component ──
                                    Component {
                                        id: documentCardComponent

                                        Rectangle {
                                            width: parent ? parent.width : 0
                                            height: docCardRow.height + Math.round(20 * root.sf)
                                            radius: Math.round(10 * root.sf)
                                            color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.85)
                                            border.color: Tokens.borderSubtle; border.width: 1

                                            Row {
                                                id: docCardRow
                                                anchors.left: parent.left; anchors.right: parent.right
                                                anchors.top: parent.top
                                                anchors.margins: Math.round(10 * root.sf)
                                                spacing: Math.round(10 * root.sf)

                                                // File type badge
                                                Rectangle {
                                                    width: Math.round(42 * root.sf); height: Math.round(42 * root.sf)
                                                    radius: Math.round(8 * root.sf)
                                                    color: Qt.rgba(Tokens.bgSunken.r, Tokens.bgSunken.g, Tokens.bgSunken.b, 0.85)
                                                    border.color: _docTypeColor(artifactExt); border.width: 1
                                                    anchors.verticalCenter: parent.verticalCenter

                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: _docTypeLabel(artifactExt)
                                                        font.pixelSize: Math.round(8 * root.sf)
                                                        font.weight: Font.Bold
                                                        color: _docTypeColor(artifactExt)
                                                    }
                                                }

                                                // File info
                                                Column {
                                                    spacing: Math.round(2 * root.sf)
                                                    anchors.verticalCenter: parent.verticalCenter
                                                    width: parent.width - Math.round(42 * root.sf)
                                                          - Math.round(10 * root.sf)
                                                          - docCardBtns.width
                                                          - Math.round(10 * root.sf)

                                                    Text {
                                                        text: artifactName
                                                        font.family: Tokens.fontBody
                                                        font.pixelSize: Math.round(12 * root.sf)
                                                        font.weight: Font.Medium
                                                        color: Tokens.textPrimary
                                                        elide: Text.ElideMiddle
                                                        width: parent.width
                                                    }
                                                    Text {
                                                        text: _docTypeLabel(artifactExt) + " · " + artifactExt.toUpperCase()
                                                        font.pixelSize: Math.round(10 * root.sf)
                                                        color: root.textMuted
                                                    }
                                                }

                                                // Action buttons
                                                Row {
                                                    id: docCardBtns
                                                    spacing: Math.round(6 * root.sf)
                                                    anchors.verticalCenter: parent.verticalCenter

                                                    // Abrir — opens in the SO's registered app via xdg-open
                                                    Rectangle {
                                                        width: abrirText.width + Math.round(16 * root.sf)
                                                        height: Math.round(28 * root.sf)
                                                        radius: Math.round(6 * root.sf)
                                                        color: abrirMa.containsMouse
                                                               ? Tokens.accentSubtle
                                                               : Tokens.accentGhost
                                                        border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40); border.width: 1

                                                        Text {
                                                            id: abrirText
                                                            anchors.centerIn: parent
                                                            text: "Abrir"
                                                            font.family: Tokens.fontBody
                                                            font.pixelSize: Math.round(11 * root.sf)
                                                            font.weight: Font.Medium
                                                            color: Tokens.accentBase
                                                        }

                                                        MouseArea {
                                                            id: abrirMa; anchors.fill: parent
                                                            hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                                            onClicked: sysManager.openFile(artifactPath)
                                                        }
                                                    }

                                                    // Descargar — copies the gateway download URL to clipboard.
                                                    // The QML compositor runs inside the Wayland session, not in a browser
                                                    // context, so it cannot trigger a browser download directly.
                                                    // The user can paste the URL in the noVNC browser tab to download.
                                                    Rectangle {
                                                        width: descText.width + Math.round(16 * root.sf)
                                                        height: Math.round(28 * root.sf)
                                                        radius: Math.round(6 * root.sf)
                                                        color: descMa.containsMouse
                                                               ? Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.18)
                                                               : Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.08)
                                                        border.color: Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.35); border.width: 1

                                                        Text {
                                                            id: descText
                                                            anchors.centerIn: parent
                                                            text: "Descargar"
                                                            font.family: Tokens.fontBody
                                                            font.pixelSize: Math.round(11 * root.sf)
                                                            font.weight: Font.Medium
                                                            color: Tokens.successBase
                                                        }

                                                        MouseArea {
                                                            id: descMa; anchors.fill: parent
                                                            hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                                            onClicked: {
                                                                var url = _downloadUrl(artifactPath);
                                                                sysManager.copyToClipboard(url);
                                                                root.showToast("URL copiada: " + url, "success");
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                } // end delegateCol
            }

            // ── Live streaming footer ──
            footer: Item {
                width: messageList.width
                height: streamCol.height + Math.round(10 * root.sf)
                visible: isSending

                Column {
                    id: streamCol
                    anchors.left: parent.left; anchors.right: parent.right
                    spacing: Math.round(6 * root.sf)

                    // Plan display
                    Rectangle {
                        visible: activePlan !== null
                        width: parent.width * 0.88; height: planCol.height + Math.round(16 * root.sf)
                        radius: Math.round(10 * root.sf)
                        color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.7)
                        border.color: Tokens.borderDefault; border.width: 1

                        Column {
                            id: planCol; anchors.left: parent.left; anchors.right: parent.right
                            anchors.top: parent.top; anchors.margins: Math.round(10 * root.sf); spacing: Math.round(6 * root.sf)

                            Text {
                                text: "≣ " + (activePlan ? activePlan.title || "Plan" : "")
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Bold; color: Tokens.accentBase
                            }

                            Repeater {
                                model: activePlan ? activePlan.steps || [] : []
                                Row {
                                    spacing: Math.round(6 * root.sf)
                                    Text {
                                        text: modelData.status === "completed" ? "✓" : (modelData.status === "in_progress" ? "⋯" : (modelData.status === "skipped" ? "—" : "○"))
                                        font.pixelSize: Math.round(10 * root.sf)
                                        color: modelData.status === "completed" ? Tokens.successBase : (modelData.status === "in_progress" ? Tokens.accentBase : Tokens.textMuted)
                                    }
                                    Text {
                                        text: modelData.description || modelData.title || ""
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(10 * root.sf)
                                        color: modelData.status === "completed" ? Tokens.successBase : (modelData.status === "in_progress" ? Tokens.textPrimary : Tokens.textMuted)
                                    }
                                }
                            }
                        }
                    }

                    // Live tool steps
                    Repeater {
                        model: streamingSteps

                        Rectangle {
                            width: parent.width * 0.88; height: stepRow.height + Math.round(12 * root.sf)
                            radius: Math.round(8 * root.sf)
                            color: modelData.type === "error"
                                       ? Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.08)
                                       : modelData.type === "tool"
                                           ? Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.06)
                                           : Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.6)
                            border.color: modelData.type === "error"
                                              ? Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.22)
                                              : modelData.type === "tool"
                                                  ? Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.20)
                                                  : Tokens.borderSubtle
                            border.width: 1

                            Row {
                                id: stepRow; anchors.left: parent.left; anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.margins: Math.round(8 * root.sf); spacing: Math.round(6 * root.sf)

                                Text {
                                    text: modelData.type === "thinking" ? "⊙" :
                                          modelData.type === "tool" ? "⚡" :
                                          modelData.type === "error" ? "✗" : "•"
                                    font.pixelSize: Math.round(11 * root.sf)
                                    color: modelData.type === "thinking" ? Tokens.accentBase :
                                           modelData.type === "tool" ? Tokens.warnBase :
                                           modelData.type === "error" ? Tokens.dangerBase : Tokens.textMuted
                                }

                                Text {
                                    text: {
                                        if (modelData.type === "thinking") {
                                            var iter = modelData.iteration || 1;
                                            return modelData.done ? "Thought complete" : "Thinking" + (iter > 1 ? " (round " + iter + ")" : "") + "...";
                                        } else if (modelData.type === "tool") {
                                            return (modelData.name || "tool") + (modelData.status === "running" ? " ⋯" : (modelData.status === "success" ? " ✓" : " ✗"));
                                        } else if (modelData.type === "error") {
                                            return modelData.message || "Error";
                                        }
                                        return "";
                                    }
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(10 * root.sf); font.weight: Font.Medium
                                    color: modelData.type === "error" ? Tokens.dangerBase :
                                           modelData.type === "tool" ? Tokens.successBase : Tokens.infoBase
                                    width: parent.width - Math.round(40 * root.sf)
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }

                    // Streaming content preview
                    Row {
                        visible: streamingContent.length > 0
                        spacing: Math.round(8 * root.sf)

                        Rectangle {
                            width: Math.round(28 * root.sf); height: Math.round(28 * root.sf)
                            radius: Math.round(8 * root.sf)
                            color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.10)
                            Text {
                                anchors.centerIn: parent
                                text: "◉"
                                font.pixelSize: Math.round(14 * root.sf)
                                color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.7)
                            }
                        }

                        Rectangle {
                            width: streamCol.width - Math.round(44 * root.sf); height: streamTextCol.height + Math.round(14 * root.sf)
                            radius: Math.round(12 * root.sf)
                            color: "transparent"

                            Column {
                                id: streamTextCol; anchors.left: parent.left; anchors.right: parent.right
                                anchors.top: parent.top; anchors.margins: Math.round(4 * root.sf); spacing: Math.round(4 * root.sf)

                                Text {
                                    text: getAgentName()
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf); font.weight: Font.DemiBold
                                    color: Tokens.successBase
                                }
                                Text {
                                    width: parent.width
                                    text: mdToStyled(streamingContent)
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(13 * root.sf); color: Tokens.textPrimary
                                    wrapMode: Text.Wrap; lineHeight: 1.5
                                    textFormat: Text.StyledText
                                }
                            }

                        // Typing cursor
                        Rectangle {
                            anchors.bottom: parent.bottom; anchors.bottomMargin: Math.round(12 * root.sf)
                            anchors.right: streamTextCol.right; anchors.rightMargin: Math.round(10 * root.sf)
                            width: Math.round(2 * root.sf); height: Math.round(14 * root.sf)
                            color: Tokens.accentBase
                            SequentialAnimation on opacity {
                                running: isSending; loops: Animation.Infinite
                                NumberAnimation { to: 0; duration: 500 }
                                NumberAnimation { to: 1; duration: 500 }
                            }
                        }
                    }
                    } // close streaming Row

                    // Working status bar — styled as message bubble
                    Row {
                        visible: isSending && streamingContent.length === 0
                        spacing: Math.round(12 * root.sf)

                        // Hermes orb avatar
                        Rectangle {
                            width: Math.round(28 * root.sf); height: Math.round(28 * root.sf)
                            radius: Math.round(8 * root.sf)
                            color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.10)
                            Text {
                                anchors.centerIn: parent
                                text: "◉"
                                font.pixelSize: Math.round(14 * root.sf)
                                color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.7)
                            }
                        }

                        // Message-style bubble with thinking
                        Rectangle {
                            width: streamCol.width - Math.round(48 * root.sf)
                            height: thinkingBubbleCol.height + Math.round(20 * root.sf)
                            radius: Math.round(14 * root.sf)
                            color: Qt.rgba(Tokens.bgCard.r, Tokens.bgCard.g, Tokens.bgCard.b, 0.6)
                            border.color: Tokens.borderSubtle; border.width: 1

                            Column {
                                id: thinkingBubbleCol; anchors.left: parent.left; anchors.right: parent.right
                                anchors.top: parent.top; anchors.margins: Math.round(10 * root.sf); spacing: Math.round(8 * root.sf)

                                Row {
                                    spacing: Math.round(6 * root.sf)
                                    Text {
                                        text: getAgentName()
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(11 * root.sf); font.weight: Font.DemiBold
                                        color: Tokens.successBase
                                    }
                                    Text {
                                        visible: activeModel.length > 0
                                        text: activeModel
                                        font.family: Tokens.fontBody
                                        font.pixelSize: Math.round(9 * root.sf); color: Tokens.textMuted
                                    }
                                }

                                Row {
                                    id: streamRow2; spacing: Math.round(5 * root.sf)
                                    Repeater {
                                        model: 3
                                        Rectangle {
                                            width: Math.round(5 * root.sf); height: Math.round(5 * root.sf); radius: width / 2; color: Tokens.accentBase
                                            anchors.verticalCenter: parent.verticalCenter
                                            SequentialAnimation on opacity {
                                                running: isSending; loops: Animation.Infinite
                                                PauseAnimation { duration: index * 200 }
                                                NumberAnimation { to: 0.2; duration: 400 }
                                                NumberAnimation { to: 1.0; duration: 400 }
                                                PauseAnimation { duration: (2 - index) * 200 }
                                            }
                                        }
                                    }
                                }

                                Text {
                                    text: getWorkingStatus()
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * root.sf); color: Tokens.textMuted
                                    topPadding: Math.round(2 * root.sf)
                                }
                            }
                        }
                    }
                }
            }

            onCountChanged: Qt.callLater(function() { messageList.positionViewAtEnd(); })
        }
    }

    // ── Slash Command Suggestions ──
    Rectangle {
        id: suggestionsBox
        anchors.bottom: inputArea.top; anchors.bottomMargin: Math.round(4 * root.sf)
        anchors.left: parent.left; anchors.leftMargin: Math.round(8 * root.sf)
        width: sugCol.width + Math.round(16 * root.sf); height: sugCol.height + Math.round(12 * root.sf)
        radius: Math.round(Tokens.radiusMd * root.sf); z: 50
        color: Qt.rgba(Tokens.bgElevated.r, Tokens.bgElevated.g, Tokens.bgElevated.b, 0.97)
        border.color: Tokens.borderDefault; border.width: 1
        visible: showSuggestions && chatExpanded

        Column {
            id: sugCol; anchors.centerIn: parent; spacing: 2; padding: 0

            Repeater {
                model: {
                    if (!showSuggestions) return [];
                    var input = chatInput.text.toLowerCase();
                    var filtered = [];
                    for (var i = 0; i < commandSuggestions.length; i++) {
                        if (commandSuggestions[i].indexOf(input) === 0) filtered.push(commandSuggestions[i]);
                    }
                    return filtered;
                }

                Rectangle {
                    width: Math.round(140 * root.sf); height: Math.round(26 * root.sf); radius: Math.round(4 * root.sf)
                    color: sugItemMa.containsMouse ? Tokens.accentSubtle : "transparent"

                    Text {
                        anchors.verticalCenter: parent.verticalCenter; leftPadding: Math.round(8 * root.sf)
                        text: modelData; font.pixelSize: Math.round(12 * root.sf); font.family: Tokens.fontMono; color: Tokens.accentBase
                    }

                    MouseArea {
                        id: sugItemMa; anchors.fill: parent; hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { chatInput.text = modelData + " "; showSuggestions = false; chatInput.forceActiveFocus(); }
                    }
                }
            }
        }
    }

    // ── Input Area ──
    Rectangle {
        id: inputArea
        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        anchors.leftMargin: Math.round(12 * root.sf); anchors.rightMargin: Math.round(12 * root.sf)
        anchors.bottomMargin: Math.round(10 * root.sf)
        height: Math.round(50 * root.sf); radius: Math.round(16 * root.sf)
        color: Qt.rgba(Tokens.bgSunken.r, Tokens.bgSunken.g, Tokens.bgSunken.b, 0.96)
        border.color: chatInput.activeFocus ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40) : Tokens.borderSubtle
        border.width: 1

        // Inner shadow effect at top
        Rectangle {
            anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right
            anchors.leftMargin: Math.round(16 * root.sf); anchors.rightMargin: Math.round(16 * root.sf)
            anchors.topMargin: 1; height: 1; radius: 1
            color: Qt.rgba(1, 1, 1, 0.04)
        }

        // PERF: Removed border.color Behavior — triggers repaint on every focus change

        RowLayout {
            anchors.fill: parent; anchors.leftMargin: Math.round(16 * root.sf); anchors.rightMargin: Math.round(8 * root.sf); spacing: Math.round(10 * root.sf)

            // AI icon — clickable expand button when collapsed
            Rectangle {
                visible: !chatExpanded
                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf); radius: Math.round(8 * root.sf)
                color: expandIconMa.containsMouse ? Tokens.accentSubtle : Tokens.accentGhost
                Text { anchors.centerIn: parent; text: "◉"; font.pixelSize: Math.round(14 * root.sf); color: Tokens.accentBase }
                MouseArea {
                    id: expandIconMa; anchors.fill: parent; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: { chatExpanded = true; chatInput.forceActiveFocus(); }
                }
            }

            // Message count badge when collapsed (shows there are messages)
            Rectangle {
                visible: !chatExpanded && messages.length > 0
                width: collapsedBadge.width + Math.round(10 * root.sf); height: Math.round(20 * root.sf); radius: 10
                color: Tokens.accentSubtle

                Text {
                    id: collapsedBadge; anchors.centerIn: parent
                    text: messages.length + " msgs"
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(9 * root.sf); font.weight: Font.DemiBold; color: Tokens.accentBase
                }

                MouseArea {
                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: { chatExpanded = true; chatInput.forceActiveFocus(); }
                }
            }

            TextInput {
                id: chatInput
                Layout.fillWidth: true
                verticalAlignment: TextInput.AlignVCenter
                color: Tokens.textPrimary; font.family: Tokens.fontBody; font.pixelSize: Math.round(14 * root.sf)

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Ask Hermes anything..."
                    font.family: Tokens.fontBody
                    color: Tokens.textDisabled; font.pixelSize: Math.round(13 * root.sf)
                    visible: !parent.text && !parent.activeFocus
                }

                // Only expand on explicit click, not on focus change
                // (Wayland focus-follows-pointer causes hover to trigger focus)

                // Also expand on mouse click (in case focus doesn't change)
                MouseArea {
                    anchors.fill: parent; visible: !chatExpanded
                    cursorShape: Qt.IBeamCursor
                    onClicked: { chatExpanded = true; chatInput.forceActiveFocus(); }
                }

                onTextChanged: {
                    showSuggestions = text.length > 0 && text.charAt(0) === '/';
                }

                Keys.onReturnPressed: sendMessage()
                Keys.onUpPressed: {
                    if (chatHistory.length > 0) {
                        if (historyIndex === -1) historyIndex = chatHistory.length - 1;
                        else if (historyIndex > 0) historyIndex--;
                        chatInput.text = chatHistory[historyIndex];
                    }
                }
                Keys.onDownPressed: {
                    if (historyIndex >= 0) {
                        historyIndex++;
                        if (historyIndex >= chatHistory.length) { historyIndex = -1; chatInput.text = ""; }
                        else chatInput.text = chatHistory[historyIndex];
                    }
                }
                Keys.onEscapePressed: {
                    if (showSuggestions) showSuggestions = false;
                    else if (showAgentPicker) showAgentPicker = false;
                    else if (chatFullScreen) { chatFullScreen = false; }
                    else { chatExpanded = false; chatInput.focus = false; }
                }
            }

            Rectangle {
                visible: true
                width: agentPillText.width + Math.round(18 * root.sf); height: Math.round(26 * root.sf); radius: Math.round(13 * root.sf)
                color: Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.10)
                border.color: Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.22); border.width: 1

                Text {
                    id: agentPillText; anchors.centerIn: parent
                    text: getAgentName()
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(9 * root.sf); font.weight: Font.Bold
                    color: Tokens.successBase
                }
            }

            Rectangle {
                width: Math.round(36 * root.sf); height: Math.round(36 * root.sf); radius: Math.round(12 * root.sf)

                color: chatInput.text.trim() ? Tokens.accentBase : Tokens.accentGhost
                border.color: chatInput.text.trim() ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.45) : "transparent"
                border.width: 1
                // PERF: Removed color Behavior on send button

                // Arrow icon
                Canvas {
                    anchors.centerIn: parent
                    width: Math.round(16 * root.sf); height: Math.round(16 * root.sf)
                    property bool active: chatInput.text.trim().length > 0
                    property real s: root.sf
                    onActiveChanged: requestPaint()
                    onSChanged: requestPaint()
                    Component.onCompleted: requestPaint()
                    onPaint: {
                        var ctx = getContext("2d"); ctx.clearRect(0, 0, width, height);
                        ctx.save(); ctx.scale(s, s);
                        ctx.strokeStyle = active ? "#fff" : "#555";
                        ctx.lineWidth = 2; ctx.lineCap = "round"; ctx.lineJoin = "round";
                        // Up arrow
                        ctx.beginPath();
                        ctx.moveTo(8, 13); ctx.lineTo(8, 4);
                        ctx.moveTo(4, 7); ctx.lineTo(8, 3); ctx.lineTo(12, 7);
                        ctx.stroke();
                        ctx.restore();
                    }
                }

                // PERF: Removed SequentialAnimation on border.color — was repainting every 1200ms
                // even while idle. Static glow is visually equivalent and costs nothing.

                MouseArea {
                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                    onClicked: sendMessage()
                }
            }
        }
    }

    // ── Send Message via SSE Stream ──
    function sendMessage() {
        var msg = chatInput.text.trim();
        if (!msg || isSending) return;

        // Handle /new, /reset, /clear locally — reset work folder for fresh conversation
        var lowerMsg = msg.toLowerCase();
        if (lowerMsg === "/new" || lowerMsg === "/reset" || lowerMsg === "/clear") {
            startNewConversation();
        }

        chatExpanded = true;
        showSuggestions = false;
        showAgentPicker = false;

        var hist = chatHistory.slice();
        hist.push(msg);
        if (hist.length > 50) hist = hist.slice(-50);
        chatHistory = hist;
        historyIndex = -1;

        // Carpeta de trabajo ÚNICA = "Works" (la visible en el escritorio y en Files).
        // Es /var/lib/hermes/workspace: el daemon (User=hermes) escribe ahí los
        // entregables y el compositor (hermes-user, grupo hermes) los lee. NO usar
        // /home/<user>/Works: el daemon no puede escribir en el home del compositor,
        // y crearía una segunda "Works" fantasma. UNA sola carpeta para todo.
        if (!currentWorkFolder) {
            currentWorkFolder = generateWorkFolder(msg);
        }
        var workDir = "/var/lib/hermes/workspace";
        sysManager.createDir(workDir);

        var msgs = messages.slice();
        msgs.push({ role: "user", content: msg, createdAt: new Date().toISOString() });
        messages = msgs;
        chatInput.text = "";
        isSending = true;
        isStreaming = true;
        streamingContent = "";
        streamingSteps = [];
        __streamingReceived = false;  // I2 fix: sin esto, un turno cortado por Stop/cap dejaba el flag en true y el siguiente streaming no se mostraba
        activePlan = null;

        // ── Cableado REAL al daemon Hermes por D-Bus (no el /chat/stream de WhaleOS) ──
        // enqueue(trigger=chat_message, text, conversation_id) → el daemon procesa y
        // escribe la respuesta en la conversación; la sondeamos con get_conversation.
        // El daemon hace UUID(conversation_id) → DEBE ser un UUID v4 válido
        // (antes "conv-<ts>-<rand>" reventaba con "badly formed hexadecimal UUID
        // string" → la respuesta del LLM no se persistía ni se podía sondear).
        if (!chatConvId) chatConvId = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function(c) {
            var r = Math.random() * 16 | 0;
            return (c === "x" ? r : (r & 0x3 | 0x8)).toString(16);
        });
        // Ancla anti-duplicado: respuestas del asistente ya visibles en la UI.
        // El poller solo aceptará la respuesta nº (__assistantSeen + 1).
        var seen = 0;
        for (var mi = 0; mi < messages.length; mi++) {
            if (messages[mi].role === "assistant") seen++;
        }
        __assistantSeen = seen;
        hermes.call("enq-" + chatConvId, "enqueue", JSON.stringify({
            trigger_kind: "chat_message",
            text: msg,
            conversation_id: chatConvId,
            dedup_key: "chat:" + chatConvId + ":" + (new Date().getTime())
        }));
        __pollCount = 0;
        replyPollTimer.start();
    }

    function handleStreamEvent(event, data) {
        if (!data) return;
        switch (event) {
            case "thinking": {
                var steps = streamingSteps.slice();
                var found = false;
                for (var i = 0; i < steps.length; i++) {
                    if (steps[i].type === "thinking") { steps[i].iteration = data.iteration; found = true; break; }
                }
                if (!found) steps.push({ type: "thinking", iteration: data.iteration || 1, done: false });
                streamingSteps = steps;
                break;
            }
            case "content": {
                streamingContent = data.text || "";
                // Mark thinking as done
                var steps2 = streamingSteps.slice();
                for (var j = 0; j < steps2.length; j++) {
                    if (steps2[j].type === "thinking") steps2[j].done = true;
                }
                streamingSteps = steps2;
                break;
            }
            case "tool_start": {
                var steps3 = streamingSteps.slice();
                steps3.push({ type: "tool", id: data.id, name: data.name, arguments: data.arguments, status: "running" });
                streamingSteps = steps3;
                break;
            }
            case "tool_end": {
                var steps4 = streamingSteps.slice();
                for (var k = 0; k < steps4.length; k++) {
                    if (steps4[k].id === data.id) {
                        steps4[k].status = data.status || "success";
                        steps4[k].result = data.result;
                        break;
                    }
                }
                streamingSteps = steps4;
                break;
            }
            case "plan_created": {
                var plan = { title: data.title, steps: [], completed: false };
                if (data.steps) {
                    for (var p = 0; p < data.steps.length; p++) {
                        plan.steps.push({ id: data.steps[p].id, description: data.steps[p].description || data.steps[p].title,
                                          status: data.steps[p].status || "pending", toolCalls: [] });
                    }
                }
                activePlan = plan;
                break;
            }
            case "plan_step_update": {
                if (activePlan) {
                    var newPlan = JSON.parse(JSON.stringify(activePlan));
                    for (var q = 0; q < newPlan.steps.length; q++) {
                        if (newPlan.steps[q].id === data.stepId) {
                            newPlan.steps[q].status = data.status;
                            if (data.notes) newPlan.steps[q].notes = data.notes;
                            break;
                        }
                    }
                    activePlan = newPlan;
                }
                break;
            }
            case "plan_completed": {
                if (activePlan) {
                    var cp = JSON.parse(JSON.stringify(activePlan));
                    cp.completed = true;
                    for (var r = 0; r < cp.steps.length; r++) {
                        if (cp.steps[r].status !== "skipped") cp.steps[r].status = "completed";
                    }
                    activePlan = cp;
                }
                break;
            }
            case "done": {
                if (data.message) {
                    var msgs3 = messages.slice();
                    msgs3.push({
                        role: "assistant",
                        content: data.message.content || streamingContent,
                        toolCalls: data.message.toolCalls,
                        model: data.message.model,
                        agent: getAgentName(),
                        createdAt: data.message.createdAt || new Date().toISOString()
                    });
                    messages = msgs3;
                    streamingContent = "";
                }
                isSending = false;
                isStreaming = false;
                break;
            }
            case "error": {
                var steps5 = streamingSteps.slice();
                steps5.push({ type: "error", message: data.message || "Unknown error" });
                streamingSteps = steps5;
                break;
            }
            case "stopped": {
                isSending = false;
                isStreaming = false;
                break;
            }
            case "agent_start": {
                var runs = JSON.parse(JSON.stringify(liveAgentRuns));
                runs[data.runId] = { runId: data.runId, agentId: data.agentId, task: data.task, status: "running", steps: [] };
                liveAgentRuns = runs;
                break;
            }
            case "agent_update": {
                var runs2 = JSON.parse(JSON.stringify(liveAgentRuns));
                if (runs2[data.runId]) {
                    runs2[data.runId].status = data.status || runs2[data.runId].status;
                    if (data.step) { runs2[data.runId].steps = (runs2[data.runId].steps || []).concat([data.step]); }
                }
                liveAgentRuns = runs2;
                break;
            }
            case "agent_done": {
                var runs3 = JSON.parse(JSON.stringify(liveAgentRuns));
                if (runs3[data.runId]) {
                    runs3[data.runId].status = data.status || "completed";
                    if (data.result) runs3[data.runId].result = data.result;
                }
                liveAgentRuns = runs3;
                break;
            }
        }
        // Auto-scroll
        Qt.callLater(function() { messageList.positionViewAtEnd(); });
    }

    // ── Fan-Out Multi-Agent Modal ──
    Rectangle {
        id: fanOutOverlay
        anchors.fill: parent; z: 200
        visible: showFanOutModal
        color: Qt.rgba(0, 0, 0, 0.6)

        MouseArea { anchors.fill: parent; onClicked: showFanOutModal = false }

        Rectangle {
            anchors.centerIn: parent
            width: Math.min(Math.round(400 * root.sf), parent.width - Math.round(40 * root.sf))
            height: fanOutCol.height + Math.round(32 * root.sf)
            radius: root.radiusLg
            color: Qt.rgba(Tokens.bgElevated.r, Tokens.bgElevated.g, Tokens.bgElevated.b, 0.98)
            border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.20); border.width: 1

            MouseArea { anchors.fill: parent } // prevent close on card click

            Column {
                id: fanOutCol; anchors.left: parent.left; anchors.right: parent.right
                anchors.top: parent.top; anchors.margins: Math.round(16 * root.sf); spacing: Math.round(12 * root.sf)

                // Header
                RowLayout {
                    width: parent.width
                    Text { text: "Multi-Agent Task"; font.family: Tokens.fontDisplay; font.pixelSize: Math.round(16 * root.sf); font.weight: Font.Bold; color: Tokens.textPrimary; Layout.fillWidth: true }
                    Rectangle {
                        width: Math.round(24 * root.sf); height: Math.round(24 * root.sf); radius: 12
                        color: closeFoMa.containsMouse ? Qt.rgba(Tokens.borderSubtle.r, Tokens.borderSubtle.g, Tokens.borderSubtle.b, 0.10) : "transparent"
                        Text { anchors.centerIn: parent; text: "✕"; font.pixelSize: Math.round(12 * root.sf); color: Tokens.textMuted }
                        MouseArea { id: closeFoMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: showFanOutModal = false }
                    }
                }

                Text {
                    text: "Spawn agents to work on a task in parallel."
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * root.sf); color: Tokens.textMuted; width: parent.width; wrapMode: Text.Wrap
                }

                // Task input
                Text { text: "Task Description"; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium; color: Tokens.textSecondary }
                Rectangle {
                    width: parent.width; height: Math.round(60 * root.sf); radius: Math.round(Tokens.radiusMd * root.sf)
                    color: Qt.rgba(Tokens.bgSunken.r, Tokens.bgSunken.g, Tokens.bgSunken.b, 0.9)
                    border.color: fanOutTaskInput.activeFocus ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.35) : Tokens.borderSubtle
                    border.width: 1
                    TextInput {
                        id: fanOutTaskInput; anchors.fill: parent; anchors.margins: Math.round(10 * root.sf)
                        color: Tokens.textPrimary; font.family: Tokens.fontBody; font.pixelSize: Math.round(12 * root.sf); wrapMode: TextInput.Wrap
                        Text { visible: !parent.text; text: "Describe the task..."; color: Tokens.textDisabled; font.family: Tokens.fontBody; font.pixelSize: Math.round(12 * root.sf) }
                    }
                }

                // Agent checklist
                Text { text: "Select Agents"; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); font.weight: Font.Medium; color: Tokens.textSecondary }

                Column {
                    width: parent.width; spacing: Math.round(4 * root.sf)
                    Repeater {
                        model: agentList
                        Rectangle {
                            width: parent.width; height: Math.round(32 * root.sf); radius: Math.round(6 * root.sf)
                            color: foAgentMa.containsMouse ? Qt.rgba(Tokens.borderSubtle.r, Tokens.borderSubtle.g, Tokens.borderSubtle.b, 0.06) : "transparent"

                            RowLayout {
                                anchors.fill: parent; anchors.leftMargin: Math.round(8 * root.sf); anchors.rightMargin: Math.round(8 * root.sf); spacing: Math.round(8 * root.sf)
                                Rectangle {
                                    width: Math.round(16 * root.sf); height: Math.round(16 * root.sf); radius: Math.round(3 * root.sf)
                                    border.color: fanOutChecked[modelData.id] ? Tokens.accentBase : Tokens.borderDefault; border.width: 1
                                    color: fanOutChecked[modelData.id] ? Tokens.accentSubtle : "transparent"
                                    Text { anchors.centerIn: parent; text: fanOutChecked[modelData.id] ? "✓" : ""; font.pixelSize: Math.round(10 * root.sf); color: Tokens.accentBase }
                                }
                                Text { text: modelData.name || modelData.id; font.family: Tokens.fontBody; font.pixelSize: Math.round(11 * root.sf); color: Tokens.textPrimary; Layout.fillWidth: true }
                            }

                            MouseArea {
                                id: foAgentMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    var c = JSON.parse(JSON.stringify(fanOutChecked));
                                    c[modelData.id] = !c[modelData.id];
                                    fanOutChecked = c;
                                }
                            }
                        }
                    }
                }

                // Run button
                Rectangle {
                    width: parent.width; height: Math.round(36 * root.sf); radius: Math.round(10 * root.sf)
                    color: runFoMa.containsMouse ? Tokens.accentHover : Tokens.accentBase

                    Text { anchors.centerIn: parent; text: "⚡ Run Multi-Agent Task"; font.family: Tokens.fontBody; font.pixelSize: Math.round(12 * root.sf); font.weight: Font.Bold; color: Tokens.textOnAccent }

                    MouseArea {
                        id: runFoMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            var task = fanOutTaskInput.text.trim();
                            if (!task) { root.showToast("Enter a task description", "error"); return; }
                            var agents = [];
                            var keys = Object.keys(fanOutChecked);
                            for (var i = 0; i < keys.length; i++) {
                                if (fanOutChecked[keys[i]]) agents.push(keys[i]);
                            }
                            if (agents.length < 2) { root.showToast("Select at least 2 agents for fan-out", "error"); return; }

                            // Build directive message matching the dashboard's launchMultiAgentTask() pattern
                            var taskListStr = "";
                            for (var j = 0; j < agents.length; j++) {
                                taskListStr += "  - Agent \"" + agents[j] + "\" (label: \"" + agents[j] + "\")\n";
                            }
                            var directive = "[Fan-Out Task] Use sessions_fanout to split this task across " + agents.length + " agents and wait for all results:\n\nOverall task: " + task + "\n\nAgent assignments:\n" + taskListStr;

                            // Send as a regular chat message via /chat/stream
                            chatInput.text = directive;
                            showFanOutModal = false;
                            fanOutTaskInput.text = "";
                            fanOutChecked = ({});
                            sendMessage();
                        }
                    }
                }
            }
        }
    }

    // ============== CONVERSATION MANAGEMENT ==============
    // Re-cableado a verbos D-Bus reales: list_conversations / get_conversation /
    // delete_conversation (dbus_runtime_service.py). El daemon es dueño del store.
    // "Nueva conversación" no requiere verbo: el daemon crea la conversación
    // automáticamente en el primer enqueue con un nuevo conversation_id.
    // "Cambiar conversación" carga el historial con get_conversation.

    function loadConversations() {
        hermes.call("conv-list", "list_conversations", JSON.stringify({ agent_id: "" }));
    }

    function newChat() {
        // Genera un nuevo conversation_id local. El daemon lo creará en el primer
        // enqueue (create_or_touch). No hace falta un verbo "newConversation".
        function uuidv4() {
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
                var r = Math.random() * 16 | 0;
                var v = c === 'x' ? r : (r & 0x3 | 0x8);
                return v.toString(16);
            });
        }
        chatConvId = uuidv4();
        currentConversationId = chatConvId;
        __assistantSeen = 0;
        messages = [];
        streamingSteps = [];
        activePlan = null;
        streamingContent = "";
        showConversationList = false;
        loadConversations();
    }

    function switchToConversation(convId) {
        // Carga el historial del daemon con get_conversation, luego cambia el foco.
        hermes.call("conv-switch-" + convId, "get_conversation", JSON.stringify({ conversation_id: convId }));
    }

    function deleteConv(convId) {
        hermes.call("conv-del-" + convId, "delete_conversation", JSON.stringify({ conversation_id: convId }));
    }

    // Maneja las respuestas de los verbos de conversaciones
    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "conv-list") {
                if (!ok) return;
                try {
                    var arr = JSON.parse(jsonStr || "[]");
                    // El daemon devuelve conversation_id / message_count; el
                    // delegado del sidebar lee modelData.id / messageCount.
                    // Mapear o el clic/borrar/contador quedan rotos (undefined).
                    if (Array.isArray(arr)) conversations = arr.map(function(c) {
                        return { id: c.conversation_id, title: c.title || "Conversación",
                                 messageCount: c.message_count || 0 };
                    });
                } catch(e) {}
            } else if (reqId.indexOf("conv-switch-") === 0) {
                var convId = reqId.substring("conv-switch-".length);
                if (!ok) return;
                try {
                    var conv = JSON.parse(jsonStr || "{}");
                    var rawMsgs = conv.messages || [];
                    var out = [];
                    for (var i = 0; i < rawMsgs.length; i++) {
                        out.push({ role: rawMsgs[i].role || "user", content: rawMsgs[i].content || "" });
                    }
                    currentConversationId = convId;
                    chatConvId = convId;
                    __assistantSeen = out.filter(function(m) { return m.role === "assistant"; }).length;
                    messages = out;
                    streamingSteps = [];
                    activePlan = null;
                    streamingContent = "";
                    showConversationList = false;
                    loadConversations();
                } catch(e) {}
            } else if (reqId.indexOf("conv-del-") === 0) {
                var delId = reqId.substring("conv-del-".length);
                if (!ok) return;
                loadConversations();
                if (delId === currentConversationId) {
                    // Conversación activa borrada — limpiar estado local
                    messages = [];
                    __assistantSeen = 0;
                    chatConvId = "";
                    currentConversationId = "";
                }
            }
        }
    }



}
