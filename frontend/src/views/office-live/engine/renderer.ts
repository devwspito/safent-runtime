/** Top-down RPG ¾ view renderer */

import type { ActivityBubble } from "./activity-bubbles"
import type { Emote } from "./emotes"
import type { ParticlePool } from "./particles"
import type {
  Character,
  FurnitureInstance,
  Room,
  TileType as TileTypeVal,
} from "./types"
import { TILE_SIZE, TileType } from "./types"

import { drawBubble } from "./activity-bubbles"
import { LABEL_COLOR } from "./constants"
import { drawEmote } from "./emotes"
import { HEADSET_Y_OFFSET_ROWS, getHeadsetCanvas } from "./headset"
import { TD_TILE, getOrigin, gridToScreen, zDepth } from "./iso"
import { FURNITURE_Z_OFFSETS, getFurnitureSprite } from "./iso-furniture"
import { getCachedSprite } from "./sprite-cache"
import { getCharacterSprite, getCharacterSprites } from "./sprites"
import { drawWall, getWallInstances } from "./wall-tiles"

interface ZDrawable {
  zDepth: number
  draw: (ctx: CanvasRenderingContext2D) => void
}

/** Darken a hex color by a factor (0-1, where 0.8 = 20% darker) */
function darkenColor(hex: string, factor: number): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `#${Math.round(r * factor)
    .toString(16)
    .padStart(2, "0")}${Math.round(g * factor)
    .toString(16)
    .padStart(2, "0")}${Math.round(b * factor)
    .toString(16)
    .padStart(2, "0")}`
}

/** Lighten a hex color by a factor (>1, where 1.15 = 15% lighter) */
function lightenColor(hex: string, factor: number): string {
  const r = Math.min(255, Math.round(parseInt(hex.slice(1, 3), 16) * factor))
  const g = Math.min(255, Math.round(parseInt(hex.slice(3, 5), 16) * factor))
  const b = Math.min(255, Math.round(parseInt(hex.slice(5, 7), 16) * factor))
  return `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`
}

export interface A2AMeeting {
  fromId: string
  toId: string
}

export function renderFrame(
  ctx: CanvasRenderingContext2D,
  canvasWidth: number,
  canvasHeight: number,
  tileMap: TileTypeVal[][],
  furniture: FurnitureInstance[],
  characters: Character[],
  rooms: Room[],
  zoom: number,
  panX: number,
  panY: number,
  hoveredAgentId: string | null,
  bubbles?: Map<string, ActivityBubble>,
  frameCount?: number,
  emotes?: Map<string, Emote>,
  particlePool?: ParticlePool,
  a2aMeetings?: Map<string, A2AMeeting>,
  hoveredRoomId?: string | null,
  hoveredFurnitureIdx?: number,
  furnitureBadges?: Map<number, number>
): void {
  // Blueprint grid background
  ctx.fillStyle = "#1a1a2e"
  ctx.fillRect(0, 0, canvasWidth, canvasHeight)
  const gridSpacing = 32 * zoom
  if (gridSpacing > 4) {
    ctx.save()
    ctx.strokeStyle = "rgba(255,255,255,0.03)"
    ctx.lineWidth = 0.5
    const offX = ((panX % gridSpacing) + gridSpacing) % gridSpacing
    const offY = ((panY % gridSpacing) + gridSpacing) % gridSpacing
    for (let x = offX; x < canvasWidth; x += gridSpacing) {
      ctx.beginPath()
      ctx.moveTo(x, 0)
      ctx.lineTo(x, canvasHeight)
      ctx.stroke()
    }
    for (let y = offY; y < canvasHeight; y += gridSpacing) {
      ctx.beginPath()
      ctx.moveTo(0, y)
      ctx.lineTo(canvasWidth, y)
      ctx.stroke()
    }
    ctx.restore()
  }

  const tmRows = tileMap.length
  const tmCols = tmRows > 0 ? tileMap[0]!.length : 0
  if (tmRows === 0 || tmCols === 0) return

  const { originX, originY } = getOrigin(
    canvasWidth,
    canvasHeight,
    tmCols,
    tmRows,
    zoom,
    panX,
    panY
  )

  const tileSize = TD_TILE * zoom

  // Build room color lookup
  const roomColorMap = new Map<string, string>()
  for (const room of rooms) {
    for (let r = room.row; r < room.row + room.height; r++) {
      for (let c = room.col; c < room.col + room.width; c++) {
        roomColorMap.set(`${c},${r}`, room.floorColor)
      }
    }
  }

  // Collect all drawables for z-sorting
  const drawables: ZDrawable[] = []

  // ─── Floor tiles (simple rectangles with checker pattern) ──
  for (let row = 0; row < tmRows; row++) {
    for (let col = 0; col < tmCols; col++) {
      const tile = tileMap[row]![col]!
      if (tile === TileType.VOID) continue
      if (tile === TileType.WALL) continue

      const screen = gridToScreen(col, row, originX, originY, zoom)
      const sx = screen.x
      const sy = screen.y

      const roomColor = roomColorMap.get(`${col},${row}`)
      const baseColor = roomColor || "#6A6A5A"
      const variant = (col + row) % 2
      const tileColor = variant === 0 ? baseColor : darkenColor(baseColor, 0.92)

      drawables.push({
        zDepth: row - 0.5,
        draw: (c) => {
          // Tile fill
          c.fillStyle = tileColor
          c.fillRect(sx, sy, tileSize, tileSize)
          // Subtle border for tile texture
          c.strokeStyle = darkenColor(baseColor, 0.85)
          c.lineWidth = 0.5
          c.strokeRect(sx + 0.5, sy + 0.5, tileSize - 1, tileSize - 1)
        },
      })
    }
  }

  // ─── Wall instances ─────────────────────────────────
  const wallInstances = getWallInstances(tileMap)
  for (const wall of wallInstances) {
    drawables.push({
      zDepth: wall.zDepth,
      draw: (c) => drawWall(c, wall, originX, originY, zoom),
    })
  }

  // ─── Furniture (pixel art sprites) ─────────────────
  for (const f of furniture) {
    const d = zDepth(f.gridCol, f.gridRow)
    const screen = gridToScreen(f.gridCol, f.gridRow, originX, originY, zoom)
    const sx = screen.x
    const sy = screen.y
    const spriteData = getFurnitureSprite(f.type, f.variant)
    const cached = getCachedSprite(spriteData, zoom)

    if (f.type === "desk") {
      // Split desk into back (surface) and front (panel) for sitting illusion
      const splitY = Math.floor(cached.height * 0.35)
      const drawY = sy + tileSize - cached.height

      drawables.push({
        zDepth: d + 0.01,
        draw: (c) => {
          c.drawImage(
            cached,
            0,
            0,
            cached.width,
            splitY,
            sx,
            drawY,
            cached.width,
            splitY
          )
        },
      })

      drawables.push({
        zDepth: d + 1.8,
        draw: (c) => {
          c.drawImage(
            cached,
            0,
            splitY,
            cached.width,
            cached.height - splitY,
            sx,
            drawY + splitY,
            cached.width,
            cached.height - splitY
          )
        },
      })
    } else if (f.type === "laptop" || f.type === "phone") {
      const deskSurfaceOffset = tileSize * 0.65
      drawables.push({
        zDepth: d + 2.0,
        draw: (c) => {
          c.drawImage(
            cached,
            sx,
            sy + tileSize - cached.height - deskSurfaceOffset
          )
        },
      })
    } else {
      const zOff = FURNITURE_Z_OFFSETS[f.type] ?? 0.1
      drawables.push({
        zDepth: d + zOff,
        draw: (c) => {
          c.drawImage(cached, sx, sy + tileSize - cached.height)
        },
      })
    }
  }

  // ─── Interactive furniture overlays (glow + icon labels + badges) ──
  const FURNITURE_ICONS: Record<string, string> = {
    bookshelf: "📚",
    whiteboard: "📋",
    tv: "📺",
    printer: "📄",
    router: "⚙️",
    toolbox: "🔧",
    emptydesk: "➕",
  }
  const FURNITURE_LABELS: Record<string, string> = {
    bookshelf: "Conocimiento",
    whiteboard: "Reglas",
    tv: "Workflows",
    printer: "Auditoría",
    router: "Gateway",
    toolbox: "Herramientas",
    emptydesk: "Nuevo Agente",
  }
  for (let fi = 0; fi < furniture.length; fi++) {
    const f = furniture[fi]!
    if (!INTERACTIVE_FURNITURE.has(f.type)) continue

    const d = zDepth(f.gridCol, f.gridRow)
    const screen = gridToScreen(f.gridCol, f.gridRow, originX, originY, zoom)
    const sx = screen.x
    const sy = screen.y
    const spriteData = getFurnitureSprite(f.type, f.variant)
    const cached = getCachedSprite(spriteData, zoom)
    const drawY = sy + tileSize - cached.height
    const isHovered = hoveredFurnitureIdx === fi
    const fc2 = frameCount ?? 0

    // Hover: pronounced glow + label
    if (isHovered) {
      drawables.push({
        zDepth: d + 3.0,
        draw: (c) => {
          c.save()
          // Bright glow outline
          c.strokeStyle = "rgba(136,187,255,0.5)"
          c.lineWidth = Math.max(2, zoom * 0.7)
          c.shadowColor = "rgba(136,187,255,0.4)"
          c.shadowBlur = zoom * 4
          c.strokeRect(sx - 2, drawY - 2, cached.width + 4, cached.height + 4)
          c.restore()

          // Feature label above sprite
          c.save()
          const label = FURNITURE_LABELS[f.type] || f.type
          const lFontSize = Math.max(8, zoom * 2.5)
          c.font = `bold ${lFontSize}px sans-serif`
          c.textAlign = "center"
          c.textBaseline = "bottom"
          const lx = sx + cached.width / 2
          const ly = drawY - zoom * 2
          const tw = c.measureText(label).width
          const lPad = zoom * 1.2
          // Label bg
          c.fillStyle = "rgba(0,0,0,0.75)"
          c.beginPath()
          c.roundRect(
            lx - tw / 2 - lPad,
            ly - lFontSize - lPad * 0.5,
            tw + lPad * 2,
            lFontSize + lPad,
            lPad * 0.5
          )
          c.fill()
          // Label text
          c.fillStyle = "#FFFFFF"
          c.fillText(label, lx, ly)
          c.restore()
        },
      })
    }

    // Idle: breathing pulse + always-visible icon above sprite
    if (!isHovered) {
      const pulse = 0.08 + Math.sin(fc2 * 0.02 + fi * 1.5) * 0.04
      drawables.push({
        zDepth: d + 3.0,
        draw: (c) => {
          c.save()
          c.globalAlpha = pulse
          c.fillStyle = "#FFFFFF"
          c.fillRect(sx, drawY, cached.width, cached.height)
          c.restore()

          // Small icon above furniture (always visible)
          const icon = FURNITURE_ICONS[f.type]
          if (icon) {
            c.save()
            const isEmptyDesk = f.type === "emptydesk"
            const iconSize = isEmptyDesk
              ? Math.max(14, zoom * 5)
              : Math.max(10, zoom * 3)
            c.font = isEmptyDesk
              ? `bold ${iconSize}px sans-serif`
              : `${iconSize}px sans-serif`
            c.textAlign = "center"
            c.textBaseline = "middle"
            const iconAlpha = isEmptyDesk
              ? 0.7 + Math.sin(fc2 * 0.03 + fi * 2) * 0.2
              : 0.5 + Math.sin(fc2 * 0.025 + fi * 2) * 0.15
            c.globalAlpha = iconAlpha
            if (isEmptyDesk) {
              // Draw centered on the desk sprite
              c.fillStyle = "#FFFFFF"
              c.fillText("+", sx + cached.width / 2, drawY + cached.height / 2)
            } else {
              c.fillText(icon, sx + cached.width / 2, drawY - zoom * 0.5)
            }
            c.restore()
          }
        },
      })
    }

    // Badge counter
    const badgeCount = furnitureBadges?.get(fi)
    if (badgeCount && badgeCount > 0) {
      drawables.push({
        zDepth: 9998,
        draw: (c) => {
          c.save()
          const badgeSize = Math.max(10, zoom * 3)
          const bx = sx + cached.width - badgeSize * 0.3
          const by = drawY - badgeSize * 0.3
          c.beginPath()
          c.arc(bx, by, badgeSize * 0.55, 0, Math.PI * 2)
          c.fillStyle = "#3B82F6"
          c.fill()
          c.strokeStyle = "#1a1a2e"
          c.lineWidth = Math.max(1, zoom * 0.3)
          c.stroke()
          c.fillStyle = "#FFFFFF"
          c.font = `bold ${Math.max(7, zoom * 2)}px sans-serif`
          c.textAlign = "center"
          c.textBaseline = "middle"
          c.fillText(String(badgeCount), bx, by)
          c.restore()
        },
      })
    }
  }

  // ─── Characters ─────────────────────────────────────
  for (const ch of characters) {
    if (ch.status === "offline") continue

    const sprites = getCharacterSprites(ch.palette, ch.hueShift)
    if (!sprites) continue

    const spriteData = getCharacterSprite(sprites, ch.state, ch.dir, ch.frame)
    const cached = getCachedSprite(spriteData, zoom)

    // Convert character world position to screen position
    const gridCol = ch.x / TILE_SIZE
    const gridRow = ch.y / TILE_SIZE
    const screen = gridToScreen(gridCol, gridRow, originX, originY, zoom)

    // Center character on tile
    const charCenterX = screen.x + tileSize / 2
    const charCenterY = screen.y + tileSize / 2

    // Pull agent north (up) so desk front panel covers legs — always applied
    const sittingOffset = -tileSize * 0.7

    // Apply shake (error) and bounce (celebrate) offsets
    const shakeOff = (ch.shakeOffset ?? 0) * zoom
    const bounceOff = (ch.bounceOffset ?? 0) * zoom

    const drawX = Math.round(charCenterX - cached.width / 2 + shakeOff)
    const drawY = Math.round(
      charCenterY - cached.height / 2 + sittingOffset + bounceOff
    )

    const charDepth = zDepth(gridCol, gridRow) + 1.1

    // Shadow + breathing glow (indicates clickable)
    const isAgentHovered = hoveredAgentId === ch.id
    const agentFc = frameCount ?? 0
    const breathAlpha = 0.12 + Math.sin(agentFc * 0.03 + gridCol * 0.5) * 0.06
    drawables.push({
      zDepth: charDepth - 0.05,
      draw: (c) => {
        c.save()
        // Breathing glow ring (always visible — signals clickable)
        if (!isAgentHovered) {
          c.globalAlpha = breathAlpha
          c.fillStyle = "#88BBFF"
          c.beginPath()
          c.ellipse(
            charCenterX,
            charCenterY + tileSize * 0.1,
            tileSize * 0.4,
            tileSize * 0.2,
            0,
            0,
            Math.PI * 2
          )
          c.fill()
        }
        // Shadow
        c.globalAlpha = 0.3
        c.fillStyle = "#000000"
        c.beginPath()
        c.ellipse(
          charCenterX,
          charCenterY + tileSize * 0.3,
          tileSize * 0.3,
          tileSize * 0.15,
          0,
          0,
          Math.PI * 2
        )
        c.fill()
        c.restore()
      },
    })

    // Hover highlight (brighter + larger)
    if (isAgentHovered) {
      drawables.push({
        zDepth: charDepth - 0.1,
        draw: (c) => {
          c.save()
          // Outer glow
          c.globalAlpha = 0.15
          c.fillStyle = "#88CCFF"
          c.beginPath()
          c.ellipse(
            charCenterX,
            charCenterY,
            tileSize * 0.55,
            tileSize * 0.55,
            0,
            0,
            Math.PI * 2
          )
          c.fill()
          // Inner highlight
          c.globalAlpha = 0.3
          c.fillStyle = "#FFFFFF"
          c.beginPath()
          c.ellipse(
            charCenterX,
            charCenterY,
            tileSize * 0.4,
            tileSize * 0.4,
            0,
            0,
            Math.PI * 2
          )
          c.fill()
          c.restore()
        },
      })
    }

    // Character sprite + tint overlay + speed lines
    const chTintColor = ch.tintColor
    const chTintAlpha = ch.tintAlpha ?? 0
    const chIntensity = ch.intensityMultiplier ?? 1
    const fc = frameCount ?? 0
    drawables.push({
      zDepth: charDepth,
      draw: (c) => {
        c.save()
        if (ch.status === "busy") c.globalAlpha = 0.7
        c.drawImage(cached, drawX, drawY)

        // Red tint overlay for error state
        if (chTintColor && chTintAlpha > 0) {
          c.globalAlpha = chTintAlpha
          c.fillStyle = chTintColor
          c.fillRect(drawX, drawY, cached.width, cached.height)
        }
        c.restore()

        // Speed lines for intense typing (tool_call)
        if (chIntensity > 1 && fc % 6 < 3) {
          c.save()
          c.strokeStyle = "rgba(255,255,200,0.55)"
          c.lineWidth = Math.max(1, zoom * 0.4)
          const lx = drawX + cached.width + zoom * 1
          const ly = drawY + cached.height * 0.3
          for (let i = 0; i < 3; i++) {
            const offY = i * zoom * 2.5
            c.beginPath()
            c.moveTo(lx, ly + offY)
            c.lineTo(lx + zoom * 2.5, ly + offY - zoom * 1.5)
            c.stroke()
          }
          c.restore()
        }
      },
    })

    // Headset overlay for A2A calls
    if (ch.meetTarget) {
      const hsCanvas = getHeadsetCanvas(ch.dir, zoom)
      const headsetDrawX = drawX // Same X as character (both 16px wide with outline)
      const headsetDrawY = drawY + HEADSET_Y_OFFSET_ROWS * zoom
      drawables.push({
        zDepth: charDepth + 0.05,
        draw: (c) => {
          c.drawImage(hsCanvas, headsetDrawX, headsetDrawY)
        },
      })
    }

    // Name label
    drawables.push({
      zDepth: charDepth + 0.1,
      draw: (c) => {
        const labelX = charCenterX
        const labelY = drawY - 2 * zoom
        const fontSize = Math.max(8, zoom * 3)

        c.save()
        c.font = `${fontSize}px monospace`
        c.textAlign = "center"
        c.textBaseline = "bottom"

        const textW = c.measureText(ch.agentName).width
        c.fillStyle = "rgba(0,0,0,0.6)"
        const pad = zoom * 1.5
        c.beginPath()
        c.roundRect(
          labelX - textW / 2 - pad,
          labelY - fontSize - pad * 0.5,
          textW + pad * 2,
          fontSize + pad,
          pad * 0.5
        )
        c.fill()

        c.fillStyle = LABEL_COLOR
        c.fillText(ch.agentName, labelX, labelY)

        // Crown icon for orchestrator agents
        if (ch.isOrchestrator) {
          const crownSize = Math.max(8, fontSize * 0.9)
          const crownX = labelX - textW / 2 - crownSize - 2
          const crownY = labelY - fontSize - pad * 0.2
          c.fillStyle = "#F59E0B"
          c.font = `${crownSize}px sans-serif`
          c.textAlign = "left"
          c.textBaseline = "bottom"
          c.fillText("👑", crownX, crownY + crownSize)
          c.font = `${fontSize}px monospace`
          c.textAlign = "center"
          c.textBaseline = "bottom"
        }

        const dotR = Math.max(2, zoom * 0.8)
        const dotX = labelX + textW / 2 + dotR + 2
        const dotY = labelY - fontSize / 2
        c.beginPath()
        c.arc(dotX, dotY, dotR, 0, Math.PI * 2)
        c.fillStyle =
          ch.status === "online"
            ? "#44CC66"
            : ch.status === "busy"
              ? "#DDAA33"
              : "#888888"
        c.fill()

        c.restore()
      },
    })

    // Activity bubble
    const bubble = bubbles?.get(ch.id)
    if (bubble && fc >= 0) {
      drawables.push({
        zDepth: charDepth + 0.2,
        draw: (c) => {
          drawBubble(c, bubble, charCenterX, drawY, zoom, fc)
        },
      })
    }

    // Emote (above bubble)
    const emote = emotes?.get(ch.id)
    if (emote) {
      drawables.push({
        zDepth: charDepth + 0.3,
        draw: (c) => {
          drawEmote(c, emote, charCenterX, drawY, zoom, !!bubble)
        },
      })
    }
  }

  // ─── Room ambiance overlay ──────────────────────────
  for (const room of rooms) {
    const topLeft = gridToScreen(room.col, room.row, originX, originY, zoom)
    const roomW = room.width * tileSize
    const roomH = room.height * tileSize
    const ambDepth = zDepth(room.col, room.row) - 0.9

    drawables.push({
      zDepth: ambDepth,
      draw: (c) => {
        c.save()
        c.globalAlpha = 0.08
        c.fillStyle = room.floorColor
        c.fillRect(topLeft.x, topLeft.y, roomW, roomH)
        c.restore()

        // Room border
        c.save()
        c.strokeStyle = "rgba(0,0,0,0.15)"
        c.lineWidth = Math.max(1, zoom * 0.5)
        c.strokeRect(topLeft.x, topLeft.y, roomW, roomH)
        c.restore()
      },
    })
  }

  // ─── Room labels (sign above room) ─────────────────
  for (const room of rooms) {
    const isRoomHovered = hoveredRoomId === room.departmentId
    // Position: centered horizontally, above the top wall
    const labelScreen = gridToScreen(
      room.col + room.width / 2,
      room.row - 0.3,
      originX,
      originY,
      zoom
    )
    const labelX = labelScreen.x + tileSize / 2
    const labelY = labelScreen.y + tileSize / 2
    const fontSize = Math.max(10, zoom * 3.2)
    // Draw on top of everything (high z-depth = last to render)
    const labelDepth = 9999

    drawables.push({
      zDepth: labelDepth,
      draw: (c) => {
        c.save()

        // Scale up slightly on hover for emphasis
        if (isRoomHovered) {
          c.translate(labelX, labelY)
          c.scale(1.06, 1.06)
          c.translate(-labelX, -labelY)
        }

        c.font = `bold ${fontSize}px monospace`
        // Add a small arrow icon "▸" after the name to hint clickable
        const displayText = room.departmentName
        const textW = c.measureText(displayText).width
        const arrowW = c.measureText(" ▸").width
        const totalTextW = textW + arrowW
        const padX = zoom * 3
        const padY = zoom * 1.8
        const signW = totalTextW + padX * 2
        const signH = fontSize + padY * 2
        const signX = labelX - signW / 2
        const signY = labelY - signH / 2
        const radius = zoom * 1.5

        // Hover glow
        if (isRoomHovered) {
          c.shadowColor = "rgba(136,187,255,0.5)"
          c.shadowBlur = zoom * 5
        }

        // Sign background with subtle gradient
        const baseColor = room.floorColor || "#5A5A4A"
        const topColor = isRoomHovered
          ? lightenColor(baseColor, 1.2)
          : baseColor
        const grad = c.createLinearGradient(signX, signY, signX, signY + signH)
        grad.addColorStop(0, topColor)
        grad.addColorStop(1, darkenColor(baseColor, 0.75))
        c.fillStyle = grad
        c.beginPath()
        c.roundRect(signX, signY, signW, signH, radius)
        c.fill()

        // Border
        c.shadowBlur = 0
        c.strokeStyle = isRoomHovered
          ? "rgba(136,187,255,0.5)"
          : darkenColor(baseColor, 0.5)
        c.lineWidth = Math.max(1, zoom * (isRoomHovered ? 0.7 : 0.4))
        c.stroke()

        // Inner highlight line at top
        c.strokeStyle = `rgba(255,255,255,${isRoomHovered ? 0.35 : 0.15})`
        c.lineWidth = Math.max(0.5, zoom * 0.2)
        c.beginPath()
        c.moveTo(signX + radius, signY + zoom * 0.4)
        c.lineTo(signX + signW - radius, signY + zoom * 0.4)
        c.stroke()

        // Text with slight shadow
        c.textAlign = "center"
        c.textBaseline = "middle"
        c.fillStyle = "rgba(0,0,0,0.4)"
        c.fillText(displayText, labelX - arrowW / 2 + 0.5, labelY + 0.5)
        c.fillStyle = "#FFFFFF"
        c.fillText(displayText, labelX - arrowW / 2, labelY)

        // Arrow indicator (subtler when idle, brighter on hover)
        c.fillStyle = isRoomHovered
          ? "rgba(136,187,255,0.9)"
          : "rgba(255,255,255,0.4)"
        c.fillText(" ▸", labelX + textW / 2, labelY)

        c.restore()
      },
    })
  }

  // ─── Sort and draw ──────────────────────────────────
  drawables.sort((a, b) => a.zDepth - b.zDepth)
  for (const d of drawables) {
    d.draw(ctx)
  }

  // ─── Particles ──────────────────────────────────────
  if (particlePool?.hasActive()) {
    ctx.save()
    particlePool.draw(ctx, zoom)
    ctx.restore()
  }

  // ─── A2A call connection lines (on top of everything) ──
  if (a2aMeetings && a2aMeetings.size > 0) {
    const charMap = new Map<string, Character>()
    for (const ch of characters) charMap.set(ch.id, ch)

    const fc = frameCount ?? 0

    for (const [, meeting] of a2aMeetings) {
      const chFrom = charMap.get(meeting.fromId)
      const chTo = charMap.get(meeting.toId)
      if (!chFrom || !chTo) continue

      const fromScreen = gridToScreen(
        chFrom.x / TILE_SIZE,
        chFrom.y / TILE_SIZE,
        originX,
        originY,
        zoom
      )
      const toScreen = gridToScreen(
        chTo.x / TILE_SIZE,
        chTo.y / TILE_SIZE,
        originX,
        originY,
        zoom
      )

      const fromX = fromScreen.x + tileSize / 2
      const fromY = fromScreen.y - tileSize * 0.2
      const toX = toScreen.x + tileSize / 2
      const toY = toScreen.y - tileSize * 0.2

      ctx.save()

      // Animated dashed line
      ctx.setLineDash([zoom * 2, zoom * 1.5])
      ctx.lineDashOffset = -(fc * 0.5)
      ctx.strokeStyle = "rgba(68, 221, 170, 0.6)"
      ctx.lineWidth = Math.max(1.5, zoom * 0.5)
      ctx.beginPath()
      ctx.moveTo(fromX, fromY)
      ctx.lineTo(toX, toY)
      ctx.stroke()

      // Glow dots at endpoints
      for (const [px, py] of [
        [fromX, fromY],
        [toX, toY],
      ] as const) {
        ctx.beginPath()
        ctx.arc(px, py, zoom * 1.2, 0, Math.PI * 2)
        ctx.fillStyle = "rgba(68, 221, 170, 0.8)"
        ctx.fill()
      }

      ctx.restore()
    }
  }
}

// ─── Interactive furniture types that map to features ───
export const INTERACTIVE_FURNITURE = new Set([
  "bookshelf",
  "whiteboard",
  "tv",
  "printer",
  "router",
  "toolbox",
  "emptydesk",
])

/** Hit-test: find which room label is at canvas coordinates */
export function hitTestRoomLabel(
  canvasX: number,
  canvasY: number,
  rooms: Room[],
  canvasWidth: number,
  canvasHeight: number,
  totalCols: number,
  totalRows: number,
  zoom: number,
  panX: number,
  panY: number
): Room | null {
  const { originX, originY } = getOrigin(
    canvasWidth,
    canvasHeight,
    totalCols,
    totalRows,
    zoom,
    panX,
    panY
  )
  const tileSize = TD_TILE * zoom
  const fontSize = Math.max(10, zoom * 3.2)

  // We need a temporary canvas to measure text
  const tempCanvas =
    typeof OffscreenCanvas !== "undefined" ? new OffscreenCanvas(1, 1) : null
  const tempCtx = tempCanvas?.getContext(
    "2d"
  ) as CanvasRenderingContext2D | null

  for (const room of rooms) {
    const labelScreen = gridToScreen(
      room.col + room.width / 2,
      room.row - 0.3,
      originX,
      originY,
      zoom
    )
    const labelX = labelScreen.x + tileSize / 2
    const labelY = labelScreen.y + tileSize / 2

    // Estimate sign dimensions
    const padX = zoom * 3
    const padY = zoom * 1.8
    let textW = room.departmentName.length * fontSize * 0.6 // rough estimate
    if (tempCtx) {
      tempCtx.font = `bold ${fontSize}px monospace`
      textW = tempCtx.measureText(room.departmentName).width
    }
    const signW = textW + padX * 2
    const signH = fontSize + padY * 2
    const signX = labelX - signW / 2
    const signY = labelY - signH / 2

    if (
      canvasX >= signX &&
      canvasX <= signX + signW &&
      canvasY >= signY &&
      canvasY <= signY + signH
    ) {
      return room
    }
  }
  return null
}

/** Hit-test: find which interactive furniture is at canvas coordinates */
export function hitTestFurniture(
  canvasX: number,
  canvasY: number,
  furniture: FurnitureInstance[],
  rooms: Room[],
  canvasWidth: number,
  canvasHeight: number,
  totalCols: number,
  totalRows: number,
  zoom: number,
  panX: number,
  panY: number
): { furniture: FurnitureInstance; room: Room | null } | null {
  const { originX, originY } = getOrigin(
    canvasWidth,
    canvasHeight,
    totalCols,
    totalRows,
    zoom,
    panX,
    panY
  )
  const tileSize = TD_TILE * zoom

  for (let i = furniture.length - 1; i >= 0; i--) {
    const f = furniture[i]!
    if (!INTERACTIVE_FURNITURE.has(f.type)) continue

    const screen = gridToScreen(f.gridCol, f.gridRow, originX, originY, zoom)
    // Furniture bounding box: roughly 1 tile wide, 1.5 tiles tall (sprites extend above tile)
    const fx = screen.x
    const fy = screen.y - tileSize * 0.5
    const fw = tileSize
    const fh = tileSize * 1.5

    if (
      canvasX >= fx &&
      canvasX <= fx + fw &&
      canvasY >= fy &&
      canvasY <= fy + fh
    ) {
      // Find which room this furniture belongs to
      const room =
        rooms.find(
          (r) =>
            f.gridCol >= r.col &&
            f.gridCol < r.col + r.width &&
            f.gridRow >= r.row &&
            f.gridRow < r.row + r.height
        ) || null
      return { furniture: f, room }
    }
  }
  return null
}

/** Hit-test: find which character is at canvas coordinates */
export function hitTestCharacter(
  canvasX: number,
  canvasY: number,
  characters: Character[],
  canvasWidth: number,
  canvasHeight: number,
  totalCols: number,
  totalRows: number,
  zoom: number,
  panX: number,
  panY: number
): Character | null {
  const { originX, originY } = getOrigin(
    canvasWidth,
    canvasHeight,
    totalCols,
    totalRows,
    zoom,
    panX,
    panY
  )

  const tileSize = TD_TILE * zoom

  for (let i = characters.length - 1; i >= 0; i--) {
    const ch = characters[i]!
    if (ch.status === "offline") continue

    const gridCol = ch.x / TILE_SIZE
    const gridRow = ch.y / TILE_SIZE
    const screen = gridToScreen(gridCol, gridRow, originX, originY, zoom)

    // Character is centered on tile
    const cx = screen.x + tileSize / 2
    const cy = screen.y + tileSize / 2
    const halfW = tileSize * 0.6
    const halfH = tileSize * 1.2

    if (
      canvasX >= cx - halfW &&
      canvasX <= cx + halfW &&
      canvasY >= cy - halfH &&
      canvasY <= cy + halfH
    ) {
      return ch
    }
  }

  return null
}
