import QtQuick
import QtQuick.Controls
import "." // Tokens singleton requires explicit local import

// Sereno design system text input.
//
// Usage:
//   LumenInput {
//       sf: root.sf
//       placeholder: "Buscar..."
//       onAccepted: doSearch(text)
//   }

Item {
    id: inp

    // ── Public API ──
    property real   sf:          1.0
    property alias  text:        field.text
    property string placeholder: ""
    property bool   password:    false

    signal accepted()

    // ── Sizing ──
    implicitWidth:  Math.round(200 * sf)
    implicitHeight: Math.round(38 * sf)

    // ── States ──
    readonly property bool _focused: field.activeFocus
    readonly property bool _hovered: hoverArea.containsMouse

    // ── Border color resolves: default → hover → focus ──
    readonly property color _borderColor: {
        if (_focused) return Tokens.accentBase
        if (_hovered) return Tokens.borderStrong
        return Tokens.borderDefault
    }

    // ── Background ──
    Rectangle {
        id: bg
        anchors.fill: parent
        radius:       Math.round(Tokens.radiusMd * inp.sf)
        color:        inp._focused ? Tokens.bgSunken : Tokens.bgElevated
        border.width: 1
        border.color: inp._borderColor

        Behavior on border.color {
            enabled: !Tokens.reduceMotion
            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
        }
        Behavior on color {
            enabled: !Tokens.reduceMotion
            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
        }
    }

    // ── Focus ring (outside border, accent) ──
    Rectangle {
        anchors.fill:    parent
        anchors.margins: -Math.round(2 * inp.sf)
        radius:          Math.round((Tokens.radiusMd + 2) * inp.sf)
        color:           "transparent"
        border.width:    inp._focused ? Math.round(2 * inp.sf) : 0
        border.color:    Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.35)
        visible:         inp._focused

        Behavior on border.width {
            enabled: !Tokens.reduceMotion
            NumberAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
        }
    }

    // ── TextField ──
    TextField {
        id: field
        anchors {
            fill:         parent
            leftMargin:   Math.round(Tokens.spMd * inp.sf)
            rightMargin:  Math.round(Tokens.spMd * inp.sf)
            topMargin:    0
            bottomMargin: 0
        }

        font.pixelSize:   Math.round(13 * inp.sf)
        font.family:      "Inter"
        color:            Tokens.textPrimary
        placeholderText:  inp.placeholder
        placeholderTextColor: Tokens.textMuted
        echoMode:         inp.password ? TextInput.Password : TextInput.Normal
        verticalAlignment: TextInput.AlignVCenter

        background: Item {}   // bg handled by parent Rectangle

        onAccepted: inp.accepted()

        // Suppress default focus rectangle from QtQuick.Controls
        focusPolicy: Qt.StrongFocus
    }

    // ── Hover detection (field.hoverEnabled off by default in some Qt versions) ──
    MouseArea {
        id: hoverArea
        anchors.fill: parent
        hoverEnabled: true
        propagateComposedEvents: true
        onPressed: function(mouse) {
            field.forceActiveFocus()
            mouse.accepted = false
        }
    }
}
