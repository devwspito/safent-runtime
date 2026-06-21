import QtQuick
import QtQuick.Layouts
import "."
import QtQuick.Controls

// Lumen — Chat. Conversation thread with the agent.
// User messages: right-aligned accent bubbles.
// Lumen messages: left-aligned surface bubbles + mark avatar.
//
// Model-deferred design: the composer is always editable.
// Sending without a model shows an optimistic user bubble + ChatActionCard.
// When hasActiveModel becomes true, the last unserviced message is auto-retried.
Item {
    id: chatView
    property var shell: null

    // Hand-off bus: when shell is assigned, consume any message queued by Home.
    onShellChanged: {
        if (shell && shell.pendingMessage && shell.pendingMessage.length > 0) {
            var queued = shell.pendingMessage
            shell.pendingMessage = ""
            _sendMessage(queued)
        }
    }

    property string conversationId: ""
    property int streamingIndex: -1
    property bool isStreaming: false

    // Last message sent without an active model — retried automatically on connect.
    property string lastUnservicedMessage: ""
    // Index of the ChatActionCard for the unserviced message (to replace it on retry).
    property int unservicedCardIndex: -1

    // Sin mensaje semilla falso: el chat empieza vacío. La conversación es real.
    ListModel {
        id: messages
    }

    property bool showTyping: false

    // Banner dismissed state — persisted via backend settings so it doesn't
    // reappear after a reboot. If the backend doesn't expose this, we fall back
    // to a session-local flag (still much better than permanent alarm).
    property bool bannerDismissed: false

    // Tracks whether the user has scrolled up from the bottom of the thread.
    // Set true when scroll position leaves the bottom zone; false when they return.
    // Controls NewMessagesFab visibility during streaming.
    property bool userScrolledUp: false

    // Computed: show the invitation banner only when there's no model AND
    // the user hasn't dismissed it yet AND we're not already streaming.
    property bool showBanner: !backend.hasActiveModel && !chatView.bannerDismissed

    Connections {
        target: backend

        function onAgentChunk(convId, delta) {
            if (convId !== chatView.conversationId) return
            chatView.showTyping = false
            chatView.isStreaming = true
            if (chatView.streamingIndex < 0) {
                messages.append({
                    sender: "lumen", text: delta, hasCard: false,
                    cardType: "", timestamp: backend.clock,
                    isError: false, isActionCard: false
                })
                chatView.streamingIndex = messages.count - 1
            } else {
                var idx = chatView.streamingIndex
                messages.setProperty(idx, "text", messages.get(idx).text + delta)
            }
        }

        function onAgentToolEvent(convId, jsonEvent) {
            if (convId !== chatView.conversationId) return
            chatView._finalizeBubble()
            var ev = JSON.parse(jsonEvent)
            messages.append({
                sender: "lumen",
                text: ev.name || ev.tool_name || "herramienta",
                hasCard: true, cardType: "tool_event",
                timestamp: backend.clock, isError: false, isActionCard: false
            })
        }

        function onAgentDone(convId) {
            if (convId !== chatView.conversationId) return
            chatView.showTyping = false
            chatView.isStreaming = false
            chatView._finalizeBubble()
        }

        function onAgentError(convId, message) {
            if (convId !== chatView.conversationId) return
            chatView.showTyping = false
            chatView.isStreaming = false
            chatView._finalizeBubble()
            // Replace red error bubble with a ChatActionCard with retry
            messages.append({
                sender: "lumen", text: message, hasCard: true,
                cardType: "turn_error", timestamp: backend.clock,
                isError: false, isActionCard: true
            })
        }

        // When a service is connected, auto-retry the last unserviced message.
        function onActiveProviderChanged() {
            if (backend.hasActiveModel && chatView.lastUnservicedMessage.length > 0) {
                var msgToRetry = chatView.lastUnservicedMessage
                chatView.lastUnservicedMessage = ""
                // Remove the ChatActionCard that was placed for the unserviced message.
                if (chatView.unservicedCardIndex >= 0
                        && chatView.unservicedCardIndex < messages.count) {
                    messages.remove(chatView.unservicedCardIndex)
                    chatView.unservicedCardIndex = -1
                }
                chatView._sendMessage(msgToRetry)
            }
            // Fade out the banner — backend.hasActiveModel is now true,
            // so the showBanner computed property resolves to false automatically.
            // Setting bannerDismissed ensures it stays gone even if model later disconnects.
            chatView.bannerDismissed = true
        }
    }

    function _finalizeBubble() {
        chatView.streamingIndex = -1
    }

    function _newConvId() {
        function s4() { return Math.floor((1 + Math.random()) * 0x10000).toString(16).substring(1) }
        return s4()+s4()+"-"+s4()+"-"+s4()+"-"+s4()+"-"+s4()+s4()+s4()
    }

    // Always-available send — optimistic bubble even without a model.
    function _sendMessage(text) {
        if (text.trim().length === 0) return
        if (chatView.conversationId === "") chatView.conversationId = chatView._newConvId()
        messages.append({
            sender: "user", text: text.trim(), hasCard: false,
            cardType: "", timestamp: backend.clock,
            isError: false, isActionCard: false
        })

        if (!backend.hasActiveModel) {
            // Store for auto-retry; show ChatActionCard in thread
            chatView.lastUnservicedMessage = text.trim()
            chatView.showTyping = false
            messages.append({
                sender: "lumen", text: "",
                hasCard: true, cardType: "no_model",
                timestamp: backend.clock, isError: false, isActionCard: true
            })
            chatView.unservicedCardIndex = messages.count - 1
        } else {
            chatView.showTyping = true
            chatView.streamingIndex = -1
            backend.send(chatView.conversationId, text.trim())
        }
    }

    function _stopStreaming() {
        if (chatView.isStreaming) {
            backend.stopGeneration(chatView.conversationId)
            chatView.isStreaming = false
            chatView.showTyping = false
            chatView._finalizeBubble()
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── chat header ────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true; height: 52
            color: Theme.mode === "light" ? Theme.alpha(Theme.surface, 0.98) : Theme.alpha("#0E0C18", 0.95)
            border.color: Theme.line; border.width: 1

            // Bottom hairline
            Rectangle {
                anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
                height: 1; color: Theme.line
            }

            RowLayout {
                anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                spacing: Theme.sp2

                // Lumen avatar — mark on accent circle
                Rectangle {
                    width: 32; height: 32; radius: 16
                    color: Theme.alpha(Theme.accent, 0.90)
                    border.color: Theme.alpha(Theme.accentBright, 0.30); border.width: 1

                    Image {
                        anchors.centerIn: parent
                        width: 18; height: 18
                        source: "icons/lumen-mark-white.svg"
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }

                    // Online status dot — shows connected when model is available
                    Rectangle {
                        width: 8; height: 8; radius: 4
                        color: backend.hasActiveModel ? Theme.ok : Theme.ink4
                        anchors { right: parent.right; bottom: parent.bottom; rightMargin: -1; bottomMargin: -1 }
                        border.color: Theme.mode === "light" ? Theme.alpha(Theme.surface, 0.98) : Theme.alpha("#0E0C18", 0.95)
                        border.width: 1.5
                        Behavior on color { ColorAnimation { duration: 200 } }
                    }
                }

                ColumnLayout {
                    spacing: 1
                    Text { text: "Lumen"; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsCaption + 1; font.weight: Font.DemiBold }
                    Text {
                        text: backend.hasActiveModel ? "En línea · responde al instante" : "Tu asistente"
                        color: backend.hasActiveModel ? Theme.ok : Theme.ink4
                        font.family: Theme.font; font.pixelSize: Theme.tsMicro
                        Behavior on color { ColorAnimation { duration: 200 } }
                    }
                }

                Item { Layout.fillWidth: true }

                // New conversation — ghost button
                Rectangle {
                    radius: Theme.rSm; height: 28; color: "transparent"
                    border.color: Theme.line; border.width: 1
                    implicitWidth: newConvLabel.width + Theme.sp2

                    Text {
                        id: newConvLabel; anchors.centerIn: parent
                        text: "Nueva conversación"
                        color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro
                    }
                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            messages.clear()
                            chatView.conversationId = ""
                            chatView.streamingIndex = -1
                            chatView.showTyping = false
                            chatView.isStreaming = false
                            chatView.lastUnservicedMessage = ""
                            chatView.unservicedCardIndex = -1
                        }
                    }
                }
            }
        }

        // ── message thread ──────────────────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ListView {
                id: threadList
                anchors.fill: parent
                clip: true; model: messages; spacing: Theme.sp1
                topMargin: Theme.sp2; bottomMargin: Theme.sp1
                boundsBehavior: Flickable.StopAtBounds

                WheelScroll { target: threadList }

                ScrollBar.vertical: ScrollBar {
                    policy: ScrollBar.AsNeeded
                    contentItem: Rectangle { radius: 2; color: Theme.alpha(Theme.ink3, 0.30) }
                }

                onCountChanged: {
                    // Auto-scroll to end unless user has scrolled up intentionally
                    if (!chatView.userScrolledUp) {
                        Qt.callLater(function() { threadList.positionViewAtEnd() })
                    }
                }

                // Track whether user has scrolled away from the bottom
                property bool _atBottom: true

                onMovementEnded: {
                    var threshold = 80
                    var distFromEnd = contentHeight - (contentY + height)
                    _atBottom = distFromEnd < threshold
                    chatView.userScrolledUp = !_atBottom
                    if (_atBottom) newMsgFab.hide()
                }

                delegate: Item {
                    id: msgDelegate
                    width: threadList.width
                    height: bubbleCol.height + Theme.sp1

                    property bool isUser: model.sender === "user"
                    property bool isActionCard: model.isActionCard

                    // Avatar (Lumen side) — mark on accent circle
                    Rectangle {
                        visible: !msgDelegate.isUser && !msgDelegate.isActionCard
                        width: 28; height: 28; radius: 14
                        anchors { left: parent.left; leftMargin: Theme.sp2; top: parent.top; topMargin: 4 }
                        color: Theme.alpha(Theme.accent, 0.85)
                        border.color: Theme.alpha(Theme.accentBright, 0.25); border.width: 1

                        Image {
                            anchors.centerIn: parent
                            width: 15; height: 15
                            source: "icons/lumen-mark-white.svg"
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    Column {
                        id: bubbleCol
                        anchors {
                            left: msgDelegate.isUser ? undefined : parent.left
                            right: msgDelegate.isUser ? parent.right : undefined
                            leftMargin: msgDelegate.isUser ? 0 : (msgDelegate.isActionCard ? Theme.sp2 : 50)
                            rightMargin: msgDelegate.isUser ? Theme.sp2 : 0
                        }
                        width: msgDelegate.isActionCard
                               ? (threadList.width - Theme.sp2 * 2)
                               : Math.min(500, threadList.width * 0.70)
                        spacing: 5

                        // Message bubble — not shown for pure action-card rows
                        Rectangle {
                            width: parent.width
                            height: bubbleTxt.height + Theme.sp3
                            radius: Theme.rLg
                            visible: !msgDelegate.isActionCard && model.cardType !== "turn_error"
                            color: msgDelegate.isUser
                                   ? Theme.alpha(Theme.accent, 0.22)
                                   : Theme.card
                            border.color: msgDelegate.isUser
                                          ? Theme.alpha(Theme.accentBright, 0.28)
                                          : Theme.line
                            border.width: 1

                            Text {
                                id: bubbleTxt
                                anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.sp2; topMargin: Theme.sp1 + 2 }
                                // Append streaming caret ▌ when this is the active streaming bubble.
                                // `index` is the delegate's row index in the ListView (not model.index).
                                text: {
                                    var t = model.text
                                    if (!msgDelegate.isUser
                                            && chatView.streamingIndex === index
                                            && chatView.isStreaming) {
                                        t = t + "▌"
                                    }
                                    return t
                                }
                                color: Theme.ink
                                font.family: Theme.font; font.pixelSize: Theme.tsBody
                                wrapMode: Text.WordWrap; lineHeight: 1.50
                            }
                        }

                        // Card loader — ChatActionCard for no_model / turn_error; tool_event card
                        Loader {
                            active: model.hasCard
                            width: parent.width
                            sourceComponent: {
                                if (model.cardType === "tool_event") return toolEventCardComp
                                if (model.cardType === "no_model")   return noModelCardComp
                                if (model.cardType === "turn_error") return turnErrorCardComp
                                return null
                            }
                        }

                        Text {
                            anchors { right: msgDelegate.isUser ? parent.right : undefined }
                            text: model.timestamp
                            color: Theme.ink4; font.family: Theme.font; font.pixelSize: Theme.tsMicro
                            topPadding: 2
                            visible: !msgDelegate.isActionCard
                        }
                    }
                }

                // Typing indicator — three dots, static (no animation per perf rules)
                Item {
                    visible: chatView.showTyping
                    width: threadList.width; height: 52

                    Rectangle {
                        width: 28; height: 28; radius: 14
                        anchors { left: parent.left; leftMargin: Theme.sp2; verticalCenter: parent.verticalCenter }
                        color: Theme.alpha(Theme.accent, 0.85)
                        border.color: Theme.alpha(Theme.accentBright, 0.25); border.width: 1

                        Image {
                            anchors.centerIn: parent
                            width: 15; height: 15
                            source: "icons/lumen-mark-white.svg"
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    Rectangle {
                        anchors { left: parent.left; leftMargin: 50; verticalCenter: parent.verticalCenter }
                        width: 50; height: 32; radius: Theme.rMd
                        color: Theme.card; border.color: Theme.line; border.width: 1
                        Text {
                            anchors.centerIn: parent; text: "···"
                            color: Theme.ink3; font.family: Theme.font; font.pixelSize: 16
                            font.weight: Font.Medium; font.letterSpacing: 2
                        }
                    }
                }

                footer: Item { height: Theme.sp1 }
            }

            // ── NewMessagesFab — floats above the composer ─────────────────
            NewMessagesFab {
                id: newMsgFab
                anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: Theme.sp2 }
                visible: chatView.userScrolledUp && chatView.isStreaming
                onVisibleChanged: {
                    if (visible) show()
                    else hide()
                }
                onTapped: {
                    threadList.positionViewAtEnd()
                    chatView.userScrolledUp = false
                }
            }
        }

        // Track user scroll position for FAB display
        // ── no-model invitation banner (dismissable) ──────────────────────
        Rectangle {
            Layout.fillWidth: true
            visible: chatView.showBanner
            height: visible ? (noModelChatRow.height + Theme.sp3) : 0
            color: Theme.alpha(Theme.accent, 0.06)
            border.color: Theme.alpha(Theme.accentBright, 0.20); border.width: 1

            // Fade out when hasActiveModel transitions to true
            opacity: chatView.showBanner ? 1.0 : 0.0
            Behavior on opacity { NumberAnimation { duration: 200; easing.type: Easing.InCubic } }
            Behavior on height   { NumberAnimation { duration: 200; easing.type: Easing.InCubic } }

            RowLayout {
                id: noModelChatRow
                anchors {
                    left: parent.left; right: parent.right; top: parent.top
                    leftMargin: Theme.sp2; rightMargin: Theme.sp2; topMargin: Theme.sp1 + 2
                }
                spacing: Theme.sp1

                Image {
                    width: 14; height: 14
                    source: Theme.accentIcon("icons/sparkles-accent.svg")
                    fillMode: Image.PreserveAspectFit
                    smooth: true; mipmap: true
                    opacity: 0.70
                }

                ColumnLayout {
                    spacing: 1; Layout.fillWidth: true

                    Text {
                        text: "Tu asistente está casi listo"
                        color: Theme.ink; font.family: Theme.font
                        font.pixelSize: Theme.tsCaption + 1; font.weight: Font.DemiBold
                    }
                    Text {
                        text: "Conéctale un servicio para que pueda responder y ayudarte"
                        color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                    }
                }

                Rectangle {
                    height: 28; radius: Theme.rSm
                    implicitWidth: chatConnectTxt.width + Theme.sp2
                    color: Theme.accent
                    Text {
                        id: chatConnectTxt; anchors.centerIn: parent
                        text: "Conectar ahora"
                        color: "white"; font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.Medium
                    }
                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: { if (chatView.shell) chatView.shell.go(8) }
                    }
                }

                // Dismiss — "Más tarde"
                Text {
                    text: "Más tarde"
                    color: Theme.ink4; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                    leftPadding: 2

                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            chatView.bannerDismissed = true
                            // Persist dismissal so it doesn't reappear after reboot
                            if (typeof backend.setSetting === "function") {
                                backend.setSetting("chat_banner_dismissed", "true")
                            }
                        }
                    }
                }
            }
        }

        // ── composer ────────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true; height: 68
            color: Theme.mode === "light" ? Theme.alpha(Theme.surface, 0.98) : Theme.alpha("#0C0B14", 0.96)
            border.color: Theme.line; border.width: 1

            // Top hairline
            Rectangle {
                anchors { top: parent.top; left: parent.left; right: parent.right }
                height: 1; color: Theme.line
            }

            RowLayout {
                anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2; topMargin: Theme.sp1; bottomMargin: Theme.sp1 }
                spacing: Theme.sp1

                Rectangle {
                    Layout.fillWidth: true; height: 44; radius: Theme.rMd
                    color: Theme.card2
                    border.color: composerInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.45) : Theme.line
                    border.width: 1

                    Behavior on border.color { ColorAnimation { duration: 150 } }

                    RowLayout {
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp1 }
                        spacing: Theme.sp1

                        // Sparkles icon — accent, not emoji
                        Image {
                            width: 14; height: 14
                            source: Theme.accentIcon("icons/sparkles-accent.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                            opacity: 0.55
                        }

                        Item {
                            Layout.fillWidth: true; height: 38
                            TextInput {
                                id: composerInput
                                anchors.fill: parent; verticalAlignment: Text.AlignVCenter
                                // Always editable — model-deferred design
                                color: Theme.ink
                                font.family: Theme.font; font.pixelSize: Theme.tsBody; clip: true

                                // Enter = send, Shift+Enter = newline, Esc = stop streaming
                                Keys.onReturnPressed: function(event) {
                                    if (event.modifiers & Qt.ShiftModifier) {
                                        // Insert newline
                                        composerInput.insert(composerInput.cursorPosition, "\n")
                                    } else {
                                        event.accepted = true
                                        if (composerInput.text.trim().length > 0) {
                                            chatView._sendMessage(composerInput.text.trim())
                                            composerInput.text = ""
                                        }
                                    }
                                }
                                Keys.onEscapePressed: {
                                    if (chatView.isStreaming) chatView._stopStreaming()
                                }
                            }
                            Text {
                                visible: composerInput.text.length === 0
                                anchors.fill: parent; verticalAlignment: Text.AlignVCenter
                                text: backend.hasActiveModel
                                      ? "Escribe algo a Lumen…"
                                      : "Escribe tu mensaje…"
                                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsBody
                            }
                        }

                        // Mic button
                        Rectangle {
                            width: 30; height: 30; radius: Theme.rSm
                            color: "transparent"
                            border.color: Theme.line; border.width: 1

                            Image {
                                anchors.centerIn: parent
                                width: 14; height: 14
                                source: Theme.dimIcon("icons/mic-dim.svg")
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }
                        }
                    }
                }

                // Stop button (⏹) — visible during streaming; replaces send button
                Rectangle {
                    visible: chatView.isStreaming
                    width: 44; height: 44; radius: Theme.rMd
                    color: Theme.alpha(Theme.warn, 0.14)
                    border.color: Theme.alpha(Theme.warn, 0.32); border.width: 1

                    Text {
                        anchors.centerIn: parent
                        text: "⏹"
                        color: Theme.warnText
                        font.pixelSize: 16
                    }

                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: chatView._stopStreaming()
                    }

                    Accessible.role: Accessible.Button
                    Accessible.name: "Detener generación"
                }

                // Send button — flat accent, arrow-up icon; hidden during streaming
                Rectangle {
                    visible: !chatView.isStreaming
                    width: 44; height: 44; radius: Theme.rMd
                    color: Theme.accent
                    // Full opacity when there's text; dimmed when empty
                    opacity: composerInput.text.trim().length > 0 ? 1.0 : 0.30
                    Behavior on opacity { NumberAnimation { duration: 150 } }

                    Image {
                        anchors.centerIn: parent
                        width: 20; height: 20
                        source: "icons/arrow-up-white.svg"
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }

                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (composerInput.text.trim().length > 0) {
                                chatView._sendMessage(composerInput.text.trim())
                                composerInput.text = ""
                            }
                        }
                    }
                }
            }
        }
    }

    // ── card components ────────────────────────────────────────────────────

    // No-model action card — placed in thread when user sends without a service
    Component {
        id: noModelCardComp
        ChatActionCard {
            width: parent ? parent.width : 0
            copyText: "Aún no he podido responder. Conecta un servicio y vuelvo a intentarlo."
            ctaText: "Conectar servicio"
            ctaAction: function() { if (chatView.shell) chatView.shell.go(8) }
        }
    }

    // Turn error card — replaces the old red bubble
    Component {
        id: turnErrorCardComp
        ChatActionCard {
            width: parent ? parent.width : 0
            copyText: model ? model.text : "Algo salió mal."
            ctaText: "Reintentar"
            ctaAction: function() {
                // Remove this card and the user message before it, then re-send.
                // The simplest safe approach: just re-send the last user message
                // from the model (walk back to find sender=user).
                var lastUserMsg = ""
                for (var i = messages.count - 1; i >= 0; i--) {
                    var m = messages.get(i)
                    if (m.sender === "user") { lastUserMsg = m.text; break }
                }
                if (lastUserMsg.length > 0 && backend.hasActiveModel) {
                    chatView._sendMessage(lastUserMsg)
                }
            }
        }
    }

    Component {
        id: toolEventCardComp
        Rectangle {
            width: parent ? parent.width : 0
            height: toolCardContent.height + Theme.sp2 + 4
            radius: Theme.rMd; color: Theme.card2; border.color: Theme.line; border.width: 1

            Row {
                id: toolCardContent
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.sp1 + 2; topMargin: Theme.sp1 }
                spacing: Theme.sp1

                Rectangle {
                    width: 24; height: 24; radius: Theme.rSm - 2
                    color: Theme.alpha(Theme.accent, 0.16)
                    border.color: Theme.alpha(Theme.accentBright, 0.16); border.width: 1

                    Image {
                        anchors.centerIn: parent
                        width: 12; height: 12
                        source: Theme.dimIcon("icons/settings-dim.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                }

                Column {
                    spacing: 2
                    Text { text: "Herramienta: " + model.text; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.Medium }
                    Text { text: "Acción ejecutada por tu asistente"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro }
                }
            }
        }
    }

    Component {
        id: hotelCardComp
        Rectangle {
            width: parent ? parent.width : 0
            height: hotelCardContent.height + Theme.sp3
            radius: Theme.rMd; color: Theme.card; border.color: Theme.line; border.width: 1

            Column {
                id: hotelCardContent
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.sp2; topMargin: Theme.sp1 + 2 }
                spacing: Theme.sp1

                RowLayout {
                    width: parent.width; spacing: Theme.sp1

                    Rectangle {
                        width: 26; height: 26; radius: Theme.rSm - 2
                        color: Theme.alpha(Theme.accent, 0.16)
                        border.color: Theme.alpha(Theme.accentBright, 0.16); border.width: 1

                        Image {
                            anchors.centerIn: parent
                            width: 13; height: 13
                            source: Theme.dimIcon("icons/globe-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    Column {
                        spacing: 1
                        Text { text: "Abrí el navegador · busqué hoteles en Lisboa"; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.Medium }
                        Text { text: "Encontré 3 opciones bajo 80 € · fin de semana"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro }
                    }

                    Item { Layout.fillWidth: true }

                    Rectangle {
                        height: 18; radius: Theme.rSm - 4; color: Theme.alpha(Theme.ok, 0.14)
                        border.color: Theme.alpha(Theme.ok, 0.18); border.width: 1
                        implicitWidth: doneLabel.width + Theme.sp1 + 4
                        Text { id: doneLabel; anchors.centerIn: parent; text: "Hecho"; color: Theme.ok; font.family: Theme.font; font.pixelSize: Theme.tsMicro }
                    }
                }

                Rectangle { width: parent.width; height: 1; color: Theme.line }

                Column {
                    width: parent.width; spacing: 5
                    Repeater {
                        model: [
                            { name: "Hotel Bairro Alto", stars: "★★★★", price: "74 €", note: "Centro histórico · desayuno incluido" },
                            { name: "LX Boutique Hotel", stars: "★★★★", price: "68 €", note: "Junto al río · WiFi 5G" },
                            { name: "Pensão Amor",        stars: "★★★",  price: "55 €", note: "Barrio de Bairro Alto · encantador" }
                        ]

                        Rectangle {
                            width: parent ? parent.width : 0
                            height: hotelRow.height + Theme.sp2
                            radius: Theme.rSm; color: Theme.card2; border.color: Theme.line; border.width: 1

                            RowLayout {
                                id: hotelRow
                                anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.sp1; topMargin: Theme.sp1 }
                                spacing: Theme.sp1

                                Column {
                                    spacing: 2
                                    Text { text: modelData.name; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.Medium }
                                    Row {
                                        spacing: 5
                                        Text { text: modelData.stars; color: Theme.warn; font.pixelSize: Theme.tsMicro }
                                        Text { text: modelData.note; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro }
                                    }
                                }

                                Item { Layout.fillWidth: true }

                                Column {
                                    spacing: 3
                                    Text { text: modelData.price + "/noche"; color: Theme.accentBright; font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.DemiBold; anchors.right: parent.right }
                                    Text {
                                        text: "Ver"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro; anchors.right: parent.right
                                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: { if (chatView.shell) chatView.shell.go(2) } }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Component {
        id: permissionCardComp
        Rectangle {
            width: parent ? parent.width : 0
            height: permCardContent.height + Theme.sp3
            radius: Theme.rMd; color: Theme.card
            border.color: Theme.alpha(Theme.warn, 0.40); border.width: 1

            Column {
                id: permCardContent
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.sp2; topMargin: Theme.sp1 + 2 }
                spacing: Theme.sp1

                RowLayout {
                    width: parent.width; spacing: Theme.sp1
                    Rectangle {
                        width: 28; height: 28; radius: Theme.rSm
                        color: Theme.alpha(Theme.warn, 0.14)

                        Image {
                            anchors.centerIn: parent
                            width: 14; height: 14
                            source: Theme.dimIcon("icons/lock-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }
                    Column {
                        spacing: 2
                        Text { text: "Permiso requerido"; color: Theme.warn; font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.DemiBold }
                        Text { text: "Nada sale sin tu aprobación explícita"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro }
                    }
                }

                Rectangle { width: parent.width; height: 1; color: Theme.alpha(Theme.warn, 0.18) }

                Column {
                    width: parent.width; spacing: 4
                    Text { text: "Lumen quiere:"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption }
                    Repeater {
                        model: ["Leer tu método de pago guardado", "Realizar un cargo de 68 € × 2 noches = 136 €", "Compartir tus datos con LX Boutique Hotel"]
                        Row {
                            spacing: Theme.sp1; width: parent ? parent.width : 0
                            Rectangle {
                                width: 4; height: 4; radius: 2
                                color: Theme.warn; anchors.verticalCenter: parent.verticalCenter
                            }
                            Text { text: modelData; color: Theme.ink2; font.family: Theme.font; font.pixelSize: Theme.tsCaption; wrapMode: Text.WordWrap; width: parent.width - 16 }
                        }
                    }
                }

                RowLayout {
                    width: parent.width; spacing: Theme.sp1
                    Rectangle {
                        Layout.fillWidth: true; height: 32; radius: Theme.rSm
                        color: "transparent"; border.color: Theme.line; border.width: 1
                        Text { anchors.centerIn: parent; text: "Cancelar"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption }
                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor }
                    }
                    Rectangle {
                        Layout.fillWidth: true; height: 32; radius: Theme.rSm
                        color: Theme.alpha(Theme.warn, 0.16); border.color: Theme.alpha(Theme.warn, 0.40); border.width: 1
                        Text { anchors.centerIn: parent; text: "Aprobar reserva"; color: Theme.warn; font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.Medium }
                        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor }
                    }
                }
            }
        }
    }
}
