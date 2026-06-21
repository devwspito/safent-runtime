import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "."

Rectangle {
    id: nativeAppsLauncher
    anchors.fill: parent
    color: Tokens.bgSurface
    readonly property bool _compact: nativeAppsLauncher.width < Tokens.bpCompact * root.sf

    property string activeTab: "installed"
    property string searchQuery: ""
    property var storeResults: []
    property bool storeLoading: false
    property int totalAvailable: 0
    property var featuredApps: []
    property var installedPkgList: []
    property var busyPkgs: []
    property string busyPkgStatus: ""
    property var userInstalledApps: []

    // Cmd debe ser un único argv (sin || ni 2>/dev/null; Popen NO ejecuta shell;
    // los operadores acaban como argumentos literales y la app crashea). Apps
    // listadas aquí están horneadas y verificadas presentes en la imagen
    // (Containerfile dnf install + bake-time `which`).
    property var nativeApps: [
        { appId: "native-chromium", label: "Chromium", desc: "Web browser", cmd: "chromium-browser --ozone-platform=wayland --no-sandbox --password-store=basic", searchName: "Chromium", iconType: "chromium", accent: "#4285f4", pkg: "chromium", builtIn: true },
        { appId: "native-vscode", label: "VS Code", desc: "IDE oficial del SO", cmd: "code --ozone-platform=wayland --no-sandbox --password-store=basic", searchName: "Visual Studio Code", iconType: "editor", accent: "#0078d4", pkg: "code", builtIn: true },
        { appId: "native-text-editor", label: "Editor", desc: "Text editor", cmd: "gnome-text-editor", searchName: "Text Editor", iconType: "editor", accent: "#4ade80", pkg: "gnome-text-editor", builtIn: true },
        { appId: "native-calculator", label: "Calculator", desc: "Desktop calculator", cmd: "gnome-calculator", searchName: "Calculator", iconType: "calculator", accent: "#c084fc", pkg: "gnome-calculator", builtIn: true },
        { appId: "native-libreoffice-writer", label: "Writer", desc: "Word processor", cmd: "libreoffice --writer", searchName: "libreoffice", iconType: "office-writer", accent: "#2563eb", pkg: "libreoffice-writer", builtIn: true },
        { appId: "native-libreoffice-calc", label: "Calc", desc: "Spreadsheets", cmd: "libreoffice --calc", searchName: "libreoffice", iconType: "office-calc", accent: "#16a34a", pkg: "libreoffice-calc", builtIn: true },
        { appId: "native-libreoffice-impress", label: "Impress", desc: "Presentations", cmd: "libreoffice --impress", searchName: "libreoffice", iconType: "office-impress", accent: "#dc2626", pkg: "libreoffice-impress", builtIn: true },
        { appId: "native-evince", label: "PDF Viewer", desc: "Document viewer", cmd: "evince", searchName: "evince", iconType: "pdf", accent: "#ef4444", pkg: "evince", builtIn: true },
        { appId: "native-kgx", label: "Terminal", desc: "Terminal del SO", cmd: "qterminal", searchName: "qterminal", iconType: "editor", accent: "#0ea5e9", pkg: "kgx", builtIn: true }
    ]

    function getAllInstalledApps() {
        var all = [];
        for (var i = 0; i < nativeApps.length; i++) all.push(nativeApps[i]);
        for (var j = 0; j < userInstalledApps.length; j++) all.push(userInstalledApps[j]);
        return all;
    }

    Component.onCompleted: {
        refreshInstalled();
        loadFeatured();
        loadUserApps();
    }

    function launchNativeViaHelper(cmd, label, searchName) {
        // BUG WhaleOS (cadáver): esta función exportaba WAYLAND_DISPLAY=whaleos-0
        // + HOME=/home/ainux (entorno de la distro original, inexistente aquí)
        // → toda app lanzada desde Apps moría al instante buscando un display
        // fantasma, mientras Providers/Integraciones (root.launchNative) sí
        // abrían. Ahora TODA la UI lanza por la MISMA ruta: sysManager
        // .launchNativeApp (env Wayland real + pre-check de binario + stderr
        // a /tmp/lumen-app-*.log).
        root.launchNative(cmd, searchName || label);
        root.showToast(label + " abriéndose…", "info");
    }

    // ── Store del SO: TODO via verbos D-Bus del daemon (dnf + Flathub). ──
    // El cadáver WhaleOS usaba apt/dpkg de Ubuntu via shell — aquí no existen.
    // PackageStoreService (daemon) es la ÚNICA autoridad de paquetes; el
    // compositor solo pinta. helperExec queda únicamente para leer .desktop
    // locales (mismo filesystem, read-only).
    function helperExec(cmd, callback) {
        var out = sysManager.runCommandQuick(cmd);
        if (callback) callback(out || "");
    }

    function refreshInstalled() {
        hermes.call("pkg-installed-rpm", "list_installed_packages", JSON.stringify({ source: "rpm" }));
        hermes.call("pkg-installed-flatpak", "list_installed_packages", JSON.stringify({ source: "flatpak" }));
    }
    property var installedRpm: []
    property var installedFlatpak: []

    // Destacadas: apps REALES de Flathub (IDs canónicos) instalables al click.
    // Catálogo curado offline — la búsqueda sí consulta el catálogo vivo.
    function loadFeatured() {
        featuredApps = [
            { pkg: "org.gimp.GIMP",              source: "flatpak", desc: "Editor de imágenes" },
            { pkg: "org.videolan.VLC",           source: "flatpak", desc: "Reproductor multimedia" },
            { pkg: "org.inkscape.Inkscape",      source: "flatpak", desc: "Gráficos vectoriales" },
            { pkg: "org.blender.Blender",        source: "flatpak", desc: "3D y animación" },
            { pkg: "org.audacityteam.Audacity",  source: "flatpak", desc: "Editor de audio" },
            { pkg: "org.kde.krita",              source: "flatpak", desc: "Pintura digital" },
            { pkg: "org.mozilla.Thunderbird",    source: "flatpak", desc: "Correo electrónico" },
            { pkg: "org.keepassxc.KeePassXC",    source: "flatpak", desc: "Gestor de contraseñas" },
            { pkg: "com.obsproject.Studio",      source: "flatpak", desc: "Grabación y streaming" },
            { pkg: "org.gnome.Loupe",            source: "flatpak", desc: "Visor de imágenes" }
        ];
    }

    function searchStore(query) {
        if (!query || query.trim().length < 2) { storeResults = []; return; }
        storeLoading = true;
        hermes.call("pkg-search", "search_packages", JSON.stringify({ query: query.trim(), source: "all" }));
    }

    function isPkgInstalled(pkg) {
        for (var i = 0; i < installedPkgList.length; i++) {
            if (installedPkgList[i] === pkg || installedPkgList[i].indexOf(pkg + ":") === 0) return true;
        }
        return false;
    }
    function isPkgBusy(pkg) {
        for (var i = 0; i < busyPkgs.length; i++) { if (busyPkgs[i] === pkg) return true; }
        return false;
    }

    // Persistencia de apps añadidas por el usuario: $HOME real del compositor
    // (hermes-user), nunca /home/ainux (usuario WhaleOS inexistente).
    function saveUserApps() {
        var data = JSON.stringify(userInstalledApps);
        helperExec("bash -c \"echo '" + data.replace(/'/g, "'\\''") + "' > \\\"$HOME/.lumenso-user-apps.json\\\"\"", function() {});
    }
    function loadUserApps() {
        helperExec("bash -c 'cat \"$HOME/.lumenso-user-apps.json\" 2>/dev/null'", function(output) {
            if (output && output.trim().length > 2) {
                try { userInstalledApps = JSON.parse(output.trim()); } catch(e) {}
            }
        });
    }

    // Instalación async via daemon: install_package devuelve {op_id};
    // pkgOpTimer sondea get_pkg_op_status hasta done/error.
    property var pendingOps: ({})   // op_id → {pkg, source, desc, action}
    function installPkg(pkg, desc, source) {
        if (isPkgBusy(pkg)) return;   // re-entrada: bloquea doble-click durante scan/modal
        var src = source || "flatpak";
        var b = busyPkgs.slice(); b.push(pkg); busyPkgs = b;
        busyPkgStatus = "Centro de Seguridad: analizando " + pkg + "…";
        // Gate antivirus de SISTEMA: escanea (CVE/procedencia) antes de instalar;
        // el score se muestra y sólo si el usuario confirma se instala el paquete.
        root.beginGatedInstall(
            { kind: "package", identifier: pkg, source_url: src },
            "install_package",
            "pkg-install:" + src + ":" + pkg,
            { source: src, package_id: pkg }
        );
    }

    // El gate terminó SIN instalar (cancelar/bloqueado/error) → liberar busy del
    // paquete (paridad con McpApp). reqId = "pkg-install:" + src + ":" + pkg.
    Connections {
        target: root
        function onInstallResolved(reqId) {
            if (reqId.indexOf("pkg-install:") !== 0) return;
            var parts = reqId.split(":");
            var pkg = parts.slice(2).join(":");   // el pkg puede contener ':'
            nativeAppsLauncher._clearBusy(pkg);
        }
    }

    function addUserApp(pkg, label, desc, cmd) {
        for (var i = 0; i < nativeApps.length; i++) { if (nativeApps[i].pkg === pkg) return; }
        for (var j = 0; j < userInstalledApps.length; j++) { if (userInstalledApps[j].pkg === pkg) return; }
        var apps = userInstalledApps.slice();
        apps.push({
            appId: "native-" + pkg, label: label, desc: desc, cmd: cmd,
            searchName: pkg, iconType: "generic", accent: accentFor(pkg), pkg: pkg, builtIn: false
        });
        userInstalledApps = apps;
        saveUserApps();
    }

    // Tras instalar un flatpak el lanzador es estándar; para rpm, lee el
    // .desktop local (mismo filesystem) para sacar Exec/Name reales.
    function registerInstalledApp(pkg, source, desc) {
        if (source === "flatpak") {
            addUserApp(pkg, pkg.split(".").pop(), desc || pkg, "flatpak run " + pkg);
            return;
        }
        helperExec("bash -c \"ls /usr/share/applications/*" + pkg + "*.desktop 2>/dev/null | head -1\"", function(desktop) {
            var launchCmd = pkg, appLabel = pkg;
            if (desktop && desktop.trim().length > 0) {
                helperExec("bash -c \"grep -E '^(Exec|Name)=' '" + desktop.trim() + "' | head -4\"", function(deskData) {
                    if (deskData) {
                        var lines = deskData.trim().split("\n");
                        for (var k = 0; k < lines.length; k++) {
                            if (lines[k].indexOf("Exec=") === 0) launchCmd = lines[k].substring(5).replace(/ %[fFuUdDnNickvm]/g, "").trim();
                            if (lines[k].indexOf("Name=") === 0) appLabel = lines[k].substring(5).trim();
                        }
                    }
                    addUserApp(pkg, appLabel, desc || pkg, launchCmd);
                });
            } else {
                addUserApp(pkg, appLabel, desc || pkg, launchCmd);
            }
        });
    }

    function removePkg(pkg, source) {
        var src = source || "flatpak";
        var b = busyPkgs.slice(); b.push(pkg); busyPkgs = b;
        busyPkgStatus = "Quitando " + pkg + "…";
        root.showToast("Quitando " + pkg + "…", "info");
        hermes.call("pkg-uninstall:" + src + ":" + pkg, "uninstall_package",
                    JSON.stringify({ source: src, package_id: pkg }));
    }

    function _clearBusy(pkg) {
        var nb = []; for (var i = 0; i < busyPkgs.length; i++) { if (busyPkgs[i] !== pkg) nb.push(busyPkgs[i]); }
        busyPkgs = nb;
        if (nb.length === 0) busyPkgStatus = "";
    }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "pkg-installed-rpm" || reqId === "pkg-installed-flatpak") {
                var lst = [];
                try { lst = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) {}
                var names = [];
                for (var i = 0; i < lst.length; i++) names.push(lst[i].id || lst[i].package_id || lst[i].name || "");
                if (reqId === "pkg-installed-rpm") nativeAppsLauncher.installedRpm = names;
                else nativeAppsLauncher.installedFlatpak = names;
                nativeAppsLauncher.installedPkgList = nativeAppsLauncher.installedRpm.concat(nativeAppsLauncher.installedFlatpak);
                return;
            }
            if (reqId === "pkg-search") {
                nativeAppsLauncher.storeLoading = false;
                var res = [];
                try { res = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) {}
                var out = [];
                for (var j = 0; j < res.length; j++) {
                    out.push({ pkg: res[j].id || res[j].package_id || res[j].name || "",
                               source: res[j].source || "flatpak",
                               desc: res[j].summary || res[j].description || "" });
                }
                nativeAppsLauncher.storeResults = out;
                return;
            }
            if (reqId.indexOf("pkg-install:") === 0 || reqId.indexOf("pkg-uninstall:") === 0) {
                var parts = reqId.split(":");
                var action = parts[0] === "pkg-install" ? "install" : "uninstall";
                var src2 = parts[1], pkg2 = parts.slice(2).join(":");
                var r = {}; try { r = JSON.parse(jsonStr || "{}"); } catch (e) {}
                if (!ok || r.error || !r.op_id) {
                    nativeAppsLauncher._clearBusy(pkg2);
                    root.showToast("✕ " + (r.error || "no se pudo iniciar"), "error");
                    return;
                }
                var ops = {}; for (var k in nativeAppsLauncher.pendingOps) ops[k] = nativeAppsLauncher.pendingOps[k];
                ops[r.op_id] = { pkg: pkg2, source: src2, action: action };
                nativeAppsLauncher.pendingOps = ops;
                pkgOpTimer.start();
                return;
            }
            if (reqId.indexOf("pkg-op:") === 0) {
                var opId = reqId.substring(7);
                var meta = nativeAppsLauncher.pendingOps[opId];
                if (!meta) return;
                var st = {}; try { st = JSON.parse(jsonStr || "{}"); } catch (e) {}
                var status = st.status || "";
                if (status === "done" || status === "success" || status === "completed") {
                    var ops2 = {}; for (var k2 in nativeAppsLauncher.pendingOps) { if (k2 !== opId) ops2[k2] = nativeAppsLauncher.pendingOps[k2]; }
                    nativeAppsLauncher.pendingOps = ops2;
                    nativeAppsLauncher._clearBusy(meta.pkg);
                    nativeAppsLauncher.refreshInstalled();
                    if (meta.action === "install") {
                        root.showToast(meta.pkg + " instalado", "success");
                        nativeAppsLauncher.registerInstalledApp(meta.pkg, meta.source, "");
                    } else {
                        root.showToast(meta.pkg + " eliminado", "success");
                        var apps2 = [];
                        for (var m = 0; m < nativeAppsLauncher.userInstalledApps.length; m++) {
                            if (nativeAppsLauncher.userInstalledApps[m].pkg !== meta.pkg) apps2.push(nativeAppsLauncher.userInstalledApps[m]);
                        }
                        nativeAppsLauncher.userInstalledApps = apps2;
                        nativeAppsLauncher.saveUserApps();
                    }
                } else if (status === "error" || status === "failed") {
                    var ops3 = {}; for (var k3 in nativeAppsLauncher.pendingOps) { if (k3 !== opId) ops3[k3] = nativeAppsLauncher.pendingOps[k3]; }
                    nativeAppsLauncher.pendingOps = ops3;
                    nativeAppsLauncher._clearBusy(meta.pkg);
                    root.showToast("✕ " + meta.pkg + ": " + (st.error_message || "falló"), "error");
                }
                // pending/running → seguirá sondeando
            }
        }
    }

    function accentFor(pkg) {
        var c = ["#60a5fa","#4ade80","#c084fc","#f97316","#ef4444","#22c55e","#8b5cf6","#06b6d4","#eab308","#ec4899","#14b8a6","#a855f7","#0ea5e9"];
        var h = 0;
        for (var i = 0; i < pkg.length; i++) h = ((h << 5) - h) + pkg.charCodeAt(i);
        return c[Math.abs(h) % c.length];
    }

    Timer { id: searchDebounce; interval: 400; onTriggered: searchStore(searchQuery) }

    // ── Sondeo de operaciones async del daemon (install/uninstall) ──
    Timer {
        id: pkgOpTimer; interval: 2000; repeat: true
        running: busyPkgs.length > 0
        onTriggered: {
            var any = false;
            for (var opId in pendingOps) {
                any = true;
                hermes.call("pkg-op:" + opId, "get_pkg_op_status", JSON.stringify({ op_id: opId }));
            }
            if (!any) stop();
        }
    }

    Column {
        anchors.fill: parent
        spacing: 0

        // ── Tab bar ──
        Rectangle {
            width: parent.width
            height: Math.round(44 * root.sf)
            color: Tokens.bgElevated
            border.color: Tokens.borderSubtle
            // Only draw the bottom border
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 1
                color: Tokens.borderSubtle
            }

            Row {
                anchors.verticalCenter: parent.verticalCenter
                anchors.left: parent.left
                anchors.leftMargin: Math.round(Tokens.spMd * root.sf)
                spacing: Math.round(Tokens.spXs * root.sf)

                Repeater {
                    model: [
                        { id: "installed", label: "Instaladas (" + getAllInstalledApps().length + ")" },
                        { id: "store",     label: "Tienda" }
                    ]
                    Rectangle {
                        width: tabLbl.width + Math.round(Tokens.spXl * root.sf)
                        height: Math.round(30 * root.sf)
                        radius: Math.round(Tokens.radiusSm * root.sf)
                        color: activeTab === modelData.id ? Tokens.accentSubtle : tabMa.containsMouse ? Tokens.bgCard : "transparent"
                        border.color: activeTab === modelData.id ? Tokens.accentBase : "transparent"
                        border.width: 1

                        Behavior on color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }

                        Text {
                            id: tabLbl
                            anchors.centerIn: parent
                            text: modelData.label
                            font.pixelSize: Math.round(11 * root.sf)
                            font.family: Tokens.fontBody
                            font.weight: activeTab === modelData.id ? Font.DemiBold : Font.Normal
                            color: activeTab === modelData.id ? Tokens.accentBase : Tokens.textSecondary
                        }
                        MouseArea {
                            id: tabMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: activeTab = modelData.id
                        }
                    }
                }

                // Package count badge
                LumenBadge {
                    visible: totalAvailable > 0
                    sf: root.sf
                    text: totalAvailable.toLocaleString() + " pkgs"
                    tone: "neutral"
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
        }

        // ── Install progress banner ──
        Rectangle {
            visible: busyPkgs.length > 0
            width: parent.width
            height: Math.round(30 * root.sf)
            color: Tokens.bgElevated
            border.color: Tokens.borderSubtle

            Row {
                anchors.fill: parent
                anchors.leftMargin: Math.round(Tokens.spMd * root.sf)
                anchors.rightMargin: Math.round(Tokens.spMd * root.sf)
                spacing: Math.round(Tokens.spSm * root.sf)
                anchors.verticalCenter: parent.verticalCenter

                // Spinner ring
                Rectangle {
                    width: Math.round(14 * root.sf)
                    height: Math.round(14 * root.sf)
                    radius: width / 2
                    color: "transparent"
                    border.color: Tokens.accentBase
                    border.width: Math.round(2 * root.sf)
                    anchors.verticalCenter: parent.verticalCenter
                    RotationAnimation on rotation {
                        from: 0; to: 360; duration: 900
                        loops: Animation.Infinite
                        running: busyPkgs.length > 0 && !Tokens.reduceMotion
                    }
                }

                Text {
                    text: busyPkgStatus || ("Trabajando en " + (busyPkgs.length > 0 ? busyPkgs[0] : "") + "…")
                    font.pixelSize: Math.round(11 * root.sf)
                    font.family: Tokens.fontBody
                    color: Tokens.accentBase
                    anchors.verticalCenter: parent.verticalCenter
                }
            }

            // Indeterminate progress stripe (amber, Sereno-branded)
            Rectangle {
                id: progressBar
                anchors.bottom: parent.bottom
                height: Math.round(2 * root.sf)
                color: Tokens.accentBase
                radius: height / 2
                SequentialAnimation on width {
                    running: busyPkgs.length > 0
                    loops: Animation.Infinite
                    NumberAnimation { from: 0; to: nativeAppsLauncher.width; duration: 2200; easing.type: Easing.InOutCubic }
                    NumberAnimation { from: nativeAppsLauncher.width; to: 0; duration: 2200; easing.type: Easing.InOutCubic }
                }
            }
        }

        // ═══════ INSTALLED TAB ═══════
        Flickable {
            visible: activeTab === "installed"
            width: parent.width
            height: parent.height - Math.round(44 * root.sf) - (busyPkgs.length > 0 ? Math.round(30 * root.sf) : 0)
            contentHeight: installedGrid.height + Math.round(Tokens.spXxl * root.sf)
            clip: true
            ScrollBar.vertical: LumenScrollBar { sf: root.sf; policy: ScrollBar.AsNeeded }

            GridLayout {
                id: installedGrid
                x: Math.round(Tokens.spXl * root.sf)
                y: Math.round(Tokens.spLg * root.sf)
                width: parent.width - Math.round(Tokens.spXxl * root.sf) * 2
                columns: Math.max(1, Math.floor(width / Math.round(150 * root.sf)))
                columnSpacing: Math.round(Tokens.spMd * root.sf)
                rowSpacing: Math.round(Tokens.spMd * root.sf)

                Repeater {
                    model: getAllInstalledApps()
                    delegate: Rectangle {
                        Layout.fillWidth: true
                        Layout.minimumWidth: Math.round(130 * root.sf)
                        height: Math.round(130 * root.sf)
                        radius: Math.round(Tokens.radiusMd * root.sf)
                        color: iMa.containsMouse ? Tokens.bgElevated : Tokens.bgCard
                        border.color: iMa.containsMouse ? Tokens.accentBase : Tokens.borderSubtle
                        border.width: 1

                        Behavior on color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }
                        Behavior on border.color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }

                        // Scale spring on hover
                        scale: iMa.containsMouse ? 1.03 : 1.0
                        Behavior on scale {
                            enabled: !Tokens.reduceMotion
                            NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutBack; easing.overshoot: Tokens.springOvershoot }
                        }

                        // Main click area — z:0 (bottom)
                        MouseArea {
                            id: iMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            z: 0
                            onClicked: {
                                if (modelData.cmd && modelData.cmd.length > 0) {
                                    nativeAppsLauncher.launchNativeViaHelper(modelData.cmd, modelData.label, modelData.searchName);
                                } else {
                                    root.openAppWindow(modelData.appId, modelData.label, modelData.iconType || "generic", modelData.searchName || modelData.pkg, modelData.cmd);
                                }
                            }
                        }

                        Column {
                            anchors.centerIn: parent
                            spacing: Math.round(Tokens.spXs * root.sf)
                            width: parent.width - Math.round(Tokens.spLg * root.sf)
                            z: 1

                            Rectangle {
                                width: Math.round(42 * root.sf)
                                height: Math.round(42 * root.sf)
                                radius: Math.round(Tokens.radiusSm * root.sf)
                                anchors.horizontalCenter: parent.horizontalCenter
                                color: Tokens.bgSunken
                                border.color: modelData.accent
                                border.width: 1
                                Canvas {
                                    anchors.fill: parent
                                    anchors.margins: Math.round(6 * root.sf)
                                    property string t: modelData.iconType
                                    property string a: modelData.accent
                                    property string lbl: modelData.label
                                    Component.onCompleted: requestPaint()
                                    onPaint: {
                                        var ctx = getContext("2d");
                                        ctx.clearRect(0, 0, width, height);
                                        ctx.save();
                                        var sc = width / 22;
                                        ctx.scale(sc, sc);
                                        nativeAppsLauncher.drawIcon(ctx, t, a, lbl);
                                        ctx.restore();
                                    }
                                }
                            }

                            Text {
                                anchors.horizontalCenter: parent.horizontalCenter
                                text: modelData.label
                                font.pixelSize: Math.round(11 * root.sf)
                                font.family: Tokens.fontBody
                                font.weight: Font.DemiBold
                                color: Tokens.textPrimary
                                elide: Text.ElideRight
                                width: parent.width
                                horizontalAlignment: Text.AlignHCenter
                            }
                            Text {
                                anchors.horizontalCenter: parent.horizontalCenter
                                text: modelData.desc
                                font.pixelSize: Math.round(9 * root.sf)
                                font.family: Tokens.fontBody
                                color: Tokens.textMuted
                                elide: Text.ElideRight
                                width: parent.width
                                horizontalAlignment: Text.AlignHCenter
                            }

                            // Uninstall button (only for user-installed, not built-in)
                            Rectangle {
                                visible: !modelData.builtIn && iMa.containsMouse
                                anchors.horizontalCenter: parent.horizontalCenter
                                width: Math.round(72 * root.sf)
                                height: Math.round(22 * root.sf)
                                radius: Math.round(Tokens.radiusSm * root.sf)
                                color: unMa.containsMouse ? Tokens.dangerSubtle : Tokens.bgSunken
                                border.color: Tokens.dangerBase
                                border.width: 1
                                z: 10
                                Text {
                                    anchors.centerIn: parent
                                    text: "Quitar"
                                    font.pixelSize: Math.round(9 * root.sf)
                                    font.family: Tokens.fontBody
                                    font.weight: Font.DemiBold
                                    color: Tokens.dangerBase
                                }
                                MouseArea {
                                    id: unMa
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: removePkg(modelData.pkg, modelData.source)
                                }
                            }
                        }
                    }
                }
            }
        }

        // ═══════ STORE TAB ═══════
        Item {
            visible: activeTab === "store"
            width: parent.width
            height: parent.height - Math.round(44 * root.sf) - (busyPkgs.length > 0 ? Math.round(30 * root.sf) : 0)

            Column {
                anchors.fill: parent
                spacing: 0

                // Search bar
                Rectangle {
                    width: parent.width
                    height: Math.round(52 * root.sf)
                    color: Tokens.bgElevated

                    Row {
                        anchors.fill: parent
                        anchors.margins: Math.round(Tokens.spSm * root.sf)
                        spacing: Math.round(Tokens.spSm * root.sf)

                        LumenInput {
                            id: storeInput
                            sf: root.sf
                            width: parent.width
                                   - (srcBadge.visible ? srcBadge.width + Math.round(Tokens.spSm * root.sf) : 0)
                            height: Math.round(34 * root.sf)
                            anchors.verticalCenter: parent.verticalCenter
                            placeholder: "Busca apps (Flathub + Fedora)…"
                            onTextChanged: { searchQuery = text; searchDebounce.restart(); }
                            onAccepted: searchStore(text)
                        }

                        Rectangle {
                            id: srcBadge
                            visible: !nativeAppsLauncher._compact
                            width: Math.round(116 * root.sf)
                            height: Math.round(34 * root.sf)
                            radius: Math.round(Tokens.radiusSm * root.sf)
                            color: Tokens.accentGhost
                            border.color: Tokens.accentSubtle
                            border.width: 1
                            anchors.verticalCenter: parent.verticalCenter
                            Text {
                                anchors.centerIn: parent
                                text: "Fedora + Flathub"
                                font.pixelSize: Math.round(10 * root.sf)
                                font.family: Tokens.fontBody
                                font.weight: Font.DemiBold
                                color: Tokens.accentBase
                            }
                        }
                    }

                    Rectangle {
                        anchors.bottom: parent.bottom
                        width: parent.width
                        height: 1
                        color: Tokens.borderSubtle
                    }
                }

                Flickable {
                    width: parent.width
                    height: parent.height - Math.round(52 * root.sf)
                    contentHeight: resultsCol.height + Math.round(Tokens.spXl * root.sf)
                    clip: true
                    ScrollBar.vertical: LumenScrollBar { sf: root.sf; policy: ScrollBar.AsNeeded }

                    Column {
                        id: resultsCol
                        width: parent.width
                        leftPadding: Math.round(Tokens.spMd * root.sf)
                        rightPadding: Math.round(Tokens.spMd * root.sf)
                        topPadding: Math.round(Tokens.spSm * root.sf)
                        spacing: Math.round(Tokens.spXs * root.sf)

                        Text {
                            visible: storeLoading
                            text: "Buscando…"
                            font.pixelSize: Math.round(12 * root.sf)
                            font.family: Tokens.fontBody
                            color: Tokens.textMuted
                            topPadding: Math.round(Tokens.spXl * root.sf)
                        }

                        Text {
                            visible: !storeLoading && searchQuery.length >= 2 && storeResults.length > 0
                            text: storeResults.length + " resultados para \"" + searchQuery + "\""
                            font.pixelSize: Math.round(11 * root.sf)
                            font.family: Tokens.fontBody
                            font.weight: Font.DemiBold
                            color: Tokens.textMuted
                            bottomPadding: Math.round(Tokens.spXs * root.sf)
                        }

                        Text {
                            visible: !storeLoading && searchQuery.length >= 2 && storeResults.length === 0
                            text: "Sin resultados para \"" + searchQuery + "\""
                            font.pixelSize: Math.round(12 * root.sf)
                            font.family: Tokens.fontBody
                            color: Tokens.textMuted
                            topPadding: Math.round(Tokens.spXxl * root.sf)
                        }

                        // Search results
                        Repeater {
                            model: searchQuery.length >= 2 ? storeResults : []
                            delegate: Rectangle {
                                width: resultsCol.width - Math.round(Tokens.spXxl * root.sf)
                                height: Math.round(64 * root.sf)
                                radius: Math.round(Tokens.radiusSm * root.sf)
                                color: srMa.containsMouse ? Tokens.bgElevated : Tokens.bgCard
                                border.color: srMa.containsMouse ? Tokens.borderStrong : Tokens.borderSubtle
                                border.width: 1

                                Behavior on color {
                                    enabled: !Tokens.reduceMotion
                                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                }

                                MouseArea { id: srMa; anchors.fill: parent; hoverEnabled: true }

                                Row {
                                    anchors.fill: parent
                                    anchors.margins: Math.round(Tokens.spSm * root.sf)
                                    spacing: Math.round(Tokens.spSm * root.sf)

                                    Rectangle {
                                        width: Math.round(44 * root.sf)
                                        height: Math.round(44 * root.sf)
                                        radius: Math.round(Tokens.radiusSm * root.sf)
                                        color: Tokens.bgSunken
                                        border.color: accentFor(modelData.pkg)
                                        border.width: 1
                                        anchors.verticalCenter: parent.verticalCenter
                                        Text {
                                            anchors.centerIn: parent
                                            text: modelData.pkg.charAt(0).toUpperCase()
                                            font.pixelSize: Math.round(18 * root.sf)
                                            font.family: Tokens.fontDisplay
                                            font.weight: Font.Bold
                                            color: accentFor(modelData.pkg)
                                        }
                                    }

                                    Column {
                                        width: parent.width - Math.round(150 * root.sf)
                                        spacing: Math.round(Tokens.spXs * root.sf)
                                        anchors.verticalCenter: parent.verticalCenter
                                        Text {
                                            text: modelData.pkg
                                            font.pixelSize: Math.round(12 * root.sf)
                                            font.family: Tokens.fontBody
                                            font.weight: Font.DemiBold
                                            color: Tokens.textPrimary
                                            elide: Text.ElideRight
                                            width: parent.width
                                        }
                                        Text {
                                            text: modelData.desc
                                            font.pixelSize: Math.round(9 * root.sf)
                                            font.family: Tokens.fontBody
                                            color: Tokens.textMuted
                                            elide: Text.ElideRight
                                            width: parent.width
                                            wrapMode: Text.WordWrap
                                            maximumLineCount: 2
                                        }
                                    }

                                    // Install/Remove button
                                    LumenButton {
                                        sf: root.sf
                                        label: isPkgBusy(modelData.pkg) ? "…"
                                             : isPkgInstalled(modelData.pkg) ? (ibMa.containsMouse ? "Quitar" : "Instalada")
                                             : "Instalar"
                                        variant: isPkgInstalled(modelData.pkg) ? (ibMa.containsMouse ? "danger" : "secondary") : "primary"
                                        enabled: !isPkgBusy(modelData.pkg)
                                        implicitWidth: Math.round(88 * root.sf)
                                        implicitHeight: Math.round(30 * root.sf)
                                        anchors.verticalCenter: parent.verticalCenter
                                        // MouseArea for hover detection on the button label swap
                                        MouseArea {
                                            id: ibMa
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                if (isPkgBusy(modelData.pkg)) return;
                                                if (isPkgInstalled(modelData.pkg)) removePkg(modelData.pkg, modelData.source);
                                                else installPkg(modelData.pkg, modelData.desc, modelData.source);
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // ═══ Browse / Featured ═══
                        Column {
                            visible: searchQuery.length < 2 && !storeLoading
                            width: resultsCol.width - Math.round(Tokens.spXxl * root.sf)
                            spacing: Math.round(Tokens.spSm * root.sf)

                            // Hero banner
                            Rectangle {
                                width: parent.width
                                height: Math.round(72 * root.sf)
                                radius: Math.round(Tokens.radiusMd * root.sf)
                                color: Tokens.accentGhost
                                border.color: Tokens.accentSubtle
                                border.width: 1
                                Column {
                                    anchors.centerIn: parent
                                    spacing: Math.round(Tokens.spXs * root.sf)
                                    Text {
                                        anchors.horizontalCenter: parent.horizontalCenter
                                        text: "App Store del SO"
                                        font.pixelSize: Math.round(15 * root.sf)
                                        font.family: Tokens.fontDisplay
                                        font.weight: Font.Bold
                                        color: Tokens.textPrimary
                                    }
                                    Text {
                                        anchors.horizontalCenter: parent.horizontalCenter
                                        text: "Apps de Flathub y paquetes oficiales de Fedora"
                                        font.pixelSize: Math.round(10 * root.sf)
                                        font.family: Tokens.fontBody
                                        color: Tokens.textMuted
                                    }
                                }
                            }

                            // Quick-search chips
                            Text {
                                text: "BÚSQUEDA RÁPIDA"
                                font.pixelSize: Math.round(9 * root.sf)
                                font.family: Tokens.fontBody
                                font.weight: Font.Bold
                                font.letterSpacing: 0.6
                                color: Tokens.textDisabled
                                topPadding: Math.round(Tokens.spXs * root.sf)
                            }
                            Flow {
                                width: parent.width
                                spacing: Math.round(Tokens.spXs * root.sf)
                                Repeater {
                                    model: ["browser", "editor", "image editor", "video player", "music", "office", "game", "terminal", "file manager", "email", "chat", "pdf", "screenshot", "disk", "archive", "network"]
                                    delegate: Rectangle {
                                        width: cLbl.width + Math.round(Tokens.spMd * root.sf)
                                        height: Math.round(24 * root.sf)
                                        radius: Tokens.radiusPill
                                        color: cMa.containsMouse ? Tokens.accentSubtle : Tokens.bgCard
                                        border.color: cMa.containsMouse ? Tokens.accentBase : Tokens.borderDefault
                                        border.width: 1

                                        Behavior on color {
                                            enabled: !Tokens.reduceMotion
                                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                        }

                                        Text {
                                            id: cLbl
                                            anchors.centerIn: parent
                                            text: modelData
                                            font.pixelSize: Math.round(10 * root.sf)
                                            font.family: Tokens.fontBody
                                            color: cMa.containsMouse ? Tokens.accentBase : Tokens.textSecondary
                                        }
                                        MouseArea {
                                            id: cMa
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                storeInput.text = modelData;
                                                searchQuery = modelData;
                                                searchStore(modelData);
                                            }
                                        }
                                    }
                                }
                            }

                            // Featured packages
                            Text {
                                visible: featuredApps.length > 0
                                text: "POPULARES"
                                font.pixelSize: Math.round(9 * root.sf)
                                font.family: Tokens.fontBody
                                font.weight: Font.Bold
                                font.letterSpacing: 0.6
                                color: Tokens.textDisabled
                                topPadding: Math.round(Tokens.spXs * root.sf)
                            }
                            Repeater {
                                model: featuredApps
                                delegate: Rectangle {
                                    width: parent.width
                                    height: Math.round(56 * root.sf)
                                    radius: Math.round(Tokens.radiusSm * root.sf)
                                    color: fMa.containsMouse ? Tokens.bgElevated : Tokens.bgCard
                                    border.color: fMa.containsMouse ? Tokens.borderStrong : Tokens.borderSubtle
                                    border.width: 1

                                    Behavior on color {
                                        enabled: !Tokens.reduceMotion
                                        ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                    }

                                    MouseArea { id: fMa; anchors.fill: parent; hoverEnabled: true }

                                    Row {
                                        anchors.fill: parent
                                        anchors.margins: Math.round(Tokens.spSm * root.sf)
                                        spacing: Math.round(Tokens.spSm * root.sf)

                                        Rectangle {
                                            width: Math.round(36 * root.sf)
                                            height: Math.round(36 * root.sf)
                                            radius: Math.round(Tokens.radiusSm * root.sf)
                                            color: Tokens.bgSunken
                                            border.color: accentFor(modelData.pkg)
                                            border.width: 1
                                            anchors.verticalCenter: parent.verticalCenter
                                            Text {
                                                anchors.centerIn: parent
                                                text: modelData.pkg.charAt(0).toUpperCase()
                                                font.pixelSize: Math.round(15 * root.sf)
                                                font.family: Tokens.fontDisplay
                                                font.weight: Font.Bold
                                                color: accentFor(modelData.pkg)
                                            }
                                        }

                                        Column {
                                            width: parent.width - Math.round(140 * root.sf)
                                            spacing: Math.round(Tokens.spXs * root.sf)
                                            anchors.verticalCenter: parent.verticalCenter
                                            Text {
                                                text: modelData.pkg
                                                font.pixelSize: Math.round(11 * root.sf)
                                                font.family: Tokens.fontBody
                                                font.weight: Font.DemiBold
                                                color: Tokens.textPrimary
                                                elide: Text.ElideRight
                                                width: parent.width
                                            }
                                            Text {
                                                text: modelData.desc
                                                font.pixelSize: Math.round(9 * root.sf)
                                                font.family: Tokens.fontBody
                                                color: Tokens.textMuted
                                                elide: Text.ElideRight
                                                width: parent.width
                                                wrapMode: Text.WordWrap
                                                maximumLineCount: 2
                                            }
                                        }

                                        LumenButton {
                                            sf: root.sf
                                            label: isPkgBusy(modelData.pkg) ? "…"
                                                 : isPkgInstalled(modelData.pkg) ? "Instalada"
                                                 : "Instalar"
                                            variant: isPkgInstalled(modelData.pkg) ? "secondary" : "primary"
                                            enabled: !isPkgBusy(modelData.pkg)
                                            implicitWidth: Math.round(80 * root.sf)
                                            implicitHeight: Math.round(28 * root.sf)
                                            anchors.verticalCenter: parent.verticalCenter
                                            onClicked: {
                                                if (isPkgBusy(modelData.pkg)) return;
                                                if (isPkgInstalled(modelData.pkg)) removePkg(modelData.pkg, modelData.source);
                                                else installPkg(modelData.pkg, modelData.desc, modelData.source);
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

    function drawIcon(ctx, iconType, accent, label) {
        ctx.fillStyle = accent; ctx.strokeStyle = accent;
        ctx.lineWidth = 1.5; ctx.lineCap = "round"; ctx.lineJoin = "round";
        if (iconType === "chromium") {
            // Chromium icon — circle with colored segments
            var cx=11,cy=11,r=9;
            ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.fillStyle="#4285f4";ctx.fill();
            ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,r,-Math.PI*0.5,Math.PI/6);ctx.fillStyle="#ea4335";ctx.fill();
            ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,r,Math.PI/6,Math.PI*5/6);ctx.fillStyle="#fbbc05";ctx.fill();
            ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,r,Math.PI*5/6,Math.PI*3/2);ctx.fillStyle="#34a853";ctx.fill();
            ctx.beginPath();ctx.arc(cx,cy,4.5,0,Math.PI*2);ctx.fillStyle="#fff";ctx.fill();
            ctx.beginPath();ctx.arc(cx,cy,3,0,Math.PI*2);ctx.fillStyle="#4285f4";ctx.fill();
        } else if (iconType === "firefox") {
            // Firefox icon — globe with flame
            var cx=11,cy=11,r=9;
            // Globe base (blue)
            ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.fillStyle="#1a0a3e";ctx.fill();
            // Flame wrap (orange arc)
            ctx.beginPath();ctx.arc(cx,cy,r,-Math.PI*0.8,Math.PI*0.9);ctx.lineWidth=3.5;ctx.strokeStyle="#ff6611";ctx.stroke();
            // Flame tail (orange-yellow)
            ctx.beginPath();ctx.moveTo(17,5);ctx.quadraticCurveTo(19,9,16,14);ctx.quadraticCurveTo(13,18,8,17);ctx.lineWidth=2.5;ctx.strokeStyle="#ff9500";ctx.stroke();
            // Inner glow
            ctx.beginPath();ctx.arc(cx,cy,4.5,0,Math.PI*2);ctx.fillStyle="#3a1578";ctx.fill();
            ctx.beginPath();ctx.arc(cx,cy,3,0,Math.PI*2);ctx.fillStyle="#5b2d9e";ctx.fill();
        } else if (iconType === "editor") {
            ctx.fillStyle="#23242E";ctx.fillRect(4,2,14,18);ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.strokeRect(4,2,14,18);
            ctx.fillStyle=accent;ctx.fillRect(7,6,8,1);ctx.fillRect(7,9,6,1);ctx.fillRect(7,12,9,1);ctx.fillRect(7,15,5,1);
        } else if (iconType === "calculator") {
            ctx.fillStyle="#1e1b4b";ctx.fillRect(4,2,14,18);ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.strokeRect(4,2,14,18);
            ctx.fillStyle="#312e81";ctx.fillRect(6,4,10,4);ctx.fillStyle=accent;
            for(var rr=0;rr<3;rr++) for(var cc=0;cc<3;cc++) ctx.fillRect(6+cc*4,10+rr*3,3,2);
        } else if (iconType === "office-writer") {
            ctx.fillStyle="#1e3a8a";ctx.fillRect(3,1,16,20);ctx.strokeStyle="#2563eb";ctx.lineWidth=1;ctx.strokeRect(3,1,16,20);
            ctx.fillStyle="#93c5fd";ctx.fillRect(6,5,10,1);ctx.fillRect(6,8,8,1);ctx.fillRect(6,11,10,1);ctx.fillRect(6,14,6,1);ctx.fillRect(6,17,9,1);
        } else if (iconType === "office-calc") {
            ctx.fillStyle="#14532d";ctx.fillRect(3,1,16,20);ctx.strokeStyle="#16a34a";ctx.lineWidth=1;ctx.strokeRect(3,1,16,20);
            ctx.strokeStyle="#4ade80";ctx.lineWidth=0.5;
            for(var gi=0;gi<4;gi++){ctx.beginPath();ctx.moveTo(3,5+gi*4);ctx.lineTo(19,5+gi*4);ctx.stroke();}
            for(var gj=0;gj<3;gj++){ctx.beginPath();ctx.moveTo(8+gj*4,1);ctx.lineTo(8+gj*4,21);ctx.stroke();}
        } else if (iconType === "office-impress") {
            ctx.fillStyle="#7f1d1d";ctx.fillRect(3,1,16,20);ctx.strokeStyle="#dc2626";ctx.lineWidth=1;ctx.strokeRect(3,1,16,20);
            ctx.fillStyle="#991b1b";ctx.fillRect(5,4,12,8);
            ctx.fillStyle="#fca5a5";ctx.beginPath();ctx.moveTo(8,7);ctx.lineTo(14,8);ctx.lineTo(8,11);ctx.closePath();ctx.fill();
            ctx.fillStyle="#fca5a5";ctx.fillRect(5,15,3,2);ctx.fillRect(9,15,3,2);ctx.fillRect(13,15,3,2);
        } else if (iconType === "pdf") {
            ctx.fillStyle="#7f1d1d";ctx.fillRect(3,1,16,20);ctx.strokeStyle="#ef4444";ctx.lineWidth=1;ctx.strokeRect(3,1,16,20);
            ctx.fillStyle="#fca5a5";ctx.font="bold 7px sans-serif";ctx.textAlign="center";ctx.fillText("PDF",11,13);
        } else {
            ctx.fillStyle="#1a1a2e";ctx.fillRect(3,1,16,20);ctx.strokeStyle=accent;ctx.lineWidth=1;ctx.strokeRect(3,1,16,20);
            ctx.fillStyle=accent;ctx.font="bold 9px sans-serif";ctx.textAlign="center";
            var ch = (label && label.length > 0) ? label.charAt(0).toUpperCase() : "?";
            ctx.fillText(ch, 11, 14);
        }
    }
}
