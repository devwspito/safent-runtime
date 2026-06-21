/*
 * TerminalApp.qml — Launcher de la terminal nativa (kgx / GNOME Console).
 *
 * Antes: emulador PTY/VT100 propio (668 líneas QML) que importaba
 * `LumenSO.Terminal 1.0` — un módulo C++ que NUNCA estuvo registrado en el
 * compositor. La ventana abría con fondo negro pero nada respondía (auditoría
 * 2026-06-10 + reporte del usuario "parece Windows 95"). Aquí va una versión
 * funcional y estética: lanzamos `kgx` (GNOME Console, ya horneado en la
 * imagen) como cliente Wayland nativo. kgx es compatible con wl_seat v4 (foot
 * exige v5 → falla, ver feedback_lumenso_terminal_foot_diagnostico). El surface
 * del kgx aterriza en su propio AppWindow vía el surface-tracker del
 * compositor (searchName: "kgx"). Esta tarjeta se cierra sola tras lanzar.
 *
 * Reskin Sereno 2026-06-14: bgSunken + fontMono + Tokens amber dots.
 * Wiring del terminal (sysManager.launchNativeApp / closeTimer) intacto.
 */

import QtQuick
import "."

Rectangle {
    id: terminalApp
    anchors.fill: parent
    color: Tokens.bgVoid

    // Splash card mientras kgx arranca (≈300-700 ms)
    Rectangle {
        anchors.centerIn: parent
        width: Math.round(280 * root.sf)
        height: Math.round(96 * root.sf)
        radius: Math.round(Tokens.radiusMd * root.sf)
        color: Tokens.bgCard
        border.color: Tokens.borderDefault
        border.width: 1

        Column {
            anchors.centerIn: parent
            spacing: Math.round(Tokens.spSm * root.sf)

            // Loading dots — amber accent, staggered pulse
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Math.round(Tokens.spSm * root.sf)

                Repeater {
                    model: 3
                    Rectangle {
                        width: Math.round(7 * root.sf)
                        height: Math.round(7 * root.sf)
                        radius: width / 2
                        color: Tokens.accentBase

                        SequentialAnimation on opacity {
                            loops: Animation.Infinite
                            running: !Tokens.reduceMotion
                            PauseAnimation { duration: index * 160 }
                            NumberAnimation { to: 0.22; duration: 340; easing.type: Easing.InOutCubic }
                            NumberAnimation { to: 1.0;  duration: 340; easing.type: Easing.InOutCubic }
                            PauseAnimation { duration: (2 - index) * 160 }
                        }
                    }
                }
            }

            Text {
                text: "Abriendo terminal…"
                color: Tokens.textSecondary
                font.pixelSize: Math.round(12 * root.sf)
                font.family: Tokens.fontMono
                anchors.horizontalCenter: parent.horizontalCenter
            }
        }
    }

    // ── Wiring unchanged ─────────────────────────────────────────────────────
    Timer {
        id: closeTimer
        interval: 700
        repeat: false
        onTriggered: {
            // Remove placeholder AppWindow; kgx surface is already up (searchName "kgx").
            var wins = root.openWindows.slice();
            var filtered = wins.filter(function(w) { return w.appId !== "terminal"; });
            root.openWindows = filtered;
        }
    }

    Component.onCompleted: {
        if (typeof sysManager !== "undefined" && sysManager) {
            sysManager.launchNativeApp("kgx");
        }
        closeTimer.start();
    }
}
