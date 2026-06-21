pragma Singleton
import QtQuick

// LumenSO "Sereno" design token foundation.
// All values in logical pixels. Multiply by root.sf at usage sites.
// Wave 1 — single source of truth; kills dual-palette across the compositor.
QtObject {
    id: tokens

    // ── Motion control (gate ALL animations on this flag) ──
    property bool reduceMotion: false

    // ── Duration scale (ms) ──
    readonly property int durInstant:  90
    readonly property int durFast:    140
    readonly property int durBase:    220
    readonly property int durSlow:    320
    readonly property int durModal:   260

    // ── Easing convenience names (use in NumberAnimation.easing.type) ──
    // Enter:  Easing.OutCubic
    // Exit:   Easing.InCubic
    // Move:   Easing.InOutCubic
    // Hover:  Easing.OutQuad
    // Spring: Easing.OutBack (overshoot 1.2 — see springOvershoot)
    readonly property real springOvershoot: 1.2

    // ── Background ──
    readonly property color bgVoid:     "#0A0B0E"
    readonly property color bgSurface:  "#14151B"
    readonly property color bgElevated: "#1B1D24"
    readonly property color bgCard:     "#20222B"
    readonly property color bgSunken:   "#0E0F13"

    // ── Border ──
    readonly property color borderSubtle:  "#24262F"
    readonly property color borderDefault: "#2E313C"
    readonly property color borderStrong:  "#3C404C"

    // ── Text ──
    readonly property color textPrimary:  "#F3F4F7"
    readonly property color textSecondary:"#C2C6D0"
    readonly property color textMuted:    "#8A8F9C"
    readonly property color textDisabled: "#5A5F6B"
    readonly property color textOnAccent: "#0A0B0E"

    // ── Accent — ONE amber, three semantic states ──
    readonly property color accentBase:   "#F0A85A"
    readonly property color accentHover:  "#F7B86E"
    readonly property color accentPressed:"#C77A2E"
    readonly property color accentSubtle: Qt.rgba(240/255, 168/255, 90/255, 0.12)
    readonly property color accentGhost:  Qt.rgba(240/255, 168/255, 90/255, 0.06)

    // ── Semantic ──
    readonly property color successBase:  "#5FD1A8"
    readonly property color successSubtle:Qt.rgba(95/255, 209/255, 168/255, 0.12)
    readonly property color warnBase:     "#EFC05C"
    readonly property color warnSubtle:   Qt.rgba(239/255, 192/255, 92/255, 0.12)
    readonly property color dangerBase:   "#F0768A"
    readonly property color dangerSubtle: Qt.rgba(240/255, 118/255, 138/255, 0.12)
    readonly property color infoBase:     "#8FA6F0"
    readonly property color infoSubtle:   Qt.rgba(143/255, 166/255, 240/255, 0.12)

    // ── Spacing (4-grid, logical px) ──
    readonly property int spXs:   4
    readonly property int spSm:   8
    readonly property int spMd:  12
    readonly property int spLg:  16
    readonly property int spXl:  24
    readonly property int spXxl: 32
    readonly property int spXxxl:48

    // ── Responsive breakpoints (logical px, comparar contra el ANCHO del
    //    contenedor: el ancho propio de la pantalla = ancho de su ventana, o
    //    root.width para el chrome del escritorio). Reflow, no solo escala. ──
    readonly property int bpCompact: 720    // < bpCompact: 1 columna, sidebar colapsado/oculto, stack vertical
    readonly property int bpRegular: 1040   // < bpRegular: layout intermedio; >= : ancho completo
    readonly property int bpWide:    1440   // >= bpWide: aprovechar ancho (más columnas / paneles lado a lado)

    // ── Radius (logical px) ──
    readonly property int radiusSm:   8
    readonly property int radiusMd:  14
    readonly property int radiusLg:  20
    readonly property int radiusPill:999

    // ── Z / elevation stacking (single source of truth — never use bare magic numbers) ──
    // Items inside an AppWindow (clipped) use these relative to the app root Item.
    // Shell chrome items (Desktop.qml / TopBar / ChatBar) apply their own offsets
    // on top of these tiers but must respect the order below.
    readonly property int zBase:    0      // normal flow
    readonly property int zRaised:  10     // hover cards, raised panels
    readonly property int zSticky:  100    // sticky headers, floating toolbars
    readonly property int zModal:   1000   // in-app modals / dialogs (LumenModal)
    readonly property int zToast:   2000   // toasts / snackbars (above modal)

    // ── Font families (single source of truth — decisión 2026-06-14, fontpair) ──
    // Titulares/marca en Space Grotesk (geométrica, distintiva); cuerpo/UI en
    // Inter (legibilidad en texto denso). Ambas horneadas. Úsalas como
    // `font.family: Tokens.fontDisplay` / `Tokens.fontBody`.
    readonly property string fontDisplay: "Space Grotesk"
    readonly property string fontBody:    "Inter"
    readonly property string fontMono:    "Adwaita Mono, monospace"

    // ── Typography descriptors (for reference — apply manually in Text{} items) ──
    // display:    fontDisplay (Space Grotesk), 28px, Bold,     lh 1.20, ls -0.4
    // title:      fontDisplay (Space Grotesk), 20px, Medium,   ls -0.3
    // heading:    fontBody    (Inter),         15px, SemiBold, ls -0.2
    // body:       fontBody    (Inter),         13px, Regular,  lh 1.55
    // bodyStrong: fontBody    (Inter),         13px, Medium
    // caption:    fontBody    (Inter),         11px, Medium
    // micro:      fontBody    (Inter),          9px, SemiBold, ls  0.6
    // mono:       fontMono,                    12px
}
