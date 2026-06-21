import QtQuick
import QtQuick.Layouts
import QtWayland.Compositor
import Qt5Compat.GraphicalEffects
import "." // Tokens es un singleton de qmldir: requiere import explícito del dir local

Rectangle {
    id: appWindow
    x: initialX
    y: initialY
    // width/height set once in Component.onCompleted — NOT live-bound to windowArea
    // so chat panel expanding/collapsing never resizes open windows
    width: Math.round(700 * root.sf)
    height: Math.round(450 * root.sf)
    radius: root.radiusLg
    color: root.bgSurface
    // Border warms when focused, cools when blurred
    border.color: appWindow.activeFocus
        ? Qt.rgba(240/255, 168/255, 90/255, 0.18)
        : Qt.rgba(46/255, 49/255, 60/255, 0.55)
    border.width: 1
    clip: true
    z: 10

    // ── Open/close lifecycle motion (Wave 1) ──
    // entry flag — set via Qt.callLater so Behavior fires on frame 1
    property bool entered: false
    // close-in-progress guard — prevents double invocation and instant Repeater destruction
    property bool closing: false

    opacity: entered ? 1.0 : 0.0
    scale:   entered ? 1.0 : 0.90

    Behavior on opacity {
        enabled: !Tokens.reduceMotion
        NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutCubic }
    }
    Behavior on scale {
        enabled: !Tokens.reduceMotion
        NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutCubic }
    }

    // Small vertical enter offset (y shifts 8px up on open)
    // Applied via transform so it doesn't move the logical position
    transform: Translate {
        id: enterTranslate
        y: appWindow.entered ? 0 : Math.round(8 * root.sf)
        Behavior on y {
            enabled: !Tokens.reduceMotion
            NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutCubic }
        }
    }

    // Animated maximize: Behavior on geometry — sends ONE configure at start,
    // then the LumenSO frame animates (not spamming per-frame resize to the surface).
    property bool _maxAnimating: false
    Behavior on x      { enabled: !Tokens.reduceMotion && appWindow._maxAnimating; NumberAnimation { duration: Tokens.durSlow; easing.type: Easing.InOutCubic } }
    Behavior on y      { enabled: !Tokens.reduceMotion && appWindow._maxAnimating; NumberAnimation { duration: Tokens.durSlow; easing.type: Easing.InOutCubic } }
    Behavior on width  { enabled: !Tokens.reduceMotion && appWindow._maxAnimating; NumberAnimation { duration: Tokens.durSlow; easing.type: Easing.InOutCubic } }
    Behavior on height { enabled: !Tokens.reduceMotion && appWindow._maxAnimating; NumberAnimation { duration: Tokens.durSlow; easing.type: Easing.InOutCubic } }

    // Tap-focus scale signature: tiny bounce when window gains focus
    property bool _tapScaling: false
    SequentialAnimation {
        id: focusTapAnim
        running: false
        NumberAnimation { target: appWindow; property: "scale"; to: 1.008; duration: 80;  easing.type: Easing.OutBack; easing.overshoot: 1.2 }
        NumberAnimation { target: appWindow; property: "scale"; to: 1.0;   duration: 80;  easing.type: Easing.InCubic }
    }

    // Timer to complete deferred-removal close after exit animation finishes
    Timer {
        id: closeFinishTimer
        interval: Tokens.durBase + 20
        repeat: false
        onTriggered: appWindow._doClose()
    }

    property string windowTitle: "App"
    property string windowIcon: ""
    property string appId: ""
    property Item windowArea: parent
    property int initialX: 100
    property int initialY: Math.round(80 * root.sf)

    // Maximize state
    property bool maximized: false
    property real savedX: 0
    property real savedY: 0
    property real savedW: 0
    property real savedH: 0

    function toggleMaximize() {
        _maxAnimating = true;
        if (maximized) {
            // Send a single configure to the client for the restored size (native apps)
            if (isNative && toplevelObj) {
                var restoreSize = Qt.size(savedW, savedH - Math.round(40 * root.sf));
                if (typeof toplevelObj.sendMaximized === "function")
                    toplevelObj.sendMaximized(restoreSize);
            }
            appWindow.x = savedX; appWindow.y = savedY;
            appWindow.width = savedW; appWindow.height = savedH;
            maximized = false;
        } else {
            savedX = appWindow.x; savedY = appWindow.y;
            savedW = appWindow.width; savedH = appWindow.height;
            if (windowArea) {
                var targetW = windowArea.width;
                var targetH = windowArea.height;
                // Send one configure at the new size BEFORE starting the frame animation
                if (isNative && toplevelObj) {
                    var maxSize = Qt.size(targetW, targetH - Math.round(40 * root.sf));
                    if (typeof toplevelObj.sendMaximized === "function")
                        toplevelObj.sendMaximized(maxSize);
                }
                appWindow.x = windowArea.x;
                appWindow.y = windowArea.y;
                appWindow.width = targetW;
                appWindow.height = targetH;
            }
            maximized = true;
        }
        // Re-disable the geometry Behaviors after animation completes so
        // user drag-resize is instant (not animated)
        maxAnimEndTimer.restart();
    }

    Timer {
        id: maxAnimEndTimer
        interval: Tokens.durSlow + 20
        repeat: false
        onTriggered: appWindow._maxAnimating = false
    }

    // Native app properties
    property bool isNative: appId.indexOf("native-") === 0 || appId.indexOf("wayland-") === 0
    property string nativeCmd: ""
    property string nativeSearchName: ""
    property int launchCountdown: 60

    // ── Responsive initial sizing ──
    // Sizes are relative to windowArea, not absolute pixels.
    // Compact screens (< bpCompact*sf wide): near-maximised with a small margin.
    // Normal screens: percentage-based with a sensible max, centred with cascade offset.
    // PRESERVED: clamp to windowArea so the window never opens outside the viewport.
    Component.onCompleted: {
        if (windowArea) {
            var areaW = windowArea.width;
            var areaH = windowArea.height;
            var margin = Math.round(12 * root.sf);
            var compactThresh = Tokens.bpCompact * root.sf;

            var initW, initH;
            if (areaW < compactThresh) {
                // Portrait / very small screen — near-maximised
                initW = areaW - margin * 2;
                initH = areaH - margin * 2;
            } else if (isNative) {
                // Native apps: 80% width, 75% height, cap at windowArea
                initW = Math.min(Math.round(areaW * 0.80), areaW - margin * 2);
                initH = Math.min(Math.round(areaH * 0.75), areaH - margin * 2);
            } else {
                // QML apps: 70% width, 68% height — comfortable on any 16:9 or wider
                initW = Math.min(Math.round(areaW * 0.70), areaW - margin * 2);
                initH = Math.min(Math.round(areaH * 0.68), areaH - margin * 2);
            }
            // Hard minimums so content is never crushed
            initW = Math.max(initW, Math.round(320 * root.sf));
            initH = Math.max(initH, Math.round(240 * root.sf));

            appWindow.width  = initW;
            appWindow.height = initH;

            // Centre on windowArea, then apply per-window cascade offset
            // (initialX/Y from Desktop.qml already carry the cascade delta; we
            // override them here with centred positions so stacking reads correctly)
            appWindow.x = Math.round((areaW - initW) / 2) + index * Math.round(28 * root.sf);
            appWindow.y = Math.round((areaH - initH) / 2) + index * Math.round(28 * root.sf);

            // Clamp so no part of the window falls outside windowArea
            appWindow.x = Math.max(0, Math.min(appWindow.x, areaW - initW));
            appWindow.y = Math.max(0, Math.min(appWindow.y, areaH - initH));
        }
        if (isNative && nativeCmd.length > 0) {
            nativeLauncher.start();
        }
        // Trigger open animation: defer so Behavior fires on the very next frame
        Qt.callLater(function() { appWindow.entered = true; });
    }

    property string nativeWinId: ""

    // Wayland surface (assigned by compositor)
    property var shellSurface: null
    property var toplevelObj: null

    function focusNativeSurface() {
        if (!isNative || !shellSurface || !surfaceItem) return;

        root.bringToFront(appWindow);
        if (typeof appWindow.forceActiveFocus === "function") appWindow.forceActiveFocus();
        if (typeof contentArea.forceActiveFocus === "function") contentArea.forceActiveFocus();
        if (typeof surfaceItem.forceActiveFocus === "function") surfaceItem.forceActiveFocus();
        if (typeof surfaceItem.takeFocus === "function") surfaceItem.takeFocus();

        // CRITICAL: Set Wayland-protocol-level keyboard focus.
        // Try multiple routes to get the actual WaylandSurface object:
        //   1. surfaceItem.surface  — ShellSurfaceItem exposes the WaylandSurface
        //   2. shellSurface.surface — XdgSurface.surface property
        //   3. shellSurface itself  — WlShellSurface IS a WaylandSurface
        try {
            var wlSurface = null;
            if (surfaceItem.surface)            wlSurface = surfaceItem.surface;
            if (!wlSurface && shellSurface.surface) wlSurface = shellSurface.surface;
            if (!wlSurface)                     wlSurface = shellSurface;

            if (comp.defaultSeat && wlSurface) {
                comp.defaultSeat.keyboardFocus = wlSurface;
                console.log("LumenSO: keyboard focus SET for " + appWindow.windowTitle);
            } else {
                console.log("LumenSO: keyboard focus SKIP — seat:" + !!comp.defaultSeat + " surface:" + !!wlSurface);
            }
        } catch(e) {
            console.log("LumenSO: keyboard focus ERROR: " + e);
        }
    }

    // When a native surface arrives, configure it to fill the content area
    // (the area below LumenSO's title bar)
    onShellSurfaceChanged: { configureNativeSurface(); focusNativeSurface(); }
    onToplevelObjChanged: { configureNativeSurface(); focusNativeSurface(); }
    // Envía el configure inicial al cliente Wayland. CRÍTICO: sin un configure
    // con tamaño > 0 el cliente NUNCA adjunta buffer ni dibuja (ventana en
    // blanco) — pasa con foot/kgx/qterminal y CUALQUIER app nativa. Antes era
    // un one-shot a 150ms: si el surface llegaba después o contentArea aún era 0
    // se perdía y no reintentaba (race). Ahora reintenta hasta que el área tiene
    // tamaño, envía el configure varias veces (asegura el ack) y luego para.
    Timer {
        id: surfaceConfigureTimer
        interval: 120; repeat: true
        property int sent: 0
        property int waited: 0
        onTriggered: {
            if (toplevelObj && contentArea.width > 0 && contentArea.height > 0) {
                var sz = Qt.size(contentArea.width, contentArea.height);
                // sendMaximized = configure con estado maximized + tamaño; con SSD
                // el cliente llena el área y no dibuja su propia barra.
                if (typeof toplevelObj.sendMaximized === "function") {
                    toplevelObj.sendMaximized(sz);
                } else if (typeof toplevelObj.sendConfigure === "function") {
                    toplevelObj.sendConfigure(sz, []);
                }
                sent++;
                if (sent >= 3) { stop(); sent = 0; waited = 0; }
            } else {
                waited++;
                if (waited > 50) { stop(); waited = 0; }  // ~6s — el surface no llegó
            }
        }
    }
    function configureNativeSurface() {
        if (!isNative || !toplevelObj) return;
        surfaceConfigureTimer.sent = 0;
        surfaceConfigureTimer.waited = 0;
        surfaceConfigureTimer.restart();
    }

    // Drop shadow (elevation: modal) — replaces the old z:-1 fake border
    DropShadow {
        anchors.fill: appWindow
        source: appWindow
        horizontalOffset: 0
        verticalOffset: 12
        radius: 32
        samples: 65
        color: Qt.rgba(0, 0, 0, 0.48)
        spread: 0
        cached: true
        z: -1
    }

    // ── Title Bar (always shown — compositor provides window controls) ──
    Rectangle {
        id: titleBar
        anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right
        height: Math.round(40 * root.sf)
        // Glass-look titlebar (card tint — no double blur since parent is NOT glass)
        color: appWindow.activeFocus
            ? Qt.rgba(32/255, 34/255, 43/255, 0.95)
            : Qt.rgba(27/255, 29/255, 36/255, 0.95)
        radius: root.radiusLg
        Behavior on color { ColorAnimation { duration: 200 } }

        Rectangle { anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right; height: parent.radius; color: parent.color }
        // Single amber hairline accent line — only shows when focused
        Rectangle {
            anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right; height: 1
            opacity: appWindow.activeFocus ? 1.0 : 0.35
            Behavior on opacity { NumberAnimation { duration: 200 } }
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: "transparent" }
                GradientStop { position: 0.2; color: Qt.rgba(240/255, 168/255, 90/255, 0.30) }
                GradientStop { position: 0.5; color: Qt.rgba(240/255, 168/255, 90/255, 0.45) }
                GradientStop { position: 0.8; color: Qt.rgba(240/255, 168/255, 90/255, 0.30) }
                GradientStop { position: 1.0; color: "transparent" }
            }
        }

        MouseArea {
            id: dragArea; anchors.fill: parent; drag.target: maximized ? null : appWindow
            drag.minimumX: -appWindow.width + Math.round(100 * root.sf); drag.minimumY: 0
            drag.maximumX: windowArea ? windowArea.width - Math.round(100 * root.sf) : 800
            drag.maximumY: windowArea ? windowArea.height - Math.round(40 * root.sf) : 600
            cursorShape: Qt.SizeAllCursor
            onPressed: function(mouse) {
                root.bringToFront(appWindow);
                // Tap-focus signature — tiny spring bounce on focus claim
                if (!Tokens.reduceMotion) focusTapAnim.restart();
            }
            onDoubleClicked: toggleMaximize()
        }

        RowLayout {
            anchors.fill: parent; anchors.leftMargin: Math.round(14 * root.sf); anchors.rightMargin: Math.round(8 * root.sf); spacing: Math.round(8 * root.sf)

            Text {
                text: appWindow.windowTitle
                font.family: Tokens.fontBody
                font.pixelSize: Math.round(13 * root.sf); font.weight: Font.DemiBold
                color: Tokens.textPrimary; Layout.fillWidth: true
                elide: Text.ElideRight
            }

            // ── Maximize Button ──
            Item {
                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf)
                Layout.alignment: Qt.AlignVCenter

                Rectangle {
                    anchors.centerIn: parent
                    width: Math.round(14 * root.sf); height: Math.round(14 * root.sf); radius: width / 2
                    color: maxHover.containsMouse ? Tokens.successBase : Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.3)
                    border.color: Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.5); border.width: 0.5
                }
                MouseArea { id: maxHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: toggleMaximize() }
            }

            // ── Close Button ──
            Item {
                width: Math.round(28 * root.sf); height: Math.round(28 * root.sf)
                Layout.alignment: Qt.AlignVCenter

                Rectangle {
                    anchors.centerIn: parent
                    width: Math.round(14 * root.sf); height: Math.round(14 * root.sf); radius: width / 2
                    color: closeHover.containsMouse ? Tokens.dangerBase : Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.3)
                    border.color: Qt.rgba(Tokens.dangerBase.r, Tokens.dangerBase.g, Tokens.dangerBase.b, 0.5); border.width: 0.5
                }
                MouseArea { id: closeHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: closeWindow() }
            }
        }
    }


    // ── Body ──
    Item {
        id: contentArea
        clip: true  // Prevent native app surface from overflowing
        anchors.top: titleBar.bottom; anchors.left: parent.left
        anchors.right: parent.right; anchors.bottom: parent.bottom

        // Re-send configure when window is resized so native app fills properly
        onWidthChanged: if (isNative && toplevelObj) configureNativeSurface()
        onHeightChanged: if (isNative && toplevelObj) configureNativeSurface()

        // NOTE: No overlay MouseArea here! For native apps, the ShellSurfaceItem
        // must be the direct recipient of all input events (mouse + keyboard).
        // An overlay MouseArea would intercept events before they reach the
        // Wayland client, breaking keyboard and click-to-focus.

        Loader {
            anchors.fill: parent
            visible: !isNative
            source: {
                if (isNative) return "";
                if (appId === "nativeapps") return "NativeAppsLauncher.qml";
                if (appId === "settings") return "SettingsApp.qml";
                if (appId === "providers") return "ProvidersApp.qml";
                if (appId === "integrations") return "IntegrationsApp.qml";
                if (appId === "skills") return "SkillsApp.qml";
                if (appId === "extensions") return "AppsApp.qml";
                if (appId === "terminal") return "TerminalApp.qml";
                if (appId === "mcp") return "McpApp.qml";
                if (appId === "agents") return "AgentsApp.qml";
                if (appId === "tasks") return "TasksApp.qml";
                if (appId === "files") return "FileManagerApp.qml";
                if (appId === "security") return "SecurityCenterApp.qml";
                return "";
            }
        }

        // Embedded Wayland surface (rendered by compositor)
        ShellSurfaceItem {
            id: surfaceItem
            anchors.fill: parent
            visible: shellSurface !== null
            // CRÍTICO: sin esto el ShellSurfaceItem (anchors.fill + focusOnClick) se
            // TRAGA el input incluso en apps QML (shellSurface null) → la ventana no
            // recibe clicks y bloquea el dock/SO. enabled lo desactiva para apps QML;
            // solo las apps nativas Wayland (con surface) reciben input directo.
            enabled: shellSurface !== null
            shellSurface: appWindow.shellSurface
            autoCreatePopupItems: true
            focusOnClick: true  // Give Qt focus when clicked, so wl_keyboard events flow

            // When this item gets Qt focus (user clicked), set wl_keyboard focus
            // on the Wayland seat so key events are forwarded to the client.
            onActiveFocusChanged: {
                if (activeFocus && isNative && shellSurface !== null && comp.defaultSeat) {
                    var surf = shellSurface.surface || shellSurface;
                    if (surf) comp.defaultSeat.keyboardFocus = surf;
                }
            }

            onSurfaceDestroyed: {
                // Native app closed itself — close the AppWindow too
                closeWindow();
            }
        }
        // NOTE: Right-click blocking is handled at the C++ level in main.cpp
        // via RightClickFilter. QML MouseArea cannot intercept Wayland pointer events.

        // Native app loading indicator (shown until surface arrives or timeout)

        Column {
            anchors.centerIn: parent
            spacing: Math.round(12 * root.sf)
            visible: isNative && shellSurface === null && appWindow.launchCountdown > 0

            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Math.round(6 * root.sf)
                Repeater {
                    model: 3
                    Rectangle {
                        width: Math.round(8 * root.sf); height: Math.round(8 * root.sf); radius: width / 2; color: Tokens.accentBase
                        SequentialAnimation on opacity {
                            running: isNative && shellSurface === null; loops: Animation.Infinite
                            PauseAnimation { duration: index * 200 }
                            NumberAnimation { to: 0.2; duration: 400 }
                            NumberAnimation { to: 1.0; duration: 400 }
                            PauseAnimation { duration: (2 - index) * 200 }
                        }
                    }
                }
            }

            Text {
                text: "Launching " + windowTitle + "..."
                font.family: Tokens.fontBody
                font.pixelSize: Math.round(13 * root.sf); color: Tokens.textMuted
                anchors.horizontalCenter: parent.horizontalCenter
            }
        }

        // Auto-close if app doesn't produce a surface in time
        Timer {
            id: launchTimeout
            interval: 1000; running: isNative && shellSurface === null && appWindow.launchCountdown > 0; repeat: true
            onTriggered: {
                appWindow.launchCountdown--;
                if (appWindow.launchCountdown <= 0) {
                    root.showToast(windowTitle + " did not open — it may be a CLI tool", "info");
                    closeWindow();
                }
            }
        }
    }

    // ── Native App Launch ──
    Timer {
        id: nativeLauncher
        interval: 200; running: false; repeat: false
        onTriggered: {
            if (!isNative || nativeCmd.length === 0) return;
            sysManager.launchNativeApp(nativeCmd);
        }
    }


    function closeWindow() {
        // Guard re-entry (surface destroyed → onSurfaceDestroyed → closeWindow → again)
        if (appWindow.closing) return;
        appWindow.closing = true;

        if (Tokens.reduceMotion) {
            // No animation path — instant removal
            _doClose();
            return;
        }

        // Exit animation: scale down + fade — defers actual removal until animation ends
        appWindow.entered = false;     // reverses the entry Behaviors (scale 1→0.90, opacity 1→0)
        closeFinishTimer.start();      // remove from openWindows after durBase ms
    }

    // Actual teardown — called after exit animation by closeFinishTimer,
    // or immediately when reduceMotion is true.
    function _doClose() {
        try {
            var wins = root.openWindows;
            var newWins = [];
            for (var i = 0; i < wins.length; i++) {
                if (wins[i].appId !== appId) newWins.push(wins[i]);
            }
            root.openWindows = newWins;
        } catch(e) {
            console.log("LumenSO: openWindows cleanup: " + e);
        }

        // Signal Wayland surface
        if (toplevelObj) {
            if (typeof toplevelObj.sendClose === "function") {
                toplevelObj.sendClose();
            } else if (shellSurface) {
                try {
                    var surf = shellSurface.surface || shellSurface;
                    if (surf && surf.client) surf.client.close();
                } catch(e2) {
                    console.log("LumenSO: closeWindow fallback error: " + e2);
                }
            }
        }

        appWindow.visible = false;
    }

    // ── Resize Handle ──
    MouseArea {
        width: Math.round(16 * root.sf); height: Math.round(16 * root.sf)
        anchors.right: parent.right; anchors.bottom: parent.bottom
        cursorShape: Qt.SizeFDiagCursor
        property point pressPos
        onPressed: function(mouse) { pressPos = Qt.point(mouse.x, mouse.y); root.bringToFront(appWindow); }
        onPositionChanged: function(mouse) {
            var dx = mouse.x - pressPos.x;
            var dy = mouse.y - pressPos.y;
            appWindow.width = Math.max(Math.round(350 * root.sf), appWindow.width + dx);
            appWindow.height = Math.max(Math.round(250 * root.sf), appWindow.height + dy);
        }
    }
}
