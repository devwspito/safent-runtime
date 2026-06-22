/** Sprite loading — loads character PNGs + furniture sprites in code
 *  Adapted from pixel-agents (MIT) */

import type { CharacterSprites, SpriteData } from "./types"
import { Direction } from "./types"

// ── Character sprite loading from PNG ─────────────────────────

const CHAR_FRAME_W = 16
const CHAR_FRAME_H = 32
const CHAR_FRAMES_PER_ROW = 7
const CHAR_DIRECTIONS = ["down", "up", "right"] as const
const CHAR_COUNT = 6

interface LoadedCharacterData {
  down: SpriteData[]
  up: SpriteData[]
  right: SpriteData[]
}

let loadedCharacters: LoadedCharacterData[] | null = null
const spriteCache = new Map<string, CharacterSprites>()

function flipSpriteHorizontal(sprite: SpriteData): SpriteData {
  return sprite.map((row) => [...row].reverse())
}

function extractFramesFromImage(img: HTMLImageElement): LoadedCharacterData {
  const canvas = document.createElement("canvas")
  canvas.width = img.width
  canvas.height = img.height
  const ctx = canvas.getContext("2d")!
  ctx.drawImage(img, 0, 0)

  const result: LoadedCharacterData = { down: [], up: [], right: [] }
  const dirKeys = CHAR_DIRECTIONS

  for (let dirIdx = 0; dirIdx < dirKeys.length; dirIdx++) {
    const dir = dirKeys[dirIdx]
    const rowOffsetY = dirIdx * CHAR_FRAME_H
    const frames: SpriteData[] = []

    for (let f = 0; f < CHAR_FRAMES_PER_ROW; f++) {
      const sprite: string[][] = []
      const frameOffsetX = f * CHAR_FRAME_W

      for (let y = 0; y < CHAR_FRAME_H; y++) {
        const row: string[] = []
        for (let x = 0; x < CHAR_FRAME_W; x++) {
          const imgData = ctx.getImageData(
            frameOffsetX + x,
            rowOffsetY + y,
            1,
            1
          ).data
          const r = imgData[0]!
          const g = imgData[1]!
          const b = imgData[2]!
          const a = imgData[3]!
          if (a < 128) {
            row.push("")
          } else {
            row.push(
              `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`
            )
          }
        }
        sprite.push(row)
      }
      frames.push(sprite)
    }

    result[dir!] = frames
  }

  return result
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = (e) =>
      reject(new Error(`Failed to load image: ${src} — ${e}`))
    img.src = src
  })
}

export async function loadCharacterSprites(): Promise<void> {
  if (loadedCharacters) return

  const results = await Promise.allSettled(
    Array.from({ length: CHAR_COUNT }, (_, i) =>
      loadImage(`/app/assets/office/characters/char_${i}.png`).then(
        extractFramesFromImage
      )
    )
  )

  const characters: LoadedCharacterData[] = []
  for (let i = 0; i < results.length; i++) {
    const r = results[i]!
    if (r.status === "fulfilled") {
      characters.push(r.value)
    } else {
      console.warn(`[office] Failed to load char_${i}.png:`, r.reason)
    }
  }

  if (characters.length > 0) {
    loadedCharacters = characters
    spriteCache.clear()
  } else {
    console.error(
      "[office] No character sprites loaded — characters will be invisible"
    )
  }
}

export function getCharacterSprites(
  paletteIndex: number,
  _hueShift = 0
): CharacterSprites | null {
  if (!loadedCharacters || loadedCharacters.length === 0) return null

  const cacheKey = `${paletteIndex}`
  const cached = spriteCache.get(cacheKey)
  if (cached) return cached

  const char = loadedCharacters[paletteIndex % loadedCharacters.length]!
  const d = char.down
  const u = char.up
  const rt = char.right
  const flip = flipSpriteHorizontal

  const sprites: CharacterSprites = {
    walk: {
      [Direction.DOWN]: [d[0]!, d[1]!, d[2]!, d[1]!],
      [Direction.UP]: [u[0]!, u[1]!, u[2]!, u[1]!],
      [Direction.RIGHT]: [rt[0]!, rt[1]!, rt[2]!, rt[1]!],
      [Direction.LEFT]: [
        flip(rt[0]!),
        flip(rt[1]!),
        flip(rt[2]!),
        flip(rt[1]!),
      ],
    },
    typing: {
      [Direction.DOWN]: [d[3]!, d[4]!],
      [Direction.UP]: [u[3]!, u[4]!],
      [Direction.RIGHT]: [rt[3]!, rt[4]!],
      [Direction.LEFT]: [flip(rt[3]!), flip(rt[4]!)],
    },
    thinking: {
      [Direction.DOWN]: [d[5]!, d[6]!],
      [Direction.UP]: [u[5]!, u[6]!],
      [Direction.RIGHT]: [rt[5]!, rt[6]!],
      [Direction.LEFT]: [flip(rt[5]!), flip(rt[6]!)],
    },
  }

  spriteCache.set(cacheKey, sprites)
  return sprites
}

export function getCharacterSprite(
  sprites: CharacterSprites,
  state: string,
  dir: number,
  frame: number
): SpriteData {
  const fallback = sprites.walk[Direction.DOWN]![1]!
  if (state === "type" || state === "meet") {
    const frames = sprites.typing[dir as Direction]
    return frames ? frames[frame % frames.length]! : fallback
  }
  if (state === "walk") {
    const frames = sprites.walk[dir as Direction]
    return frames ? frames[frame % frames.length]! : fallback
  }
  if (state === "think") {
    const frames = sprites.thinking[dir as Direction]
    return frames ? frames[frame % frames.length]! : fallback
  }
  if (state === "error") {
    // Use typing frames during error shake
    const frames = sprites.typing[dir as Direction]
    return frames ? frames[frame % frames.length]! : fallback
  }
  if (state === "celebrate") {
    // Standing pose (walk frame 1)
    const walkFrames = sprites.walk[dir as Direction]
    return walkFrames ? walkFrames[1]! : fallback
  }
  // idle
  const walkFrames = sprites.walk[dir as Direction]
  return walkFrames ? walkFrames[1]! : fallback
}

// ── Furniture sprites — from pixel-agents (MIT) ──────────────

const _ = ""

/** Modern desk: 32x20 pixels — ¾ view: surface on top, front panel visible below
 *  Short enough that agent behind desk shows head + torso above it */
export const DESK_SPRITE: SpriteData = (() => {
  const E = "#4A4A4A" // edge
  const S = "#606060" // surface
  const L = "#707070" // light accent
  const M = "#888888" // metal leg
  const D = "#333333" // dark edge
  const F = "#3A3A3A" // front panel
  const rows: string[][] = []
  // Row 0: back edge
  rows.push([_, ...new Array(30).fill(E), _])
  // Rows 1-2: surface top (light = near edge in ¾ view)
  rows.push([_, E, ...new Array(28).fill(L), E, _])
  rows.push([_, E, ...new Array(28).fill(S), E, _])
  // Row 3: surface divider
  rows.push([_, D, ...new Array(28).fill(S), D, _])
  // Rows 4-6: main surface
  for (let r = 0; r < 3; r++) {
    rows.push([_, E, ...new Array(28).fill(S), E, _])
  }
  // Row 7: front edge of surface
  rows.push([_, D, ...new Array(28).fill(E), D, _])
  // Rows 8-13: front panel (vertical face visible in ¾ view)
  for (let r = 0; r < 6; r++) {
    rows.push([_, E, ...new Array(28).fill(F), E, _])
  }
  // Row 14: bottom edge
  rows.push([_, D, ...new Array(28).fill(D), D, _])
  // Rows 15-19: legs at corners
  for (let r = 0; r < 5; r++) {
    const row = new Array(32).fill(_) as string[]
    row[2] = M
    row[3] = M
    row[28] = M
    row[29] = M
    rows.push(row)
  }
  return rows
})()

/** Plant in pot: 16x24 */
export const PLANT_SPRITE: SpriteData = (() => {
  const G = "#3D8B37"
  const D = "#2D6B27"
  const T = "#6B4E0A"
  const P = "#B85C3A"
  const R = "#8B4422"
  return [
    [_, _, _, _, _, _, G, G, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, G, G, G, G, _, _, _, _, _, _, _],
    [_, _, _, _, G, G, D, G, G, G, _, _, _, _, _, _],
    [_, _, _, G, G, D, G, G, D, G, G, _, _, _, _, _],
    [_, _, G, G, G, G, G, G, G, G, G, G, _, _, _, _],
    [_, G, G, D, G, G, G, G, G, G, D, G, G, _, _, _],
    [_, G, G, G, G, D, G, G, D, G, G, G, G, _, _, _],
    [_, _, G, G, G, G, G, G, G, G, G, G, _, _, _, _],
    [_, _, _, G, G, G, D, G, G, G, G, _, _, _, _, _],
    [_, _, _, _, G, G, G, G, G, G, _, _, _, _, _, _],
    [_, _, _, _, _, G, G, G, G, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, T, T, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, T, T, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, T, T, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, R, R, R, R, R, _, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, _, R, P, P, P, R, _, _, _, _, _, _],
    [_, _, _, _, _, _, R, R, R, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Bookshelf: 16x32 (1 tile wide, 2 tiles tall) */
export const BOOKSHELF_SPRITE: SpriteData = (() => {
  const W = "#8B6914"
  const D = "#6B4E0A"
  const R = "#CC4444"
  const B = "#4477AA"
  const G = "#44AA66"
  const Y = "#CCAA33"
  const P = "#9955AA"
  return [
    [_, W, W, W, W, W, W, W, W, W, W, W, W, W, W, _],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, D, R, R, B, B, G, G, Y, Y, R, R, B, B, D, W],
    [W, D, R, R, B, B, G, G, Y, Y, R, R, B, B, D, W],
    [W, D, R, R, B, B, G, G, Y, Y, R, R, B, B, D, W],
    [W, D, R, R, B, B, G, G, Y, Y, R, R, B, B, D, W],
    [W, D, R, R, B, B, G, G, Y, Y, R, R, B, B, D, W],
    [W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, D, P, P, Y, Y, B, B, G, G, P, P, R, R, D, W],
    [W, D, P, P, Y, Y, B, B, G, G, P, P, R, R, D, W],
    [W, D, P, P, Y, Y, B, B, G, G, P, P, R, R, D, W],
    [W, D, P, P, Y, Y, B, B, G, G, P, P, R, R, D, W],
    [W, D, P, P, Y, Y, B, B, G, G, P, P, R, R, D, W],
    [W, D, P, P, Y, Y, B, B, G, G, P, P, R, R, D, W],
    [W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, D, G, G, R, R, P, P, B, B, Y, Y, G, G, D, W],
    [W, D, G, G, R, R, P, P, B, B, Y, Y, G, G, D, W],
    [W, D, G, G, R, R, P, P, B, B, Y, Y, G, G, D, W],
    [W, D, G, G, R, R, P, P, B, B, Y, Y, G, G, D, W],
    [W, D, G, G, R, R, P, P, B, B, Y, Y, G, G, D, W],
    [W, D, G, G, R, R, P, P, B, B, Y, Y, G, G, D, W],
    [W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, D, D, D, D, D, D, D, D, D, D, D, D, D, D, W],
    [W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W],
    [_, W, W, W, W, W, W, W, W, W, W, W, W, W, W, _],
  ]
})()

/** Water cooler: 16x24 */
export const WATER_COOLER_SPRITE: SpriteData = (() => {
  const W = "#CCDDEE"
  const L = "#88BBDD"
  const D = "#999999"
  const B = "#666666"
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, D, L, L, L, L, L, L, D, _, _, _, _],
    [_, _, _, _, D, L, L, L, L, L, L, D, _, _, _, _],
    [_, _, _, _, D, L, L, L, L, L, L, D, _, _, _, _],
    [_, _, _, _, D, L, L, L, L, L, L, D, _, _, _, _],
    [_, _, _, _, D, L, L, L, L, L, L, D, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, D, W, W, W, W, D, _, _, _, _, _],
    [_, _, _, _, _, D, W, W, W, W, D, _, _, _, _, _],
    [_, _, _, _, _, D, W, W, W, W, D, _, _, _, _, _],
    [_, _, _, _, _, D, W, W, W, W, D, _, _, _, _, _],
    [_, _, _, _, _, D, W, W, W, W, D, _, _, _, _, _],
    [_, _, _, _, D, D, W, W, W, W, D, D, _, _, _, _],
    [_, _, _, _, D, W, W, W, W, W, W, D, _, _, _, _],
    [_, _, _, _, D, W, W, W, W, W, W, D, _, _, _, _],
    [_, _, _, _, D, D, D, D, D, D, D, D, _, _, _, _],
    [_, _, _, _, _, D, B, B, B, B, D, _, _, _, _, _],
    [_, _, _, _, _, D, B, B, B, B, D, _, _, _, _, _],
    [_, _, _, _, _, D, B, B, B, B, D, _, _, _, _, _],
    [_, _, _, _, D, D, B, B, B, B, D, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, D, D, D, D, D, D, D, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Whiteboard: 32x16 (2 tiles wide, 1 tile tall) — hangs on wall */
export const WHITEBOARD_SPRITE: SpriteData = (() => {
  const F = "#AAAAAA"
  const W = "#EEEEFF"
  const M = "#CC4444"
  const B = "#4477AA"
  return [
    [
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
    ],
    [
      _,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      M,
      M,
      M,
      W,
      W,
      W,
      W,
      W,
      B,
      B,
      B,
      B,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      M,
      W,
      W,
      W,
      W,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      B,
      B,
      W,
      W,
      M,
      W,
      W,
      W,
      W,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      W,
      W,
      M,
      M,
      M,
      M,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      B,
      B,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      B,
      B,
      B,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      M,
      M,
      M,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      M,
      M,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      B,
      B,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      W,
      W,
      W,
      W,
      B,
      B,
      B,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      M,
      M,
      M,
      M,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      W,
      F,
      _,
    ],
    [
      _,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      F,
      _,
    ],
    [
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
    ],
    [
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
    ],
    [
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
      _,
    ],
  ]
})()

/** Chair: 16x16 — top-down desk chair */
export const CHAIR_SPRITE: SpriteData = (() => {
  const W = "#8B6914"
  const D = "#6B4E0A"
  const B = "#5C3D0A"
  const S = "#A07828"
  return [
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, _, D, W, W, D, _, _, _, _, _, _],
    [_, _, _, _, _, _, D, W, W, D, _, _, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, D, _, _, _, _, D, _, _, _, _, _],
    [_, _, _, _, _, D, _, _, _, _, D, _, _, _, _, _],
  ]
})()

/** Laptop (silver): 16x16 — ¾ view: keyboard at top (toward agent), screen at bottom (toward camera) */
export const LAPTOP_SPRITE: SpriteData = (() => {
  // ¾ view from behind: camera sees the BACK of the lid (shell/logo side)
  // Top = hinge (near agent), Bottom = lid bottom edge (toward camera)
  const A = "#C0C0C8" // aluminum shell
  const S = "#AAAAAE" // shell edge
  const D = "#B0B0B8" // shell detail/logo area
  const H = "#555558" // hinge
  const E = "#999EA0" // bottom edge of lid
  const B = "#888890" // base edge (thin, visible behind hinge)
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, B, B, B, B, B, B, B, B, B, B, _, _, _],
    [_, _, _, H, H, H, H, H, H, H, H, H, H, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, D, D, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, D, D, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, E, E, E, E, E, E, E, E, E, E, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Desk lamp: 16x16 — top-down lamp with light cone */
export const LAMP_SPRITE: SpriteData = (() => {
  const Y = "#FFDD55"
  const L = "#FFEE88"
  const D = "#888888"
  const B = "#555555"
  const G = "#FFFFCC"
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, G, G, G, G, _, _, _, _, _, _],
    [_, _, _, _, _, G, Y, Y, Y, Y, G, _, _, _, _, _],
    [_, _, _, _, G, Y, Y, L, L, Y, Y, G, _, _, _, _],
    [_, _, _, _, Y, Y, L, L, L, L, Y, Y, _, _, _, _],
    [_, _, _, _, Y, Y, L, L, L, L, Y, Y, _, _, _, _],
    [_, _, _, _, _, Y, Y, Y, Y, Y, Y, _, _, _, _, _],
    [_, _, _, _, _, _, D, D, D, D, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, D, D, D, D, _, _, _, _, _, _],
    [_, _, _, _, _, B, B, B, B, B, B, _, _, _, _, _],
    [_, _, _, _, _, B, B, B, B, B, B, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

// ── DESK VARIANTS (3 modern types) ───────────────────────────

/** Modern dark desk — black matte */
export const DESK_DARK_SPRITE: SpriteData = (() => {
  const E = "#2A2A2A"
  const S = "#3A3A3A"
  const L = "#484848"
  const M = "#666666"
  const D = "#1A1A1A"
  const F = "#222222"
  const rows: string[][] = []
  rows.push([_, ...new Array(30).fill(E), _])
  rows.push([_, E, ...new Array(28).fill(L), E, _])
  rows.push([_, E, ...new Array(28).fill(S), E, _])
  rows.push([_, D, ...new Array(28).fill(S), D, _])
  for (let r = 0; r < 3; r++) {
    rows.push([_, E, ...new Array(28).fill(S), E, _])
  }
  rows.push([_, D, ...new Array(28).fill(E), D, _])
  for (let r = 0; r < 6; r++) {
    rows.push([_, E, ...new Array(28).fill(F), E, _])
  }
  rows.push([_, D, ...new Array(28).fill(D), D, _])
  for (let r = 0; r < 5; r++) {
    const row = new Array(32).fill(_) as string[]
    row[2] = M
    row[3] = M
    row[28] = M
    row[29] = M
    rows.push(row)
  }
  return rows
})()

/** Modern white desk — clean white */
export const DESK_WHITE_SPRITE: SpriteData = (() => {
  const E = "#BBBBBB"
  const S = "#D8D8D8"
  const L = "#E8E8E8"
  const M = "#999999"
  const D = "#AAAAAA"
  const F = "#C0C0C0"
  const rows: string[][] = []
  rows.push([_, ...new Array(30).fill(E), _])
  rows.push([_, E, ...new Array(28).fill(L), E, _])
  rows.push([_, E, ...new Array(28).fill(S), E, _])
  rows.push([_, D, ...new Array(28).fill(S), D, _])
  for (let r = 0; r < 3; r++) {
    rows.push([_, E, ...new Array(28).fill(S), E, _])
  }
  rows.push([_, D, ...new Array(28).fill(E), D, _])
  for (let r = 0; r < 6; r++) {
    rows.push([_, E, ...new Array(28).fill(F), E, _])
  }
  rows.push([_, D, ...new Array(28).fill(D), D, _])
  for (let r = 0; r < 5; r++) {
    const row = new Array(32).fill(_) as string[]
    row[2] = M
    row[3] = M
    row[28] = M
    row[29] = M
    rows.push(row)
  }
  return rows
})()

export const DESK_VARIANTS: SpriteData[] = [
  DESK_SPRITE,
  DESK_DARK_SPRITE,
  DESK_WHITE_SPRITE,
]

// ── NEW OFFICE OBJECTS ───────────────────────────────────────

/** Flat-screen TV: 16x16 — wall-mounted */
export const TV_SPRITE: SpriteData = (() => {
  const F = "#1A1A1A" // thin bezel
  const S = "#111118" // inner bezel
  const B = "#224477" // dark screen
  const L = "#3366AA" // screen highlight
  const W = "#5588CC" // bright spot
  const M = "#333333" // wall mount bracket
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, M, M, _, _, _, _, _, _, _],
    [_, F, F, F, F, F, F, F, F, F, F, F, F, F, F, _],
    [_, F, S, S, S, S, S, S, S, S, S, S, S, S, F, _],
    [_, F, S, B, B, L, L, L, L, B, B, B, B, S, F, _],
    [_, F, S, B, L, L, W, W, L, L, B, B, B, S, F, _],
    [_, F, S, B, L, W, W, L, L, L, B, B, B, S, F, _],
    [_, F, S, B, B, L, L, L, L, B, B, B, B, S, F, _],
    [_, F, S, B, B, B, L, L, B, B, B, B, B, S, F, _],
    [_, F, S, B, B, B, B, B, B, B, B, L, B, S, F, _],
    [_, F, S, B, B, B, B, B, B, B, B, B, B, S, F, _],
    [_, F, S, S, S, S, S, S, S, S, S, S, S, S, F, _],
    [_, F, F, F, F, F, F, F, F, F, F, F, F, F, F, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Coffee machine: 16x24 — black body with red button */
export const COFFEE_MACHINE_SPRITE: SpriteData = (() => {
  const B = "#333333"
  const D = "#222222"
  const S = "#555555"
  const R = "#CC3333"
  const G = "#888888"
  const W = "#AAAAAA"
  const C = "#6B4422"
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, R, R, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, G, G, G, G, B, D, _, _, _, _],
    [_, _, _, _, D, B, G, W, W, G, B, D, _, _, _, _],
    [_, _, _, _, D, B, G, W, W, G, B, D, _, _, _, _],
    [_, _, _, _, D, B, G, G, G, G, B, D, _, _, _, _],
    [_, _, _, _, D, B, _, C, C, _, B, D, _, _, _, _],
    [_, _, _, _, D, B, _, C, C, _, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, D, B, B, B, B, D, _, _, _, _, _],
    [_, _, _, _, _, D, B, B, B, B, D, _, _, _, _, _],
    [_, _, _, _, D, D, D, D, D, D, D, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, D, D, D, D, D, D, D, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Printer: 16x16 — grey/white body with paper tray */
export const PRINTER_SPRITE: SpriteData = (() => {
  const F = "#777777"
  const B = "#AAAAAA"
  const W = "#DDDDDD"
  const P = "#EEEEEE"
  const D = "#555555"
  const G = "#44AA55"
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, F, F, F, F, F, F, F, F, F, F, _, _, _],
    [_, _, _, F, B, B, B, B, B, B, B, B, F, _, _, _],
    [_, _, _, F, B, W, W, W, W, W, W, B, F, _, _, _],
    [_, _, _, F, B, W, P, P, P, P, W, B, F, _, _, _],
    [_, _, _, F, B, W, P, P, P, P, W, B, F, _, _, _],
    [_, _, _, F, B, W, W, W, W, W, W, B, F, _, _, _],
    [_, _, _, F, B, B, B, B, B, B, B, B, F, _, _, _],
    [_, _, _, F, F, F, F, F, F, F, F, F, F, _, _, _],
    [_, _, F, F, D, D, D, D, D, D, D, D, F, F, _, _],
    [_, _, F, B, B, B, B, B, B, B, B, B, B, F, _, _],
    [_, _, F, B, G, B, B, B, B, B, B, B, B, F, _, _],
    [_, _, F, B, B, B, B, B, B, B, B, B, B, F, _, _],
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, _, _, _, P, P, P, P, P, P, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Router/modem: 16x16 — black box with 2 antennas + LEDs */
export const ROUTER_SPRITE: SpriteData = (() => {
  const B = "#222222"
  const D = "#333333"
  const S = "#444444"
  const G = "#44DD44"
  const Y = "#DDDD44"
  const A = "#555555"
  return [
    [_, _, _, _, B, _, _, _, _, _, _, B, _, _, _, _],
    [_, _, _, _, B, _, _, _, _, _, _, B, _, _, _, _],
    [_, _, _, _, B, _, _, _, _, _, _, B, _, _, _, _],
    [_, _, _, _, B, _, _, _, _, _, _, B, _, _, _, _],
    [_, _, _, _, B, _, _, _, _, _, _, B, _, _, _, _],
    [_, _, _, B, B, B, B, B, B, B, B, B, B, _, _, _],
    [_, _, _, B, D, D, D, D, D, D, D, D, B, _, _, _],
    [_, _, _, B, D, S, S, S, S, S, S, D, B, _, _, _],
    [_, _, _, B, D, S, G, S, Y, S, G, D, B, _, _, _],
    [_, _, _, B, D, S, S, S, S, S, S, D, B, _, _, _],
    [_, _, _, B, D, D, D, D, D, D, D, D, B, _, _, _],
    [_, _, _, B, A, A, A, A, A, A, A, A, B, _, _, _],
    [_, _, _, B, B, B, B, B, B, B, B, B, B, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Smartphone: 16x16 — tiny iPhone style within tile */
export const PHONE_SPRITE: SpriteData = (() => {
  // Very small smartphone lying flat on desk
  const B = "#1a1a1a" // black bezel
  const S = "#3355AA" // screen blue
  const L = "#5577CC" // screen light
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, B, B, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, B, S, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, B, S, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, B, L, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, B, S, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, B, B, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

// ── CHAIR VARIANTS (4 types) ────────────────────────────────

/** Blue office chair */
export const CHAIR_BLUE_SPRITE: SpriteData = (() => {
  const W = "#334488"
  const D = "#223366"
  const B = "#1A2244"
  const S = "#4466AA"
  return [
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, _, D, W, W, D, _, _, _, _, _, _],
    [_, _, _, _, _, _, D, W, W, D, _, _, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, D, _, _, _, _, D, _, _, _, _, _],
    [_, _, _, _, _, D, _, _, _, _, D, _, _, _, _, _],
  ]
})()

/** Red office chair */
export const CHAIR_RED_SPRITE: SpriteData = (() => {
  const W = "#883333"
  const D = "#662222"
  const B = "#441A1A"
  const S = "#AA4444"
  return [
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, _, D, W, W, D, _, _, _, _, _, _],
    [_, _, _, _, _, _, D, W, W, D, _, _, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, D, _, _, _, _, D, _, _, _, _, _],
    [_, _, _, _, _, D, _, _, _, _, D, _, _, _, _, _],
  ]
})()

/** Green office chair */
export const CHAIR_GREEN_SPRITE: SpriteData = (() => {
  const W = "#338844"
  const D = "#226633"
  const B = "#1A4422"
  const S = "#44AA55"
  return [
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, S, S, S, S, B, D, _, _, _, _],
    [_, _, _, _, D, B, B, B, B, B, B, D, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, _, D, W, W, D, _, _, _, _, _, _],
    [_, _, _, _, _, _, D, W, W, D, _, _, _, _, _, _],
    [_, _, _, _, _, D, D, D, D, D, D, _, _, _, _, _],
    [_, _, _, _, _, D, _, _, _, _, D, _, _, _, _, _],
    [_, _, _, _, _, D, _, _, _, _, D, _, _, _, _, _],
  ]
})()

export const CHAIR_VARIANTS: SpriteData[] = [
  CHAIR_SPRITE,
  CHAIR_BLUE_SPRITE,
  CHAIR_RED_SPRITE,
  CHAIR_GREEN_SPRITE,
]

// ── MONITOR VARIANTS (3 types) ──────────────────────────────

/** Laptop (black): 16x16 — ¾ view from behind: back of lid (shell) */
export const LAPTOP_BLACK_SPRITE: SpriteData = (() => {
  // ¾ view from behind: camera sees the BACK of the lid (dark shell)
  const A = "#2A2A2E" // dark shell
  const S = "#1E1E22" // shell edge
  const D = "#333338" // shell detail/logo area
  const H = "#444448" // hinge
  const E = "#222226" // bottom edge of lid
  const B = "#3A3A3E" // base edge
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, B, B, B, B, B, B, B, B, B, B, _, _, _],
    [_, _, _, H, H, H, H, H, H, H, H, H, H, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, D, D, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, D, D, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, E, E, E, E, E, E, E, E, E, E, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Laptop (white): 16x16 — ¾ view from behind: back of lid (white shell) */
export const LAPTOP_WHITE_SPRITE: SpriteData = (() => {
  // ¾ view from behind: camera sees the BACK of the lid (white shell)
  const A = "#E8E8EC" // white shell
  const S = "#D0D0D4" // shell edge
  const D = "#D8D8DC" // shell detail/logo area
  const H = "#AAAAAE" // hinge
  const E = "#C8C8CC" // bottom edge of lid
  const B = "#BBBBC0" // base edge
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, B, B, B, B, B, B, B, B, B, B, _, _, _],
    [_, _, _, H, H, H, H, H, H, H, H, H, H, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, D, D, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, D, D, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, S, A, A, A, A, A, A, A, A, S, _, _, _],
    [_, _, _, E, E, E, E, E, E, E, E, E, E, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

export const LAPTOP_VARIANTS: SpriteData[] = [
  LAPTOP_SPRITE,
  LAPTOP_BLACK_SPRITE,
  LAPTOP_WHITE_SPRITE,
]

// ── LAMP VARIANTS (4 types) ─────────────────────────────────

/** Green desk lamp */
export const LAMP_GREEN_SPRITE: SpriteData = (() => {
  const Y = "#55DD55"
  const L = "#88FF88"
  const D = "#888888"
  const B = "#555555"
  const G = "#CCFFCC"
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, G, G, G, G, _, _, _, _, _, _],
    [_, _, _, _, _, G, Y, Y, Y, Y, G, _, _, _, _, _],
    [_, _, _, _, G, Y, Y, L, L, Y, Y, G, _, _, _, _],
    [_, _, _, _, Y, Y, L, L, L, L, Y, Y, _, _, _, _],
    [_, _, _, _, Y, Y, L, L, L, L, Y, Y, _, _, _, _],
    [_, _, _, _, _, Y, Y, Y, Y, Y, Y, _, _, _, _, _],
    [_, _, _, _, _, _, D, D, D, D, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, D, D, D, D, _, _, _, _, _, _],
    [_, _, _, _, _, B, B, B, B, B, B, _, _, _, _, _],
    [_, _, _, _, _, B, B, B, B, B, B, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Blue desk lamp */
export const LAMP_BLUE_SPRITE: SpriteData = (() => {
  const Y = "#5577DD"
  const L = "#88AAFF"
  const D = "#888888"
  const B = "#555555"
  const G = "#CCDDFF"
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, G, G, G, G, _, _, _, _, _, _],
    [_, _, _, _, _, G, Y, Y, Y, Y, G, _, _, _, _, _],
    [_, _, _, _, G, Y, Y, L, L, Y, Y, G, _, _, _, _],
    [_, _, _, _, Y, Y, L, L, L, L, Y, Y, _, _, _, _],
    [_, _, _, _, Y, Y, L, L, L, L, Y, Y, _, _, _, _],
    [_, _, _, _, _, Y, Y, Y, Y, Y, Y, _, _, _, _, _],
    [_, _, _, _, _, _, D, D, D, D, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, D, D, D, D, _, _, _, _, _, _],
    [_, _, _, _, _, B, B, B, B, B, B, _, _, _, _, _],
    [_, _, _, _, _, B, B, B, B, B, B, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Red desk lamp */
export const LAMP_RED_SPRITE: SpriteData = (() => {
  const Y = "#DD5555"
  const L = "#FF8888"
  const D = "#888888"
  const B = "#555555"
  const G = "#FFCCCC"
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, G, G, G, G, _, _, _, _, _, _],
    [_, _, _, _, _, G, Y, Y, Y, Y, G, _, _, _, _, _],
    [_, _, _, _, G, Y, Y, L, L, Y, Y, G, _, _, _, _],
    [_, _, _, _, Y, Y, L, L, L, L, Y, Y, _, _, _, _],
    [_, _, _, _, Y, Y, L, L, L, L, Y, Y, _, _, _, _],
    [_, _, _, _, _, Y, Y, Y, Y, Y, Y, _, _, _, _, _],
    [_, _, _, _, _, _, D, D, D, D, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, D, D, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, D, D, D, D, _, _, _, _, _, _],
    [_, _, _, _, _, B, B, B, B, B, B, _, _, _, _, _],
    [_, _, _, _, _, B, B, B, B, B, B, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

export const LAMP_VARIANTS: SpriteData[] = [
  LAMP_SPRITE,
  LAMP_GREEN_SPRITE,
  LAMP_BLUE_SPRITE,
  LAMP_RED_SPRITE,
]

// ── WALL ART / PAINTINGS (4 types) — 16x16 ─────────────────

/** Landscape painting — mountains */
export const PAINTING_LANDSCAPE_SPRITE: SpriteData = (() => {
  const F = "#8B6914"
  const S = "#88BBDD"
  const M = "#556655"
  const G = "#44AA55"
  const W = "#EEEEFF"
  const D = "#445544"
  return [
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, F, S, S, S, S, S, S, S, S, S, S, F, _, _],
    [_, _, F, S, S, S, S, W, S, S, S, S, S, F, _, _],
    [_, _, F, S, S, S, M, M, M, S, S, S, S, F, _, _],
    [_, _, F, S, S, M, D, M, D, M, S, S, S, F, _, _],
    [_, _, F, S, M, D, D, M, D, D, M, S, S, F, _, _],
    [_, _, F, S, M, D, D, M, D, D, M, M, S, F, _, _],
    [_, _, F, M, M, D, D, M, D, D, M, M, M, F, _, _],
    [_, _, F, G, G, G, G, M, G, G, G, G, G, F, _, _],
    [_, _, F, G, G, G, G, G, G, G, G, G, G, F, _, _],
    [_, _, F, G, G, G, G, G, G, G, G, G, G, F, _, _],
    [_, _, F, G, G, G, G, G, G, G, G, G, G, F, _, _],
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Abstract painting — colorful squares */
export const PAINTING_ABSTRACT_SPRITE: SpriteData = (() => {
  const F = "#333333"
  const R = "#CC4444"
  const B = "#4477CC"
  const Y = "#DDAA33"
  const G = "#44AA66"
  const W = "#EEEEDD"
  return [
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, F, R, R, R, B, B, B, Y, Y, Y, G, F, _, _],
    [_, _, F, R, R, R, B, B, B, Y, Y, Y, G, F, _, _],
    [_, _, F, R, R, R, B, B, B, Y, Y, Y, G, F, _, _],
    [_, _, F, W, W, Y, G, G, R, R, B, B, W, F, _, _],
    [_, _, F, W, W, Y, G, G, R, R, B, B, W, F, _, _],
    [_, _, F, W, W, Y, G, G, R, R, B, B, W, F, _, _],
    [_, _, F, B, B, R, W, W, Y, G, G, R, B, F, _, _],
    [_, _, F, B, B, R, W, W, Y, G, G, R, B, F, _, _],
    [_, _, F, B, B, R, W, W, Y, G, G, R, B, F, _, _],
    [_, _, F, G, Y, B, R, R, W, W, Y, G, R, F, _, _],
    [_, _, F, G, Y, B, R, R, W, W, Y, G, R, F, _, _],
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Portrait painting — sunset */
export const PAINTING_SUNSET_SPRITE: SpriteData = (() => {
  const F = "#6B4E0A"
  const O = "#DD8833"
  const Y = "#FFCC55"
  const R = "#CC5533"
  const P = "#AA4466"
  const S = "#FFDD88"
  const D = "#884422"
  return [
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, F, P, P, R, R, O, O, R, R, P, P, F, _, _],
    [_, _, F, P, R, R, O, O, O, O, R, R, P, F, _, _],
    [_, _, F, R, R, O, O, Y, Y, O, O, R, R, F, _, _],
    [_, _, F, R, O, O, Y, S, S, Y, O, O, R, F, _, _],
    [_, _, F, O, O, Y, Y, S, S, Y, Y, O, O, F, _, _],
    [_, _, F, O, Y, Y, Y, Y, Y, Y, Y, Y, O, F, _, _],
    [_, _, F, D, D, D, O, O, O, O, D, D, D, F, _, _],
    [_, _, F, D, D, D, D, D, D, D, D, D, D, F, _, _],
    [_, _, F, D, D, D, D, D, D, D, D, D, D, F, _, _],
    [_, _, F, D, D, D, D, D, D, D, D, D, D, F, _, _],
    [_, _, F, D, D, D, D, D, D, D, D, D, D, F, _, _],
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Company logo / certificate — framed text */
export const PAINTING_CERT_SPRITE: SpriteData = (() => {
  const F = "#AA8833"
  const W = "#EEEEDD"
  const T = "#444444"
  const G = "#CCBB88"
  return [
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, F, G, G, G, G, G, G, G, G, G, G, F, _, _],
    [_, _, F, G, W, W, W, W, W, W, W, W, G, F, _, _],
    [_, _, F, G, W, T, T, T, T, T, T, W, G, F, _, _],
    [_, _, F, G, W, W, W, W, W, W, W, W, G, F, _, _],
    [_, _, F, G, W, T, T, T, T, T, T, W, G, F, _, _],
    [_, _, F, G, W, T, T, T, T, T, T, W, G, F, _, _],
    [_, _, F, G, W, W, W, W, W, W, W, W, G, F, _, _],
    [_, _, F, G, W, W, T, T, T, T, W, W, G, F, _, _],
    [_, _, F, G, W, W, W, W, W, W, W, W, G, F, _, _],
    [_, _, F, G, W, W, W, F, F, W, W, W, G, F, _, _],
    [_, _, F, G, G, G, G, G, G, G, G, G, G, F, _, _],
    [_, _, F, F, F, F, F, F, F, F, F, F, F, F, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

export const PAINTING_VARIANTS: SpriteData[] = [
  PAINTING_LANDSCAPE_SPRITE,
  PAINTING_ABSTRACT_SPRITE,
  PAINTING_SUNSET_SPRITE,
  PAINTING_CERT_SPRITE,
]

// ── PLANT VARIANT — taller/different color ──────────────────

/** Flower plant in blue pot */
export const PLANT_FLOWER_SPRITE: SpriteData = (() => {
  const G = "#2D8B47"
  const D = "#1D6B37"
  const T = "#5B3E0A"
  const P = "#3A5C8B"
  const R = "#2A4466"
  const F = "#DD5577"
  const Y = "#FFDD44"
  return [
    [_, _, _, _, _, _, F, Y, F, _, _, _, _, _, _, _],
    [_, _, _, _, _, F, Y, F, Y, F, _, _, _, _, _, _],
    [_, _, _, _, G, G, D, G, G, G, _, _, _, _, _, _],
    [_, _, _, G, G, D, G, G, D, G, G, _, _, _, _, _],
    [_, _, G, G, G, G, G, G, G, G, G, G, _, _, _, _],
    [_, G, G, D, G, G, G, G, G, G, D, G, G, _, _, _],
    [_, G, G, G, G, D, G, G, D, G, G, G, G, _, _, _],
    [_, _, G, G, G, G, G, G, G, G, G, G, _, _, _, _],
    [_, _, _, G, G, G, D, G, G, G, G, _, _, _, _, _],
    [_, _, _, _, G, G, G, G, G, G, _, _, _, _, _, _],
    [_, _, _, _, _, G, G, G, G, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, T, T, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, T, T, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, T, T, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, R, R, R, R, R, _, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, R, P, P, P, P, P, R, _, _, _, _, _],
    [_, _, _, _, _, R, P, P, P, R, _, _, _, _, _, _],
    [_, _, _, _, _, _, R, R, R, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

export const PLANT_VARIANTS: SpriteData[] = [PLANT_SPRITE, PLANT_FLOWER_SPRITE]

// Aliases for backward compatibility
export const MONITOR_SPRITE = LAPTOP_SPRITE
export const TABLET_SPRITE = LAPTOP_SPRITE

// ── Wall sprite loading from PNG ─────────────────────────────

const WALL_PIECE_W = 16
const WALL_PIECE_H = 32
const WALL_GRID_COLS = 4
const WALL_BITMASK_COUNT = 16

let loadedWallSprites: SpriteData[] | null = null

export async function loadWallSprites(): Promise<void> {
  if (loadedWallSprites) return

  try {
    const img = await loadImage("/app/assets/office/walls.png")
    const canvas = document.createElement("canvas")
    canvas.width = img.width
    canvas.height = img.height
    const ctx = canvas.getContext("2d")!
    ctx.drawImage(img, 0, 0)

    const sprites: SpriteData[] = []
    for (let mask = 0; mask < WALL_BITMASK_COUNT; mask++) {
      const ox = (mask % WALL_GRID_COLS) * WALL_PIECE_W
      const oy = Math.floor(mask / WALL_GRID_COLS) * WALL_PIECE_H
      const sprite: string[][] = []
      for (let y = 0; y < WALL_PIECE_H; y++) {
        const row: string[] = []
        for (let x = 0; x < WALL_PIECE_W; x++) {
          const d = ctx.getImageData(ox + x, oy + y, 1, 1).data
          const r = d[0]!,
            g = d[1]!,
            b = d[2]!,
            a = d[3]!
          if (a < 128) {
            row.push("")
          } else {
            row.push(
              `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`
            )
          }
        }
        sprite.push(row)
      }
      sprites.push(sprite)
    }
    loadedWallSprites = sprites
  } catch (err) {
    console.warn("[office] Failed to load walls.png:", err)
  }
}

export function getWallSpriteByMask(mask: number): SpriteData | null {
  if (!loadedWallSprites) return null
  return loadedWallSprites[mask] || null
}

/** Side table: 24x16 — surface near top so item in row-1 appears to rest on it */
export const SIDE_TABLE_SPRITE: SpriteData = (() => {
  const W = "#8B7355" // wood top
  const L = "#9B8365" // wood light
  const D = "#6B5335" // wood dark
  const E = "#5B4325" // edge
  const F = "#7A6345" // front panel
  const G = "#777777" // metal leg
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, E, E, E, E, E, E, E, E, E, E, E, E, E, E, E, E, E, E, E, E, E, E, _],
    [_, E, L, L, L, L, L, L, L, L, L, L, L, L, L, L, L, L, L, L, L, L, E, _],
    [_, E, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, W, E, _],
    [_, E, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, E, _],
    [_, _, E, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, E, _, _],
    [_, _, E, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, E, _, _],
    [_, _, E, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, E, _, _],
    [_, _, E, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, E, _, _],
    [_, _, E, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, F, E, _, _],
    [_, _, _, G, G, _, _, _, _, _, _, _, _, _, _, _, _, _, _, G, G, _, _, _],
    [_, _, _, G, G, _, _, _, _, _, _, _, _, _, _, _, _, _, _, G, G, _, _, _],
    [_, _, _, G, G, _, _, _, _, _, _, _, _, _, _, _, _, _, _, G, G, _, _, _],
    [_, _, _, G, G, _, _, _, _, _, _, _, _, _, _, _, _, _, _, G, G, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()

/** Toolbox: 16x16 — red/orange tool chest with handle + wrench icon */
export const TOOLBOX_SPRITE: SpriteData = (() => {
  const R = "#CC3333" // red body
  const D = "#AA2222" // dark red
  const O = "#DD6633" // orange accent
  const M = "#888888" // metal/silver
  const H = "#666666" // handle dark
  const B = "#222222" // black outline
  const W = "#EEEEEE" // white wrench
  const Y = "#FFCC33" // yellow latch
  return [
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, H, H, H, H, _, _, _, _, _, _],
    [_, _, _, _, _, _, H, M, M, H, _, _, _, _, _, _],
    [_, _, _, B, B, B, B, B, B, B, B, B, B, _, _, _],
    [_, _, _, B, R, R, R, R, R, R, R, R, B, _, _, _],
    [_, _, _, B, R, O, O, O, O, O, O, R, B, _, _, _],
    [_, _, _, B, R, O, W, O, O, W, O, R, B, _, _, _],
    [_, _, _, B, R, O, O, W, W, O, O, R, B, _, _, _],
    [_, _, _, B, R, O, O, W, W, O, O, R, B, _, _, _],
    [_, _, _, B, R, O, W, O, O, W, O, R, B, _, _, _],
    [_, _, _, B, D, D, D, Y, Y, D, D, D, B, _, _, _],
    [_, _, _, B, D, D, D, D, D, D, D, D, B, _, _, _],
    [_, _, _, B, B, B, B, B, B, B, B, B, B, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
    [_, _, _, _, _, _, _, _, _, _, _, _, _, _, _, _],
  ]
})()
