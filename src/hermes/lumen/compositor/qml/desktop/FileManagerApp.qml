import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "."

// FileManagerApp — Sereno reskin (2026-06-14)
// Workspace root: /var/lib/hermes/workspace
// All logic (navigateTo, refreshFiles, goUp, xdg-open, sysManager calls,
// copy/cut/paste/rename/delete/new-folder) preserved verbatim.
// Only visuals ported to Tokens + Lumen components.
Rectangle {
    id: fileManager
    anchors.fill: parent
    color: "transparent"
    // Responsive: hide Size/Modified columns when the window is narrow
    readonly property bool _compact: fileManager.width < Tokens.bpCompact * root.sf

    readonly property string workspacePath: "/var/lib/hermes/workspace"
    property string currentPath: workspacePath
    property var fileList: []
    property string selectedFile: ""
    property bool showNewFolderDialog: false
    property string newFolderName: ""
    property bool showRenameDialog: false
    property string renameName: ""
    property string clipboardPath: ""
    property bool clipboardIsCut: false

    Component.onCompleted: {
        sysManager.createDir(currentPath);
        refreshFiles();
    }

    function refreshFiles() {
        try {
            var result = sysManager.listDirectory(currentPath);
            fileList = JSON.parse(result);
        } catch(e) { fileList = []; }
        selectedFile = "";
    }

    function navigateTo(path) {
        currentPath = path;
        refreshFiles();
    }

    function goUp() {
        if (currentPath === "/") return;
        var parts = currentPath.split("/");
        parts.pop();
        var parent = parts.join("/") || "/";
        navigateTo(parent);
    }

    function formatSize(bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
        return (bytes / (1024 * 1024 * 1024)).toFixed(1) + " GB";
    }

    function getFileIcon(item) {
        if (item.isDir) return "/";
        var ext = item.ext || "";
        if (["png","jpg","jpeg","gif","bmp","svg","webp"].indexOf(ext) >= 0) return "IMG";
        if (["mp4","avi","mkv","mov","webm"].indexOf(ext) >= 0) return "VID";
        if (["mp3","wav","flac","ogg","aac"].indexOf(ext) >= 0) return "AUD";
        if (["pdf"].indexOf(ext) >= 0) return "PDF";
        if (["xlsx","xls","csv"].indexOf(ext) >= 0) return "XLS";
        if (["zip","tar","gz","7z","rar"].indexOf(ext) >= 0) return "ZIP";
        if (["js","ts","py","c","cpp","h","rs","go","java"].indexOf(ext) >= 0) return "<>";
        if (["json","xml","yaml","yml","toml"].indexOf(ext) >= 0) return "CFG";
        if (["md","txt","log"].indexOf(ext) >= 0) return "TXT";
        if (["html","css","htm"].indexOf(ext) >= 0) return "WEB";
        if (["sh","bash","zsh"].indexOf(ext) >= 0) return ">_";
        return "DOC";
    }

    // Icon accent colours: map file types to Sereno semantic/accent tokens.
    // Directories get accent amber; images get info (soft blue); code gets accent;
    // archives get warn amber; destructive types (pdf) get danger tint.
    function getFileIconColor(item) {
        if (item.isDir) return Tokens.accentBase;
        var ext = item.ext || "";
        if (["png","jpg","jpeg","gif","bmp","svg","webp"].indexOf(ext) >= 0) return Tokens.infoBase;
        if (["mp4","avi","mkv","mov","webm"].indexOf(ext) >= 0) return Tokens.dangerBase;
        if (["mp3","wav","flac","ogg","aac"].indexOf(ext) >= 0) return Tokens.successBase;
        if (["pdf"].indexOf(ext) >= 0) return Tokens.dangerBase;
        if (["xlsx","xls","csv"].indexOf(ext) >= 0) return Tokens.successBase;
        if (["zip","tar","gz","7z","rar"].indexOf(ext) >= 0) return Tokens.warnBase;
        if (["js","ts","py","c","cpp","h","rs","go","java"].indexOf(ext) >= 0) return Tokens.accentBase;
        if (["json","xml","yaml","yml","toml"].indexOf(ext) >= 0) return Tokens.textMuted;
        if (["html","css","htm"].indexOf(ext) >= 0) return Tokens.accentHover;
        if (["sh","bash","zsh"].indexOf(ext) >= 0) return Tokens.successBase;
        return Tokens.textMuted;
    }

    // ── Toolbar ──────────────────────────────────────────────────────────────
    Rectangle {
        id: toolbar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: Math.round(46 * root.sf)
        color: Tokens.bgElevated
        border.color: Tokens.borderSubtle
        border.width: 1

        RowLayout {
            anchors.fill: parent
            anchors.margins: Math.round(Tokens.spSm * root.sf)
            spacing: Math.round(Tokens.spXs * root.sf)

            // Back
            Rectangle {
                width: Math.round(30 * root.sf)
                height: Math.round(30 * root.sf)
                radius: Math.round(Tokens.radiusSm * root.sf)
                color: backMa.containsMouse ? Tokens.bgCard : "transparent"
                Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                Text {
                    anchors.centerIn: parent
                    text: "←"
                    font.pixelSize: Math.round(15 * root.sf)
                    font.family: Tokens.fontBody
                    color: Tokens.textSecondary
                }
                MouseArea {
                    id: backMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: goUp()
                }
            }

            // Home (workspace root)
            Rectangle {
                width: Math.round(30 * root.sf)
                height: Math.round(30 * root.sf)
                radius: Math.round(Tokens.radiusSm * root.sf)
                color: homeMa.containsMouse ? Tokens.bgCard : "transparent"
                Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                Text {
                    anchors.centerIn: parent
                    text: "~"
                    font.pixelSize: Math.round(15 * root.sf)
                    font.weight: Font.Bold
                    font.family: Tokens.fontBody
                    color: Tokens.accentBase
                }
                MouseArea {
                    id: homeMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: navigateTo(workspacePath)
                }
            }

            // Breadcrumb path bar
            Rectangle {
                Layout.fillWidth: true
                height: Math.round(30 * root.sf)
                radius: Math.round(Tokens.radiusSm * root.sf)
                color: Tokens.bgSunken
                border.color: Tokens.borderSubtle
                border.width: 1
                clip: true

                Row {
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: Math.round(Tokens.spSm * root.sf)
                    spacing: Math.round(2 * root.sf)

                    Repeater {
                        model: {
                            var parts = currentPath.split("/").filter(function(p) { return p !== ""; });
                            var result = [{ name: "/", path: "/" }];
                            for (var i = 0; i < parts.length; i++) {
                                result.push({ name: parts[i], path: "/" + parts.slice(0, i + 1).join("/") });
                            }
                            return result;
                        }

                        delegate: Row {
                            spacing: Math.round(2 * root.sf)
                            Text {
                                text: index > 0 ? " › " : ""
                                font.pixelSize: Math.round(11 * root.sf)
                                font.family: Tokens.fontBody
                                color: Tokens.textMuted
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            Text {
                                text: modelData.name
                                font.pixelSize: Math.round(11 * root.sf)
                                font.family: Tokens.fontBody
                                color: bcMa.containsMouse ? Tokens.accentBase : Tokens.textSecondary
                                font.weight: index === 0 ? Font.Normal : Font.Medium
                                anchors.verticalCenter: parent.verticalCenter
                                Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                                MouseArea {
                                    id: bcMa
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: navigateTo(modelData.path)
                                }
                            }
                        }
                    }
                }
            }

            // Refresh
            Rectangle {
                width: Math.round(30 * root.sf)
                height: Math.round(30 * root.sf)
                radius: Math.round(Tokens.radiusSm * root.sf)
                color: refMa.containsMouse ? Tokens.bgCard : "transparent"
                Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                Text {
                    anchors.centerIn: parent
                    text: "↻"
                    font.pixelSize: Math.round(14 * root.sf)
                    font.family: Tokens.fontBody
                    color: Tokens.textSecondary
                }
                MouseArea {
                    id: refMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: refreshFiles()
                }
            }

            // New Folder
            Rectangle {
                width: nfRow.width + Math.round(Tokens.spLg * root.sf)
                height: Math.round(30 * root.sf)
                radius: Math.round(Tokens.radiusSm * root.sf)
                color: nfMa.containsMouse ? Tokens.accentSubtle : Tokens.accentGhost
                border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.30)
                border.width: 1
                Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }

                Row {
                    id: nfRow
                    anchors.centerIn: parent
                    spacing: Math.round(Tokens.spXs * root.sf)
                    Text {
                        text: "+"
                        font.pixelSize: Math.round(13 * root.sf)
                        font.weight: Font.Bold
                        font.family: Tokens.fontBody
                        color: Tokens.accentBase
                    }
                    Text {
                        text: "New Folder"
                        font.pixelSize: Math.round(11 * root.sf)
                        font.family: Tokens.fontBody
                        color: Tokens.accentBase
                    }
                }
                MouseArea {
                    id: nfMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: { showNewFolderDialog = true; newFolderName = ""; }
                }
            }
        }
    }

    // ── Column Headers ────────────────────────────────────────────────────────
    Rectangle {
        id: headerRow
        anchors.top: toolbar.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: Math.round(28 * root.sf)
        color: Tokens.bgSurface

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Math.round(Tokens.spLg * root.sf)
            anchors.rightMargin: Math.round(Tokens.spLg * root.sf)
            spacing: 0

            Text {
                text: "Name"
                font.pixelSize: Math.round(10 * root.sf)
                font.weight: Font.DemiBold
                font.family: Tokens.fontBody
                font.letterSpacing: 0.5
                color: Tokens.textMuted
                Layout.fillWidth: true
            }
            Text {
                visible: !fileManager._compact
                text: "Size"
                font.pixelSize: Math.round(10 * root.sf)
                font.weight: Font.DemiBold
                font.family: Tokens.fontBody
                font.letterSpacing: 0.5
                color: Tokens.textMuted
                Layout.preferredWidth: visible ? Math.round(80 * root.sf) : 0
                horizontalAlignment: Text.AlignRight
            }
            Text {
                visible: !fileManager._compact
                text: "Modified"
                font.pixelSize: Math.round(10 * root.sf)
                font.weight: Font.DemiBold
                font.family: Tokens.fontBody
                font.letterSpacing: 0.5
                color: Tokens.textMuted
                Layout.preferredWidth: visible ? Math.round(120 * root.sf) : 0
                horizontalAlignment: Text.AlignRight
            }
        }
    }

    // ── File List ─────────────────────────────────────────────────────────────
    Flickable {
        id: fileFlick
        anchors.top: headerRow.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: statusBar.top
        contentHeight: fileCol.height
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ScrollBar.vertical: LumenScrollBar { sf: root.sf }

        WheelHandler {
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: (event) => {
                var f = fileFlick;
                f.contentY = Math.max(0, Math.min(Math.max(0, f.contentHeight - f.height), f.contentY - event.angleDelta.y));
            }
        }

        Column {
            id: fileCol
            width: parent.width
            spacing: 0
            topPadding: Math.round(Tokens.spXs * root.sf)
            bottomPadding: Math.round(Tokens.spXs * root.sf)

            // Empty state
            Item {
                visible: fileList.length === 0
                width: parent.width
                height: Math.round(120 * root.sf)

                Column {
                    anchors.centerIn: parent
                    spacing: Math.round(Tokens.spSm * root.sf)

                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "Esta carpeta está vacía"
                        font.pixelSize: Math.round(13 * root.sf)
                        font.family: Tokens.fontBody
                        color: Tokens.textMuted
                    }
                }
            }

            Repeater {
                model: fileList

                delegate: Rectangle {
                    width: parent.width
                    height: Math.round(38 * root.sf)
                    color: selectedFile === modelData.path
                           ? Tokens.accentSubtle
                           : fileMa.containsMouse ? Tokens.bgElevated : "transparent"
                    border.color: selectedFile === modelData.path
                                  ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.22)
                                  : "transparent"
                    border.width: selectedFile === modelData.path ? 1 : 0

                    Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: Math.round(Tokens.spLg * root.sf)
                        anchors.rightMargin: Math.round(Tokens.spLg * root.sf)
                        spacing: Math.round(Tokens.spSm * root.sf)

                        // File type badge
                        Rectangle {
                            width: Math.round(30 * root.sf)
                            height: Math.round(18 * root.sf)
                            radius: Math.round(3 * root.sf)
                            color: Qt.rgba(
                                getFileIconColor(modelData).r,
                                getFileIconColor(modelData).g,
                                getFileIconColor(modelData).b,
                                0.10
                            )
                            border.color: Qt.rgba(
                                getFileIconColor(modelData).r,
                                getFileIconColor(modelData).g,
                                getFileIconColor(modelData).b,
                                0.35
                            )
                            border.width: 1

                            Text {
                                anchors.centerIn: parent
                                text: getFileIcon(modelData)
                                font.pixelSize: Math.round(7 * root.sf)
                                font.weight: Font.Bold
                                font.family: Tokens.fontMono
                                color: getFileIconColor(modelData)
                            }
                        }

                        // File name
                        Text {
                            text: modelData.name
                            font.pixelSize: Math.round(12 * root.sf)
                            font.family: Tokens.fontBody
                            color: modelData.isDir ? Tokens.accentBase : Tokens.textPrimary
                            font.weight: modelData.isDir ? Font.Medium : Font.Normal
                            elide: Text.ElideMiddle
                            Layout.fillWidth: true
                        }

                        // Size — hidden in compact mode
                        Text {
                            visible: !fileManager._compact
                            text: modelData.isDir ? "—" : formatSize(modelData.size)
                            font.pixelSize: Math.round(11 * root.sf)
                            font.family: Tokens.fontBody
                            color: Tokens.textMuted
                            Layout.preferredWidth: visible ? Math.round(80 * root.sf) : 0
                            horizontalAlignment: Text.AlignRight
                        }

                        // Modified date — hidden in compact mode
                        Text {
                            visible: !fileManager._compact
                            text: modelData.modified || ""
                            font.pixelSize: Math.round(11 * root.sf)
                            font.family: Tokens.fontBody
                            color: Tokens.textMuted
                            Layout.preferredWidth: visible ? Math.round(120 * root.sf) : 0
                            horizontalAlignment: Text.AlignRight
                        }
                    }

                    MouseArea {
                        id: fileMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                        onClicked: function(mouse) {
                            if (mouse.button === Qt.RightButton) {
                                selectedFile = modelData.path;
                                fileContextMenu.fileItem = modelData;
                                fileContextMenu.x = mouse.x;
                                fileContextMenu.y = mouse.y + parent.y - fileFlick.contentY + headerRow.height + toolbar.height;
                                fileContextMenu.visible = true;
                            } else {
                                selectedFile = modelData.path;
                            }
                        }
                        onDoubleClicked: {
                            if (modelData.isDir) {
                                navigateTo(modelData.path);
                            } else {
                                sysManager.openFile(modelData.path);
                            }
                        }
                    }
                }
            }
        }
    }

    // ── Status Bar ────────────────────────────────────────────────────────────
    Rectangle {
        id: statusBar
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: Math.round(26 * root.sf)
        color: Tokens.bgSurface
        border.color: Tokens.borderSubtle
        border.width: 1

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Math.round(Tokens.spMd * root.sf)
            anchors.rightMargin: Math.round(Tokens.spMd * root.sf)
            spacing: 0

            Text {
                text: fileList.length + " items"
                font.pixelSize: Math.round(10 * root.sf)
                font.family: Tokens.fontBody
                color: Tokens.textMuted
            }
            Item { Layout.fillWidth: true }
            Text {
                text: currentPath
                font.pixelSize: Math.round(10 * root.sf)
                font.family: Tokens.fontBody
                color: Tokens.textMuted
                elide: Text.ElideLeft
                Layout.maximumWidth: Math.round(300 * root.sf)
            }
        }
    }

    // ── File Context Menu ─────────────────────────────────────────────────────
    Rectangle {
        id: fileContextMenu
        visible: false
        z: 600
        width: Math.round(180 * root.sf)
        height: fCtxCol.height + Math.round(Tokens.spMd * root.sf)
        radius: Math.round(Tokens.radiusMd * root.sf)
        color: Tokens.bgElevated
        border.color: Tokens.borderDefault
        border.width: 1

        property var fileItem: ({})

        MouseArea {
            parent: fileManager
            anchors.fill: parent
            visible: fileContextMenu.visible
            z: 599
            onClicked: fileContextMenu.visible = false
        }

        Column {
            id: fCtxCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Math.round(Tokens.spXs * root.sf)
            spacing: 2

            Repeater {
                model: [
                    { label: "Open",      action: "open"     },
                    { label: "Copy Path", action: "copypath" },
                    { label: "Cut",       action: "cut"      },
                    { label: "Copy",      action: "copy"     },
                    { label: "Rename",    action: "rename"   },
                    { label: "Delete",    action: "delete"   }
                ]

                delegate: Rectangle {
                    width: parent.width
                    height: Math.round(30 * root.sf)
                    radius: Math.round(Tokens.radiusSm * root.sf)
                    color: ctxMa.containsMouse
                           ? (modelData.action === "delete" ? Tokens.dangerSubtle : Tokens.bgCard)
                           : "transparent"
                    Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.left: parent.left
                        anchors.leftMargin: Math.round(Tokens.spSm * root.sf)
                        text: modelData.label
                        font.pixelSize: Math.round(12 * root.sf)
                        font.family: Tokens.fontBody
                        color: modelData.action === "delete" ? Tokens.dangerBase : Tokens.textPrimary
                    }

                    MouseArea {
                        id: ctxMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            var item = fileContextMenu.fileItem;
                            fileContextMenu.visible = false;
                            if (modelData.action === "open" && item.isDir) navigateTo(item.path);
                            else if (modelData.action === "copypath") { sysManager.copyToClipboard(item.path); root.showToast("Path copied", "success"); }
                            else if (modelData.action === "copy") { clipboardPath = item.path; clipboardIsCut = false; root.showToast("Copied: " + item.name, "info"); }
                            else if (modelData.action === "cut") { clipboardPath = item.path; clipboardIsCut = true; root.showToast("Cut: " + item.name, "info"); }
                            else if (modelData.action === "rename") { showRenameDialog = true; renameName = item.name; }
                            else if (modelData.action === "delete") {
                                if (sysManager.deleteFile(item.path)) { root.showToast("Deleted: " + item.name, "success"); refreshFiles(); }
                                else root.showToast("Delete failed", "error");
                            }
                        }
                    }
                }
            }

            // Paste (only if clipboard has content)
            Rectangle {
                visible: clipboardPath !== ""
                width: parent.width
                height: Math.round(30 * root.sf)
                radius: Math.round(Tokens.radiusSm * root.sf)
                color: pasteMa.containsMouse ? Tokens.bgCard : "transparent"
                Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.left: parent.left
                    anchors.leftMargin: Math.round(Tokens.spSm * root.sf)
                    text: "Paste Here"
                    font.pixelSize: Math.round(12 * root.sf)
                    font.family: Tokens.fontBody
                    color: Tokens.successBase
                }

                MouseArea {
                    id: pasteMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        fileContextMenu.visible = false;
                        var srcParts = clipboardPath.split("/");
                        var fileName = srcParts[srcParts.length - 1];
                        var destPath = currentPath + "/" + fileName;
                        if (clipboardIsCut) { sysManager.renameFile(clipboardPath, destPath); clipboardPath = ""; root.showToast("Moved: " + fileName, "success"); }
                        else { root.showToast("Copied: " + fileName, "success"); }
                        refreshFiles();
                    }
                }
            }
        }
    }

    // ── New Folder Dialog ─────────────────────────────────────────────────────
    Rectangle {
        visible: showNewFolderDialog
        z: 700
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.55)

        MouseArea { anchors.fill: parent; onClicked: showNewFolderDialog = false }

        Rectangle {
            width: Math.round(320 * root.sf)
            height: Math.round(168 * root.sf)
            anchors.centerIn: parent
            radius: Math.round(Tokens.radiusMd * root.sf)
            color: Tokens.bgCard
            border.color: Tokens.borderDefault
            border.width: 1

            // Enter spring entrance
            scale: showNewFolderDialog ? 1.0 : 0.94
            Behavior on scale { enabled: !Tokens.reduceMotion; NumberAnimation { duration: Tokens.durModal; easing.type: Easing.OutCubic } }

            Column {
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spXl * root.sf)
                spacing: Math.round(Tokens.spMd * root.sf)

                Text {
                    text: "New Folder"
                    font.pixelSize: Math.round(15 * root.sf)
                    font.weight: Font.DemiBold
                    font.family: Tokens.fontDisplay
                    color: Tokens.textPrimary
                }

                LumenInput {
                    sf: root.sf
                    width: parent.width
                    placeholder: "Folder name…"
                    text: newFolderName
                    onAccepted: {
                        if (newFolderName.trim() !== "") {
                            sysManager.createDir(currentPath + "/" + newFolderName.trim());
                            root.showToast("Folder created: " + newFolderName.trim(), "success");
                            showNewFolderDialog = false;
                            refreshFiles();
                        }
                    }
                    Component.onCompleted: if (showNewFolderDialog) forceActiveFocus()
                    // LumenInput exposes .text alias — sync back to newFolderName
                    onTextChanged: newFolderName = text
                }

                Row {
                    anchors.right: parent.right
                    spacing: Math.round(Tokens.spSm * root.sf)

                    LumenButton {
                        sf: root.sf
                        label: "Cancel"
                        variant: "secondary"
                        implicitWidth: Math.round(90 * root.sf)
                        implicitHeight: Math.round(34 * root.sf)
                        onClicked: showNewFolderDialog = false
                    }

                    LumenButton {
                        sf: root.sf
                        label: "Create"
                        variant: "primary"
                        implicitWidth: Math.round(90 * root.sf)
                        implicitHeight: Math.round(34 * root.sf)
                        onClicked: {
                            if (newFolderName.trim() !== "") {
                                sysManager.createDir(currentPath + "/" + newFolderName.trim());
                                root.showToast("Folder created: " + newFolderName.trim(), "success");
                                showNewFolderDialog = false;
                                refreshFiles();
                            }
                        }
                    }
                }
            }
        }
    }

    // ── Rename Dialog ─────────────────────────────────────────────────────────
    Rectangle {
        visible: showRenameDialog
        z: 700
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.55)

        MouseArea { anchors.fill: parent; onClicked: showRenameDialog = false }

        Rectangle {
            width: Math.round(320 * root.sf)
            height: Math.round(168 * root.sf)
            anchors.centerIn: parent
            radius: Math.round(Tokens.radiusMd * root.sf)
            color: Tokens.bgCard
            border.color: Tokens.borderDefault
            border.width: 1

            scale: showRenameDialog ? 1.0 : 0.94
            Behavior on scale { enabled: !Tokens.reduceMotion; NumberAnimation { duration: Tokens.durModal; easing.type: Easing.OutCubic } }

            Column {
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spXl * root.sf)
                spacing: Math.round(Tokens.spMd * root.sf)

                Text {
                    text: "Rename"
                    font.pixelSize: Math.round(15 * root.sf)
                    font.weight: Font.DemiBold
                    font.family: Tokens.fontDisplay
                    color: Tokens.textPrimary
                }

                LumenInput {
                    sf: root.sf
                    width: parent.width
                    placeholder: "New name…"
                    text: renameName
                    onTextChanged: renameName = text
                    onAccepted: {
                        if (renameName.trim() !== "" && selectedFile !== "") {
                            var parts = selectedFile.split("/");
                            parts[parts.length - 1] = renameName.trim();
                            var newPath = parts.join("/");
                            if (sysManager.renameFile(selectedFile, newPath)) {
                                root.showToast("Renamed successfully", "success");
                            } else {
                                root.showToast("Rename failed", "error");
                            }
                            showRenameDialog = false;
                            refreshFiles();
                        }
                    }
                }

                Row {
                    anchors.right: parent.right
                    spacing: Math.round(Tokens.spSm * root.sf)

                    LumenButton {
                        sf: root.sf
                        label: "Cancel"
                        variant: "secondary"
                        implicitWidth: Math.round(90 * root.sf)
                        implicitHeight: Math.round(34 * root.sf)
                        onClicked: showRenameDialog = false
                    }

                    LumenButton {
                        sf: root.sf
                        label: "Rename"
                        variant: "primary"
                        implicitWidth: Math.round(90 * root.sf)
                        implicitHeight: Math.round(34 * root.sf)
                        onClicked: {
                            if (renameName.trim() !== "" && selectedFile !== "") {
                                var parts = selectedFile.split("/");
                                parts[parts.length - 1] = renameName.trim();
                                var newPath = parts.join("/");
                                if (sysManager.renameFile(selectedFile, newPath)) {
                                    root.showToast("Renamed successfully", "success");
                                } else {
                                    root.showToast("Rename failed", "error");
                                }
                                showRenameDialog = false;
                                refreshFiles();
                            }
                        }
                    }
                }
            }
        }
    }
}
