import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import QtQuick.Controls
import "../../qml"

// SkillsWindow — standalone capability app: Habilidades del agente.
//
// Phase-0 visual refresh:
//   - All hardcoded hex/Qt.rgba() replaced by Theme tokens.
//   - Content clamped to Math.min(width-80, 760) for readable column.
//   - Empty states: "primer uso" vs "filtro vacío" with real CTA.
//   - Tab pills: active = accent@0.28 + accentBright border; inactive = transparent + line border.
//   - Text contrast: secondaries use Theme.ink3 (≥7:1), not hardcoded #6E6E76 (3.2:1).
//   - Gradient bg via Theme.bgBottom.
//
// Data source: ListSkills() → JSON   (polled every 10 s)
// Mutations (governance only):
//   PromoteSkill(skill_id)    — validated → autonomous
//   DeprecateSkill(skill_id)  — autonomous → deprecated
//
// Context properties (set by __main__.py):
//   backend    — AppBackend with promoteSkill/deprecateSkill slots
//   qmlBaseDir — absolute path to lumen/qml/

Window {
    id: appWindow
    title: "Habilidades — Hermes"
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

    // ── Skills list model ────────────────────────────────────────────────
    ListModel { id: skillsModel }

    property string filterState: "all"

    // True when the user has never taught a skill (total count = 0 regardless of filter).
    property bool neverTaught: skillsModel.count === 0

    function _populate(json) {
        var arr = []
        try { arr = JSON.parse(json) } catch (e) { arr = [] }
        skillsModel.clear()
        for (var i = 0; i < arr.length; i++) {
            var s = arr[i]
            skillsModel.append({
                skillId:     s.skill_id || s.id || "",
                name:        s.name || s.skill_name || "Sin nombre",
                description: s.description || "",
                state:       (s.state || s.status || "validated").toLowerCase(),
                surface:     s.surface_kind || s.executor || "",
                version:     s.version || "1"
            })
        }
    }

    Connections {
        target: backend
        function onListLoaded(key, json) {
            if (key === "skills") appWindow._populate(json)
        }
    }

    // ── Title bar ────────────────────────────────────────────────────────
    Rectangle {
        id: titleBar
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: 56; color: Theme.surface

        Rectangle {
            anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
            height: 1; color: Theme.line
        }

        RowLayout {
            anchors { fill: parent; leftMargin: Theme.sp3; rightMargin: Theme.sp3 }
            spacing: Theme.sp2

            Rectangle {
                width: 32; height: 32; radius: Theme.rSm
                color: Theme.alpha(Theme.accent, 0.14)
                border.color: Theme.alpha(Theme.accentBright, 0.18); border.width: 1
                Image {
                    anchors.centerIn: parent; width: 16; height: 16
                    source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/sparkles-dim.svg")
                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                }
            }

            ColumnLayout {
                spacing: 2
                Text {
                    text: "Habilidades"
                    color: Theme.ink
                    font.family: Theme.font; font.pixelSize: Theme.tsSubtitle; font.weight: Font.DemiBold
                }
                Text {
                    text: "Lo que sabe hacer Lumen"
                    color: Theme.ink3
                    font.family: Theme.font; font.pixelSize: Theme.tsCaption
                }
            }

            Item { Layout.fillWidth: true }

            Rectangle {
                height: 24; radius: Theme.rSm - 2
                implicitWidth: connRow.implicitWidth + 16
                color: backend.connected ? Theme.alpha(Theme.ok, 0.10) : Theme.alpha(Theme.warn, 0.10)
                border.color: backend.connected ? Theme.alpha(Theme.ok, 0.22) : Theme.alpha(Theme.warn, 0.22)
                border.width: 1
                Row {
                    id: connRow; anchors.centerIn: parent; spacing: 5
                    Rectangle { width: 6; height: 6; radius: 3; color: backend.connected ? Theme.ok : Theme.warn }
                    Text {
                        text: backend.connected ? "Lumen activo" : "Lumen no responde"
                        color: backend.connected ? Theme.ok : Theme.warn
                        font.family: Theme.font; font.pixelSize: Theme.tsMicro; font.weight: Font.Medium
                    }
                }
            }
        }
    }

    // ── Content ──────────────────────────────────────────────────────────
    Item {
        anchors { top: titleBar.bottom; left: parent.left; right: parent.right; bottom: parent.bottom }

        // ── Loading state ─────────────────────────────────────────────
        Item {
            anchors.fill: parent
            visible: backend.loading && !backend.daemonError.length
            Column {
                anchors.centerIn: parent; spacing: Theme.sp2
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 48; height: 48; radius: 24
                    color: Theme.alpha(Theme.accent, 0.10)
                    border.color: Theme.alpha(Theme.accentBright, 0.22); border.width: 1
                    Image {
                        anchors.centerIn: parent; width: 22; height: 22
                        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/rotate-cw-dim.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true; opacity: 0.8
                    }
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Cargando habilidades…"
                    color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsBody
                }
            }
        }

        // ── Error state ───────────────────────────────────────────────
        Item {
            anchors.fill: parent
            visible: backend.daemonError.length > 0
            Column {
                anchors.centerIn: parent; spacing: Theme.sp2; width: Math.min(parent.width - 64, 420)
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 56; height: 56; radius: 28
                    color: Theme.alpha(Theme.warn, 0.10)
                    border.color: Theme.alpha(Theme.warn, 0.24); border.width: 1
                    Image {
                        anchors.centerIn: parent; width: 24; height: 24
                        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/alert-circle-warn.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                    }
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Lumen no responde"
                    color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.DemiBold
                }
                Text {
                    width: parent.width; horizontalAlignment: Text.AlignHCenter
                    text: backend.daemonError || "Comprueba que Lumen está en marcha e inténtalo de nuevo."
                    color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption; wrapMode: Text.WordWrap
                }
            }
        }

        // ── Real content ──────────────────────────────────────────────
        ColumnLayout {
            anchors.fill: parent
            spacing: 0
            visible: !backend.loading && !backend.daemonError.length

            // ── Filter tabs pill ──────────────────────────────────────
            Item {
                Layout.fillWidth: true; height: 52

                // Centered clamp matches list below
                Item {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Math.min(parent.width - 80, 760)
                    anchors.horizontalCenter: parent.horizontalCenter
                    height: 36

                    Row {
                        anchors { left: parent.left; verticalCenter: parent.verticalCenter }
                        spacing: 6

                        Repeater {
                            model: [
                                { label: "Todas",      key: "all" },
                                { label: "Activas",    key: "autonomous" },
                                { label: "En prueba",  key: "validated" },
                                { label: "Archivadas", key: "deprecated" }
                            ]

                            // Tab pill: active = accent fill + accentBright border
                            //           inactive = transparent fill + line border
                            Rectangle {
                                property bool isActive: appWindow.filterState === modelData.key
                                height: 30; radius: Theme.rSm
                                implicitWidth: chipTxt.width + 24
                                // Active: accent@0.28 fill; inactive: transparent
                                color: isActive ? Theme.alpha(Theme.accent, 0.28) : "transparent"
                                // Active: accentBright@0.50 border; inactive: line border
                                border.color: isActive ? Theme.alpha(Theme.accentBright, 0.50) : Theme.line
                                border.width: 1

                                Text {
                                    id: chipTxt; anchors.centerIn: parent
                                    text: modelData.label
                                    // Active: ink (full contrast); inactive: ink3 (~7:1)
                                    color: isActive ? Theme.ink : Theme.ink3
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsLabel
                                    font.weight: isActive ? Font.SemiBold : Font.Normal
                                }

                                MouseArea {
                                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                    onClicked: appWindow.filterState = modelData.key
                                }
                            }
                        }
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.line }

            // ── Skills list ───────────────────────────────────────────
            ListView {
                id: skillsList
                Layout.fillWidth: true; Layout.fillHeight: true
                clip: true; spacing: 0
                topMargin: Theme.sp1; bottomMargin: Theme.sp2
                boundsBehavior: Flickable.StopAtBounds

                ScrollBar.vertical: ScrollBar {
                    policy: ScrollBar.AsNeeded
                    contentItem: Rectangle { radius: 2; color: Theme.alpha(Theme.ink3, 0.28) }
                }

                model: {
                    var result = []
                    for (var i = 0; i < skillsModel.count; i++) {
                        var s = skillsModel.get(i)
                        if (appWindow.filterState === "all" || s.state === appWindow.filterState) result.push(i)
                    }
                    return result
                }

                delegate: Item {
                    // Clamp to readable column width
                    width: skillsList.width
                    height: rowCard.height + 6

                    property int idx: modelData
                    property var skill: skillsModel.get(idx)

                    // Centered container
                    Item {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: Math.min(parent.width - 80, 760)
                        height: parent.height

                        // Static shadow underlay
                        Rectangle {
                            anchors { left: parent.left; right: parent.right; top: parent.top; topMargin: 2; leftMargin: 2; rightMargin: -2 }
                            height: rowCard.height; radius: Theme.rLg; color: "#000000"; opacity: Theme.elevRaised.opacity
                        }

                        Rectangle {
                            id: rowCard
                            anchors { left: parent.left; right: parent.right }
                            height: rowContent.height + Theme.sp3; radius: Theme.rLg
                            color: Theme.card; border.color: Theme.line; border.width: 1

                            // Inner top hairline
                            Rectangle {
                                anchors { top: parent.top; left: parent.left; right: parent.right; topMargin: 1; leftMargin: 1; rightMargin: 1 }
                                height: 1; radius: Theme.rLg - 1; color: Theme.highlightTopColor; opacity: Theme.highlightTopOpacity
                            }

                            ColumnLayout {
                                id: rowContent
                                anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.sp2; topMargin: 14 }
                                spacing: Theme.sp1

                                RowLayout {
                                    spacing: 12

                                    // State-colored icon tile
                                    Rectangle {
                                        width: Theme.iconTileMd; height: Theme.iconTileMd; radius: 10
                                        color: skill.state === "autonomous" ? Theme.alpha(Theme.ok, 0.12)
                                             : skill.state === "deprecated" ? Theme.alpha(Theme.ink4, 0.30)
                                             : Theme.alpha(Theme.accent, 0.14)
                                        border.color: skill.state === "autonomous" ? Theme.alpha(Theme.ok, 0.20)
                                                    : skill.state === "deprecated" ? Theme.line
                                                    : Theme.alpha(Theme.accentBright, 0.18)
                                        border.width: 1
                                        Image {
                                            anchors.centerIn: parent; width: 18; height: 18
                                            source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/sparkles-dim.svg")
                                            fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                            opacity: skill.state === "deprecated" ? 0.35 : 0.85
                                        }
                                    }

                                    // Name + surface description
                                    Column {
                                        Layout.fillWidth: true; spacing: 3
                                        Text {
                                            text: skill.name
                                            // Deprecated → ink3 (still legible); others → ink
                                            color: skill.state === "deprecated" ? Theme.ink3 : Theme.ink
                                            font.family: Theme.font; font.pixelSize: Theme.tsBody
                                            font.weight: Font.Medium; width: parent.width; elide: Text.ElideRight
                                        }
                                        Text {
                                            text: skill.surface.length > 0
                                                  ? skill.surface + (skill.description.length > 0 ? " · " + skill.description : "")
                                                  : skill.description
                                            // ink3 (~7:1 on dark card) — was hardcoded #9A9AA2 (passes) but unify to token
                                            color: Theme.ink3
                                            font.family: Theme.font; font.pixelSize: Theme.tsCaption
                                            width: parent.width; elide: Text.ElideRight
                                        }
                                    }

                                    // State badge
                                    Rectangle {
                                        height: 22; radius: Theme.rSm - 2
                                        implicitWidth: stateTxt.width + 16
                                        color: skill.state === "autonomous" ? Theme.alpha(Theme.ok, 0.12)
                                             : skill.state === "deprecated" ? Theme.alpha(Theme.ink4, 0.18)
                                             : Theme.alpha(Theme.accent, 0.14)
                                        border.color: skill.state === "autonomous" ? Theme.alpha(Theme.ok, 0.30)
                                                    : skill.state === "deprecated" ? Theme.line
                                                    : Theme.alpha(Theme.accentBright, 0.30)
                                        border.width: 1
                                        Text {
                                            id: stateTxt; anchors.centerIn: parent
                                            text: skill.state === "autonomous" ? "Activa"
                                                : skill.state === "deprecated" ? "Archivada"
                                                : "En prueba"
                                            color: skill.state === "autonomous" ? Theme.ok
                                                 : skill.state === "deprecated" ? Theme.ink3
                                                 : Theme.accentBright
                                            font.family: Theme.font; font.pixelSize: Theme.tsMicro; font.weight: Font.Medium
                                        }
                                    }
                                }

                                // Action row — governance mutators (supervisor only)
                                RowLayout {
                                    spacing: Theme.sp1
                                    visible: skill.state !== "deprecated"

                                    // Promote: validated → autonomous
                                    Rectangle {
                                        visible: skill.state === "validated"
                                        height: 28; radius: Theme.rSm; color: "transparent"
                                        border.color: Theme.alpha(Theme.ok, 0.35); border.width: 1
                                        implicitWidth: promTxt.width + 24
                                        Text {
                                            id: promTxt; anchors.centerIn: parent
                                            text: "Activar"; color: Theme.ok
                                            font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.Medium
                                        }
                                        MouseArea {
                                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                            onClicked: backend.promoteSkill(skill.skillId)
                                        }
                                    }

                                    // Deprecate: autonomous → deprecated
                                    Rectangle {
                                        visible: skill.state === "autonomous"
                                        height: 28; radius: Theme.rSm; color: "transparent"
                                        border.color: Theme.line; border.width: 1
                                        implicitWidth: depTxt.width + 24
                                        Text {
                                            id: depTxt; anchors.centerIn: parent
                                            text: "Archivar"; color: Theme.ink3
                                            font.family: Theme.font; font.pixelSize: Theme.tsCaption
                                        }
                                        MouseArea {
                                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                            onClicked: backend.deprecateSkill(skill.skillId)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // ── Empty state ───────────────────────────────────────
                // Two psychologically distinct states:
                //   first-use  — neverTaught (total list empty, no filter active)
                //   filter-empty — list has items but the active filter hides them all
                Item {
                    visible: skillsList.model.length === 0
                    width: skillsList.width; height: skillsList.height

                    // true when the list is empty because a filter/tab is active (not first-use)
                    property bool isFilterEmpty: !appWindow.neverTaught && appWindow.filterState !== "all"

                    // Centered card — max 480px, generous padding
                    Rectangle {
                        anchors.centerIn: parent
                        width: Math.min(parent.width - 80, 480)
                        height: emptyCol.height + 64
                        radius: Theme.rXl
                        color: Theme.card
                        border.color: Theme.line; border.width: 1

                        // Top highlight
                        Rectangle {
                            anchors { top: parent.top; left: parent.left; right: parent.right; topMargin: 1; leftMargin: 1; rightMargin: 1 }
                            height: 1; radius: Theme.rXl - 1; color: Theme.highlightTopColor; opacity: Theme.highlightTopOpacity
                        }

                        ColumnLayout {
                            id: emptyCol
                            anchors { left: parent.left; right: parent.right; top: parent.top; topMargin: 40; leftMargin: Theme.sp4; rightMargin: Theme.sp4 }
                            spacing: Theme.sp2

                            // Icon — teach mode for first-use, neutral for filter-empty
                            Rectangle {
                                Layout.alignment: Qt.AlignHCenter
                                width: 64; height: 64; radius: 32
                                color: Theme.alpha(Theme.accent, 0.10)
                                border.color: Theme.alpha(Theme.accentBright, 0.22); border.width: 1
                                Image {
                                    anchors.centerIn: parent; width: 28; height: 28
                                    source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/sparkles-dim.svg")
                                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                }
                            }

                            // Title
                            Text {
                                Layout.alignment: Qt.AlignHCenter
                                text: parent.parent.isFilterEmpty
                                      ? "Ninguna habilidad coincide con este filtro"
                                      : "Lumen aún no ha aprendido nada"
                                color: Theme.ink
                                font.family: Theme.font; font.pixelSize: Theme.tsSubtitle; font.weight: Font.SemiBold
                                horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }

                            // Body
                            Text {
                                Layout.alignment: Qt.AlignHCenter
                                Layout.fillWidth: true
                                text: parent.parent.isFilterEmpty
                                      ? ""
                                      : "Enséñale algo una vez — redactar un correo, rellenar un formulario, buscar en una web — y lo hará solo a partir de entonces."
                                color: Theme.ink3
                                font.family: Theme.font; font.pixelSize: Theme.tsBody; wrapMode: Text.WordWrap
                                horizontalAlignment: Text.AlignHCenter; lineHeight: 1.5
                                visible: !parent.parent.isFilterEmpty
                            }

                            // CTA: first-use → "Enseñar algo nuevo"; filter-empty → "Borrar filtros"
                            PrimaryButton {
                                visible: !parent.parent.isFilterEmpty
                                Layout.fillWidth: true
                                label: "Enseñar algo nuevo"
                                iconSource: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/sparkles-dim.svg")
                                iconWidth: 16
                                onClicked: backend.loadList("skills")
                            }

                            // Filter-empty CTA — ghost button style
                            Rectangle {
                                visible: parent.parent.isFilterEmpty
                                Layout.fillWidth: true; height: 44; radius: Theme.rLg
                                color: "transparent"
                                border.color: Theme.alpha(Theme.accentBright, 0.40); border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: "Borrar filtros"
                                    color: Theme.accentBright
                                    font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.Medium
                                }
                                MouseArea {
                                    anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                    onClicked: appWindow.filterState = "all"
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
