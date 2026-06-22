/** Headset overlay for A2A calls — pixel art drawn on top of character heads */

import type { SpriteData } from "./types"
import { Direction } from "./types"

// ── Colors ──────────────────────────────────────────────────
const _ = ""
const K = "#222222" // headband (dark)
const G = "#333333" // headband highlight
const E = "#44DDAA" // earpiece (cyan/green — matches connection line)
const D = "#339977" // earpiece dark
const M = "#44DDAA" // mic boom
const L = "#66EEBB" // mic tip (light green glow)

// ── Headset sprites per direction ───────────────────────────
// Character frame is 16w x 32h. Head is ~rows 2-10.
// These sprites are positioned relative to the character's head.
// Size: 16w x 10h (covers the head area)

/** Facing DOWN — headband visible on top, earpieces on sides, mic on left */
const HEADSET_DOWN: SpriteData = [
  [_, _, _, _, _, K, K, K, K, K, K, _, _, _, _, _],
  [_, _, _, _, K, G, G, G, G, G, G, K, _, _, _, _],
  [_, _, _, _, K, _, _, _, _, _, _, K, _, _, _, _],
  [_, _, _, E, D, _, _, _, _, _, _, D, E, _, _, _],
  [_, _, _, E, D, _, _, _, _, _, _, D, E, _, _, _],
  [_, _, _, E, D, _, _, _, _, _, _, D, E, _, _, _],
  [_, _, _, D, _, _, _, _, _, _, _, _, D, _, _, _],
  [_, _, M, _, _, _, _, _, _, _, _, _, _, _, _, _],
  [_, M, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  [_, L, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
]

/** Facing UP — only headband visible from behind */
const HEADSET_UP: SpriteData = [
  [_, _, _, _, _, K, K, K, K, K, K, _, _, _, _, _],
  [_, _, _, _, K, G, G, G, G, G, G, K, _, _, _, _],
  [_, _, _, _, K, _, _, _, _, _, _, K, _, _, _, _],
  [_, _, _, E, D, _, _, _, _, _, _, D, E, _, _, _],
  [_, _, _, E, D, _, _, _, _, _, _, D, E, _, _, _],
  [_, _, _, D, _, _, _, _, _, _, _, _, D, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
]

/** Facing RIGHT — headband, right earpiece visible, mic curves down-right */
const HEADSET_RIGHT: SpriteData = [
  [_, _, _, _, _, _, K, K, K, K, K, K, _, _, _, _],
  [_, _, _, _, _, _, K, G, G, G, G, K, _, _, _, _],
  [_, _, _, _, _, _, K, _, _, _, _, K, _, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, D, E, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, D, E, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, D, E, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, _, D, _, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, _, _, M, _, _],
  [_, _, _, _, _, _, _, _, _, _, _, _, _, _, M, _],
  [_, _, _, _, _, _, _, _, _, _, _, _, _, _, L, _],
]

/** Facing LEFT — mirror of RIGHT */
const HEADSET_LEFT: SpriteData = HEADSET_RIGHT.map((row) => [...row].reverse())

const HEADSET_SPRITES: Record<number, SpriteData> = {
  [Direction.DOWN]: HEADSET_DOWN,
  [Direction.UP]: HEADSET_UP,
  [Direction.RIGHT]: HEADSET_RIGHT,
  [Direction.LEFT]: HEADSET_LEFT,
}

// ── Cached canvases ─────────────────────────────────────────

const headsetCache = new Map<string, HTMLCanvasElement>()

export function getHeadsetCanvas(dir: number, zoom: number): HTMLCanvasElement {
  const key = `${dir}:${zoom}`
  const cached = headsetCache.get(key)
  if (cached) return cached

  const sprite = HEADSET_SPRITES[dir] || HEADSET_DOWN
  const cols = sprite[0]!.length
  const rows = sprite.length

  // +2 for outline like character sprites
  const canvas = document.createElement("canvas")
  canvas.width = (cols + 2) * zoom
  canvas.height = (rows + 2) * zoom
  const ctx = canvas.getContext("2d")!
  ctx.imageSmoothingEnabled = false

  // Outline pass
  const OUTLINE_OFFSETS: [number, number][] = [
    [-1, 0],
    [1, 0],
    [0, -1],
    [0, 1],
  ]
  ctx.fillStyle = "#000000"
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      if (!sprite[r]![c]) continue
      for (const [dx, dy] of OUTLINE_OFFSETS) {
        ctx.fillRect((c + 1 + dx) * zoom, (r + 1 + dy) * zoom, zoom, zoom)
      }
    }
  }

  // Color pass
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const color = sprite[r]![c]!
      if (!color) continue
      ctx.fillStyle = color
      ctx.fillRect((c + 1) * zoom, (r + 1) * zoom, zoom, zoom)
    }
  }

  headsetCache.set(key, canvas)
  return canvas
}

// ── Headset Y offset relative to character drawY ────────────
// The headset sits on the character's head (approximately row 2-3 of the 32px sprite)
// Character sprite is drawn at drawY; head top is around drawY + 2*zoom (with outline offset)
export const HEADSET_Y_OFFSET_ROWS = 1 // rows from top of character sprite
