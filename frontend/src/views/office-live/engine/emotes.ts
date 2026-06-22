/** Floating pixel-art emotes above characters — separate from speech bubbles */

import type { SpriteData } from "./types"

import { EMOTE_FADE_MS, EMOTE_LIFETIME_MS, EMOTE_POP_MS } from "./constants"

// ── Types ────────────────────────────────────────────────────

export type EmoteType =
  | "error"
  | "coffee"
  | "lightbulb"
  | "exclamation"
  | "star"
  | "phone"

export interface Emote {
  agentId: string
  type: EmoteType
  createdAt: number
  opacity: number
  yOffset: number
  scale: number
}

// ── 7x7 pixel-art emote sprites ─────────────────────────────

const _ = ""
const R = "#DD3333" // red
const Y = "#FFCC00" // yellow
const W = "#FFFFFF" // white
const B = "#885522" // brown
const G = "#FFDD44" // gold
const D = "#AA7711" // dark gold
const T = "#BBBBBB" // steam/light gray
const C = "#44DDAA" // cyan/green (phone)

const EMOTE_SPRITES: Record<EmoteType, SpriteData> = {
  error: [
    [R, _, _, _, _, _, R],
    [_, R, _, _, _, R, _],
    [_, _, R, _, R, _, _],
    [_, _, _, R, _, _, _],
    [_, _, R, _, R, _, _],
    [_, R, _, _, _, R, _],
    [R, _, _, _, _, _, R],
  ],
  coffee: [
    [_, _, T, _, T, _, _],
    [_, _, _, T, _, _, _],
    [_, B, B, B, B, _, _],
    [_, B, W, W, B, B, _],
    [_, B, W, W, B, B, _],
    [_, B, B, B, B, _, _],
    [_, _, B, B, _, _, _],
  ],
  lightbulb: [
    [_, _, Y, Y, Y, _, _],
    [_, Y, G, G, G, Y, _],
    [_, Y, G, G, G, Y, _],
    [_, Y, G, G, G, Y, _],
    [_, _, Y, Y, Y, _, _],
    [_, _, D, D, D, _, _],
    [_, _, _, D, _, _, _],
  ],
  exclamation: [
    [_, _, _, R, _, _, _],
    [_, _, _, R, _, _, _],
    [_, _, _, R, _, _, _],
    [_, _, _, R, _, _, _],
    [_, _, _, R, _, _, _],
    [_, _, _, _, _, _, _],
    [_, _, _, R, _, _, _],
  ],
  star: [
    [_, _, _, G, _, _, _],
    [_, _, G, G, G, _, _],
    [G, G, G, G, G, G, G],
    [_, G, G, G, G, G, _],
    [_, G, G, G, G, G, _],
    [_, G, _, _, _, G, _],
    [G, _, _, _, _, _, G],
  ],
  phone: [
    [_, C, C, C, C, C, _],
    [_, C, W, W, W, C, _],
    [_, C, W, W, W, C, _],
    [_, C, W, W, W, C, _],
    [_, C, C, C, C, C, _],
    [_, C, _, C, _, C, _],
    [_, C, C, C, C, C, _],
  ],
}

// ── Sprite cache (canvas ImageData) ─────────────────────────

const emoteImageCache = new Map<string, HTMLCanvasElement>()

function getEmoteCanvas(type: EmoteType, pixelSize: number): HTMLCanvasElement {
  const key = `${type}:${pixelSize}`
  const cached = emoteImageCache.get(key)
  if (cached) return cached

  const sprite = EMOTE_SPRITES[type]
  const w = sprite[0]!.length * pixelSize
  const h = sprite.length * pixelSize
  const canvas = document.createElement("canvas")
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext("2d")!

  for (let row = 0; row < sprite.length; row++) {
    for (let col = 0; col < sprite[row]!.length; col++) {
      const color = sprite[row]![col]!
      if (color) {
        ctx.fillStyle = color
        ctx.fillRect(col * pixelSize, row * pixelSize, pixelSize, pixelSize)
      }
    }
  }

  emoteImageCache.set(key, canvas)
  return canvas
}

// ── Lifecycle ────────────────────────────────────────────────

export function createEmote(agentId: string, type: EmoteType): Emote {
  return {
    agentId,
    type,
    createdAt: Date.now(),
    opacity: 1,
    yOffset: 0,
    scale: 0.3,
  }
}

export function updateEmote(emote: Emote, now: number): boolean {
  const age = now - emote.createdAt
  if (age > EMOTE_LIFETIME_MS) return false

  // Pop-in animation
  if (age < EMOTE_POP_MS) {
    const t = age / EMOTE_POP_MS
    // Ease-out bounce: overshoot then settle
    emote.scale =
      0.3 + 0.7 * (t < 0.7 ? (t / 0.7) * 1.15 : 1.15 - ((t - 0.7) / 0.3) * 0.15)
  } else {
    emote.scale = 1.0
  }

  // Float-up drift
  emote.yOffset = (age / 1000) * 6

  // Fade-out
  const fadeStart = EMOTE_LIFETIME_MS - EMOTE_FADE_MS
  if (age > fadeStart) {
    emote.opacity = 1 - (age - fadeStart) / EMOTE_FADE_MS
  } else {
    emote.opacity = 1
  }

  return true
}

// ── Rendering ────────────────────────────────────────────────

export function drawEmote(
  ctx: CanvasRenderingContext2D,
  emote: Emote,
  charCenterX: number,
  charTopY: number,
  zoom: number,
  hasBubble: boolean
): void {
  const pixelSize = Math.max(1, Math.round(zoom * 0.4))
  const emoteCanvas = getEmoteCanvas(emote.type, pixelSize)
  const w = emoteCanvas.width * emote.scale
  const h = emoteCanvas.height * emote.scale

  let yBase = charTopY - zoom * 10 - emote.yOffset * zoom
  if (hasBubble) {
    yBase -= zoom * 8
  }

  ctx.save()
  ctx.globalAlpha = emote.opacity

  // Dark circle background
  const cx = charCenterX
  const cy = yBase
  const radius = Math.max(w, h) * 0.6
  ctx.beginPath()
  ctx.arc(cx, cy, radius, 0, Math.PI * 2)
  ctx.fillStyle = "rgba(0,0,0,0.45)"
  ctx.fill()

  // Sprite
  ctx.drawImage(emoteCanvas, cx - w / 2, cy - h / 2, w, h)
  ctx.restore()
}
