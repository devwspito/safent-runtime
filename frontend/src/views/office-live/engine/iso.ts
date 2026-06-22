/** Top-down coordinate transforms */

export const TD_TILE = 16 // tile size in world pixels (square)

/**
 * Calculate the screen-space origin: the pixel position where grid (0,0)
 * would be rendered, accounting for centering, zoom, and pan.
 */
export function getOrigin(
  canvasW: number,
  canvasH: number,
  totalCols: number,
  totalRows: number,
  zoom: number,
  panX: number,
  panY: number
): { originX: number; originY: number } {
  return {
    originX: canvasW / 2 - (totalCols * TD_TILE * zoom) / 2 + panX,
    originY: canvasH / 2 - (totalRows * TD_TILE * zoom) / 2 + panY,
  }
}

/**
 * Convert grid (col, row) to screen pixels.
 * Returns the TOP-LEFT corner of the tile in screen space.
 */
export function gridToScreen(
  col: number,
  row: number,
  originX: number,
  originY: number,
  zoom: number
): { x: number; y: number } {
  return {
    x: originX + col * TD_TILE * zoom,
    y: originY + row * TD_TILE * zoom,
  }
}

/**
 * Inverse: screen pixels back to fractional grid (col, row).
 */
export function screenToGrid(
  screenX: number,
  screenY: number,
  originX: number,
  originY: number,
  zoom: number
): { col: number; row: number } {
  return {
    col: (screenX - originX) / (TD_TILE * zoom),
    row: (screenY - originY) / (TD_TILE * zoom),
  }
}

/** Depth sort key: higher row = closer to camera = drawn later */
export function zDepth(_col: number, row: number): number {
  return row
}

// Legacy aliases for imports that haven't been updated
export const getIsoOrigin = getOrigin
export const isoDepth = zDepth
