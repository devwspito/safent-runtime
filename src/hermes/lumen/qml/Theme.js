.pragma library
// Lumen design tokens — neutral dark, macOS-grade.
// Accent: Blue macOS #0A84FF (spec 011 "Sereno"). Replaces indigo #6E56CF.
// This shim is read by views that import Theme.js directly. The canonical
// source is Theme.qml (singleton); keep both in sync.

// ── Color ────────────────────────────────────────────────────────────────
var accent       = "#0A84FF"
var accentBright = "#3399FF"
var accentGlow   = "#0A84FF"
var ok           = "#34D399"
var warn         = "#F5B945"
var info         = "#5BC8E0"

// Text hierarchy (ink → ink4 = progressively quieter)
var ink          = "#F4F4F6"   // primary — warm white
var ink2         = "#C8C8D0"   // secondary
var ink3         = "#9A9AA2"   // tertiary / captions
var ink4         = "#5A5A64"   // disabled / metadata / placeholder

// Surface hierarchy — neutral dark, no purple tint
var bg0      = "#0B0B0D"   // deepest background
var surface  = "#111113"   // view background
var surface2 = "#161618"   // elevated surface
var card     = "#1D1D20"   // card fill
var card2    = "#232326"   // input/inset fill

// Borders — thin hairlines only
var line     = "#2A2A2E"   // default hairline
var line2    = "#333338"   // slightly brighter divider

var fontFamily = "Inter"
var mono       = "monospace"

// Keep the public alias the old views used
var font = fontFamily

// GPU budget — FBOs and continuous animations disabled on weak hardware
var lightEffects = false

// ── Spacing tokens (8-pt grid) ───────────────────────────────────────────
var sp1  =  8   // xs  — tight inset, icon padding
var sp2  = 16   // sm  — inner card padding, icon gap
var sp3  = 24   // md  — section gap, outer card padding
var sp4  = 40   // lg  — view-level breathing room

// ── Type scale (px) ──────────────────────────────────────────────────────
var tsDisplay  = 32   // hero / onboarding headline
var tsTitle    = 22   // page title
var tsSubtitle = 17   // card section header
var tsBody     = 14   // default body text
var tsCaption  = 12   // metadata, labels, timestamps
var tsMicro    = 11   // badge text, status pills

// ── Radius tokens ────────────────────────────────────────────────────────
var rSm  =  8   // chips, small controls, badges
var rMd  = 12   // input fields, inner cards
var rLg  = 16   // cards
var rXl  = 20   // panels, dock, orb

// ── Helpers ──────────────────────────────────────────────────────────────
function alpha(hex, a) {
    // hex "#RRGGBB" -> "#AARRGGBB" with alpha a in [0,1]
    var v = Math.round(a * 255).toString(16)
    if (v.length < 2) v = "0" + v
    return "#" + v + hex.substring(1)
}
