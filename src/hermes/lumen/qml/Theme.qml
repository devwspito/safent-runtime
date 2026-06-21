pragma Singleton
import QtQuick

// Lumen design-token singleton. Supports dark (default) and light modes.
// Set Theme.mode = "light" or "dark" at runtime; all bindings update live.
//
// Page-title tiers:
//   pageTitleSize (22) — in-app view headings, left-aligned, DemiBold
//   heroSize      (32) — onboarding / Home hero, center-aligned, Light
//
// The alpha() helper mirrors the one in the legacy Theme.js shim.
// White-on-accent and ok/warn/info semantics don't change between modes —
// only the neutral chrome (bg, surface, card, ink, line) adapts.
//
// Font: Inter variable font baked at qml/fonts/InterVariable.ttf.
// The FontLoader below guarantees Qt resolves "Inter" even if the system RPM
// (rsms-inter-fonts) is absent or exposes a different family name.
//
// GPU tier:
//   lightEffects = false  → VNC/software-render safe (static depth, no blur/motion)
//   lightEffects = true   → GPU tier (shadows with blur, animated transitions)
//   Controlled by env var HERMES_GPU_EFFECTS=1 at launch.
//   The gschema override 90_hermes-no-animations keeps this false by default.

QtObject {
    id: theme

    // ── Inter variable font loader ────────────────────────────────────────────
    // Loaded once here so every Text in the app resolves "Inter" without
    // depending on system font packages. source is relative to this file.
    property FontLoader _interLoader: FontLoader {
        source: Qt.resolvedUrl("fonts/InterVariable.ttf")
    }

    // ── Mode switch ───────────────────────────────────────────────────────────
    property string mode: "dark"

    // ── Accent "Sereno" — macOS Blue (spec 011). Replaces indigo #6E56CF. ─────
    // Preset: Azul macOS #0A84FF. Override by setting accent at runtime.
    readonly property color accent:       "#0A84FF"
    readonly property color accentBright: mode === "light" ? "#0070E0" : "#3399FF"
    readonly property color accentGlow:   "#0A84FF"
    readonly property color ok:           mode === "light" ? "#1F9D63" : "#34D399"
    readonly property color warn:         mode === "light" ? "#B9821A" : "#F5B945"
    readonly property color danger:       mode === "light" ? "#D92B2B" : "#FF453A"
    readonly property color info:         "#5BC8E0"

    // warn-text: light mode needs a darker value to hit 4.5:1 on light bg
    readonly property color warnText: mode === "light" ? "#8A5D0A" : warn

    // ── Text hierarchy ────────────────────────────────────────────────────────
    readonly property color ink:  mode === "light" ? "#1C1C1E" : "#F4F4F6"
    readonly property color ink2: mode === "light" ? "#3A3A3E" : "#C8C8D0"
    // ink3 light fixed from #6E6E76 → #6A6A72 for ≥4.5:1 on #FFFFFF (M1 a11y)
    readonly property color ink3: mode === "light" ? "#6A6A72" : "#9A9AA2"
    // ink4 was failing ~2.5:1; raised to meet 3:1 non-text / 4.5:1 text minimums
    readonly property color ink4: mode === "light" ? "#6E6E76" : "#8A8A93"

    // Placeholder text — same luminance tier as ink4
    readonly property color inkPlaceholder: mode === "light" ? "#6E6E76" : "#8A8A93"

    // Disabled label — use ink2 (never opacity-fade text for accessibility)
    // Disabled fill replaces opacity-based disabled backgrounds
    readonly property color disabledFill: mode === "light" ? "#D8D8DC" : "#26262A"

    // ── Surface hierarchy ─────────────────────────────────────────────────────
    // bg0: deepest background. bgBottom: subtle gradient end for vertical depth.
    // Use in a Gradient { GradientStop{position:0; color:Theme.bg0} GradientStop{position:1; color:Theme.bgBottom} }
    // on the window root Rectangle. Costs nothing on software render.
    readonly property color bg0:      mode === "light" ? "#F5F5F7" : "#0B0B0D"
    readonly property color bgBottom: mode === "light" ? "#EDEDEF" : "#111113"
    readonly property color surface:  mode === "light" ? "#FFFFFF"  : "#141416"
    readonly property color surface2: mode === "light" ? "#FFFFFF"  : "#1B1B1E"
    readonly property color card:     mode === "light" ? "#FFFFFF"  : "#212125"
    readonly property color card2:    mode === "light" ? "#F0F0F2"  : "#2A2A2E"

    // ── Borders ───────────────────────────────────────────────────────────────
    readonly property color line:  mode === "light" ? "#E3E3E6" : "#2A2A2E"
    readonly property color line2: mode === "light" ? "#D8D8DC" : "#333338"

    // ── Focus ring — solid, ≥3:1 against any surface ─────────────────────────
    readonly property color focusRing: mode === "light" ? "#0070E0" : "#3399FF"

    // ── Top hairline highlight (inner-top of cards/panels) ────────────────────
    // White @ 0.05 dark / 0.04 light — static, no layer.enabled
    readonly property real highlightTopOpacity: mode === "light" ? 0.04 : 0.05
    readonly property color highlightTopColor:  mode === "light" ? "#000000" : "#FFFFFF"

    // ── Typography ────────────────────────────────────────────────────────────
    // fontFamily resolves to "Inter" once FontLoader completes — guaranteed
    // regardless of whether the system has rsms-inter-fonts installed.
    readonly property string fontFamily: _interLoader.status === FontLoader.Ready
                                         ? _interLoader.name
                                         : "Inter"
    readonly property string mono:       "monospace"
    // Alias the old views used
    readonly property string font: fontFamily

    // ── GPU tier ──────────────────────────────────────────────────────────────
    // Default: false (VNC / software-render safe).
    // To enable: set HERMES_GPU_EFFECTS=1 in the environment before launch, or
    // pass --gpu-effects as a command-line argument to the process.
    //
    // The Python launcher (__main__.py / app_main.py) injects "--gpu-effects"
    // into sys.argv when it detects HERMES_GPU_EFFECTS=1 so this QML expression
    // stays pure (no process.env, which is a Node-only API).
    //
    // The gschema override 90_hermes-no-animations keeps this false by default
    // on boot — VNC sessions always start without GPU effects.
    readonly property bool lightEffects: Qt.application.arguments.indexOf("--gpu-effects") >= 0

    // ── Accessibility ─────────────────────────────────────────────────────────
    // reduceMotion mirrors !lightEffects: without GPU, all transitions are instant.
    readonly property bool reduceMotion: !lightEffects

    // ── Spacing tokens (8-pt grid) ────────────────────────────────────────────
    readonly property int sp1:  8
    readonly property int sp2: 16
    readonly property int sp3: 24
    readonly property int sp4: 40

    // ── Type scale (px) ───────────────────────────────────────────────────────
    readonly property int tsDisplay:  32   // hero / onboarding — center, Light
    readonly property int tsTitle:    22   // page title (pageTitleSize alias)
    readonly property int tsLead:     16   // intro lead / subtitle lead
    readonly property int tsButton:   15   // button labels, CTAs
    readonly property int tsSubtitle: 17   // card section header
    readonly property int tsLabel:    13   // form labels, secondary dense text
    readonly property int tsBody:     14   // default body text
    readonly property int tsCaption:  12   // metadata, labels, timestamps
    readonly property int tsMicro:    11   // badge text, status pills

    // Page-title convenience aliases (two visual tiers)
    readonly property int pageTitleSize: tsTitle    // 22 — in-app views, left, DemiBold
    readonly property int heroSize:      tsDisplay  // 32 — onboarding/Home, center, Light

    // ── Control / row / icon-tile size tokens ─────────────────────────────────
    readonly property int ctrlSm:     36   // small icon buttons, dense controls
    readonly property int ctrlMd:     44   // standard controls (WCAG touch target)
    readonly property int ctrlLg:     52   // primary CTA buttons
    readonly property int rowMd:      64   // standard list rows
    readonly property int rowLg:      72   // large list rows (two-line with icon)
    readonly property int iconTileSm: 28   // tight contexts (dock indicators)
    readonly property int iconTileMd: 36   // standard icon tile
    readonly property int iconTileLg: 40   // prominent icon tile

    // ── Radius tokens ─────────────────────────────────────────────────────────
    readonly property int rSm:  8
    readonly property int rMd: 12
    readonly property int rLg: 16
    readonly property int rXl: 20

    // ── Elevation scale — static offset rectangles (no layer.enabled / blur) ──
    // Use as a shadow underlay Rectangle offset below the card:
    //   color: "#000000", opacity: elev*.opacity, y: card.y + elev*.offsetY
    readonly property var elevRaised:   ({ offsetY: 2, opacity: 0.14 })
    readonly property var elevFloating: ({ offsetY: 3, opacity: 0.20 })
    readonly property var elevModal:    ({ offsetY: 4, opacity: 0.32 })

    // ── Icon path helpers ─────────────────────────────────────────────────────
    // Light-mode dim icons use "-dimlight.svg"; accent icons use "-accentlight.svg".
    // White-on-accent icons (arrow-up-white, etc.) are unchanged in both modes.
    // ok/warn-colored icons are unchanged — they are semantic color, not palette.

    function dimIcon(base) {
        // base = "icons/foo-dim.svg" or just "icons/foo" (no suffix)
        if (mode === "light") {
            return base.replace("-dim.svg", "-dimlight.svg")
        }
        return base
    }

    function accentIcon(base) {
        // base = "icons/foo-accent.svg"
        if (mode === "light") {
            return base.replace("-accent.svg", "-accentlight.svg")
        }
        return base
    }

    // ── Alpha helper ──────────────────────────────────────────────────────────
    // hex "#RRGGBB" -> Qt color string "#AARRGGBB" with alpha a in [0,1]
    function alpha(hex, a) {
        // Accept Qt color objects by converting to string first
        var h = (typeof hex === "string") ? hex : hex.toString()
        // Strip alpha channel if already present (8-char hex)
        if (h.length === 9) h = "#" + h.substring(3)
        var v = Math.round(a * 255).toString(16)
        if (v.length < 2) v = "0" + v
        return "#" + v + h.substring(1)
    }
}
