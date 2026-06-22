/** Per-zoom offscreen canvas cache with Habbo-style outlines */

import type { SpriteData } from "./types"

// Separate caches for outlined vs plain sprites
const outlinedCaches = new Map<number, WeakMap<SpriteData, HTMLCanvasElement>>()
const plainCaches = new Map<number, WeakMap<SpriteData, HTMLCanvasElement>>()

const OUTLINE_COLOR = "#000000"
const OUTLINE_OFFSETS: [number, number][] = [
  [-1, 0],
  [1, 0],
  [0, -1],
  [0, 1],
  [-1, -1],
  [-1, 1],
  [1, -1],
  [1, 1],
]

export function getCachedSprite(
  sprite: SpriteData,
  zoom: number,
  outline: boolean = true
): HTMLCanvasElement {
  const cacheStore = outline ? outlinedCaches : plainCaches
  let cache = cacheStore.get(zoom)
  if (!cache) {
    cache = new WeakMap()
    cacheStore.set(zoom, cache)
  }

  const cached = cache.get(sprite)
  if (cached) return cached

  const rows = sprite.length
  const cols = sprite[0]!.length
  const canvas = document.createElement("canvas")
  const ctx = canvas.getContext("2d")!
  ctx.imageSmoothingEnabled = false

  if (outline) {
    // +2 pixels each dimension for outline
    canvas.width = (cols + 2) * zoom
    canvas.height = (rows + 2) * zoom

    // Pass 1: Draw outline (4 cardinal offsets in black)
    ctx.fillStyle = OUTLINE_COLOR
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const color = sprite[r]![c]!
        if (color === "") continue
        for (const [dx, dy] of OUTLINE_OFFSETS) {
          ctx.fillRect((c + 1 + dx) * zoom, (r + 1 + dy) * zoom, zoom, zoom)
        }
      }
    }

    // Pass 2: Draw normal sprite centered (+1 offset)
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const color = sprite[r]![c]!
        if (color === "") continue
        ctx.fillStyle = color
        ctx.fillRect((c + 1) * zoom, (r + 1) * zoom, zoom, zoom)
      }
    }
  } else {
    // Plain sprite without outline
    canvas.width = cols * zoom
    canvas.height = rows * zoom

    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const color = sprite[r]![c]!
        if (color === "") continue
        ctx.fillStyle = color
        ctx.fillRect(c * zoom, r * zoom, zoom, zoom)
      }
    }
  }

  cache.set(sprite, canvas)
  return canvas
}
