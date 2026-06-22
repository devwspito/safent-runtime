/** Top-down wall rendering — clean thin lines instead of heavy tiles */

import type { TileType as TileTypeVal } from "./types"
import { TileType } from "./types"

import { TD_TILE, gridToScreen, zDepth } from "./iso"

export interface WallInstance {
  gridCol: number
  gridRow: number
  zDepth: number
  mask: number // bitmask: N=1, S=2, E=4, W=8
}

/** Collect all wall tile positions with neighbor bitmask */
export function getWallInstances(tileMap: TileTypeVal[][]): WallInstance[] {
  const instances: WallInstance[] = []
  const rows = tileMap.length
  const cols = rows > 0 ? tileMap[0]!.length : 0

  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      if (tileMap[r]![c] !== TileType.WALL) continue

      let mask = 0
      if (r > 0 && tileMap[r - 1]![c] === TileType.WALL) mask |= 1
      if (r < rows - 1 && tileMap[r + 1]![c] === TileType.WALL) mask |= 2
      if (c < cols - 1 && tileMap[r]![c + 1] === TileType.WALL) mask |= 4
      if (c > 0 && tileMap[r]![c - 1] === TileType.WALL) mask |= 8

      instances.push({
        gridCol: c,
        gridRow: r,
        zDepth: zDepth(c, r),
        mask,
      })
    }
  }
  return instances
}

/** Draw a wall tile as a subtle solid block with clean edges */
export function drawWall(
  ctx: CanvasRenderingContext2D,
  wall: WallInstance,
  originX: number,
  originY: number,
  zoom: number
): void {
  const screen = gridToScreen(
    wall.gridCol,
    wall.gridRow,
    originX,
    originY,
    zoom
  )
  const tileSize = TD_TILE * zoom

  // Solid wall base — dark blue-gray, same as background but slightly lighter
  ctx.fillStyle = "#222238"
  ctx.fillRect(screen.x, screen.y, tileSize, tileSize)

  // Top highlight edge (gives depth illusion)
  const hasNorth = !!(wall.mask & 1)
  if (!hasNorth) {
    ctx.fillStyle = "#3a3a55"
    ctx.fillRect(screen.x, screen.y, tileSize, Math.max(2, zoom * 0.8))
  }

  // Subtle inner border
  ctx.strokeStyle = "rgba(255,255,255,0.06)"
  ctx.lineWidth = 0.5
  ctx.strokeRect(screen.x + 0.5, screen.y + 0.5, tileSize - 1, tileSize - 1)
}

// Legacy exports for compatibility
export const getIsoWallInstances = getWallInstances
export type IsoWallInstance = WallInstance
export const drawIsoWall = drawWall
