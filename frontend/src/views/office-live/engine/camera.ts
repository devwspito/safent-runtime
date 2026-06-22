/** Camera system — pan + zoom with animated transitions */

import type { Room } from "./types"

import { TD_TILE, getOrigin, gridToScreen } from "./iso"

export interface Camera {
  panX: number
  panY: number
  zoom: number
  isDragging: boolean
  isMouseDown: boolean
  dragStartX: number
  dragStartY: number
  dragStartPanX: number
  dragStartPanY: number
  // Animated transition targets
  targetPanX: number
  targetPanY: number
  targetZoom: number
  isAnimating: boolean
}

const MIN_ZOOM = 0.5
const MAX_ZOOM = 8
const ZOOM_STEP = 0.5
const LERP_SPEED = 8
const SNAP_THRESHOLD = 0.5

export function createCamera(): Camera {
  return {
    panX: 0,
    panY: 0,
    zoom: 4,
    isDragging: false,
    isMouseDown: false,
    dragStartX: 0,
    dragStartY: 0,
    dragStartPanX: 0,
    dragStartPanY: 0,
    targetPanX: 0,
    targetPanY: 0,
    targetZoom: 4,
    isAnimating: false,
  }
}

export function handleWheel(camera: Camera, e: WheelEvent): void {
  const delta = e.deltaY > 0 ? -ZOOM_STEP : ZOOM_STEP
  camera.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, camera.zoom + delta))
  camera.targetZoom = camera.zoom
  camera.isAnimating = false
}

const DRAG_THRESHOLD = 5

export function handleMouseDown(camera: Camera, e: MouseEvent): void {
  if (e.button === 0 || e.button === 1) {
    camera.isMouseDown = true
    camera.isDragging = false
    camera.dragStartX = e.clientX
    camera.dragStartY = e.clientY
    camera.dragStartPanX = camera.panX
    camera.dragStartPanY = camera.panY
    if (e.button === 1) e.preventDefault()
  }
}

export function handleMouseMove(camera: Camera, e: MouseEvent): void {
  if (!camera.isMouseDown) return
  const dx = Math.abs(e.clientX - camera.dragStartX)
  const dy = Math.abs(e.clientY - camera.dragStartY)
  if (!camera.isDragging && (dx > DRAG_THRESHOLD || dy > DRAG_THRESHOLD)) {
    camera.isDragging = true
    camera.isAnimating = false
  }
  if (camera.isDragging) {
    camera.panX = camera.dragStartPanX + (e.clientX - camera.dragStartX)
    camera.panY = camera.dragStartPanY + (e.clientY - camera.dragStartY)
    camera.targetPanX = camera.panX
    camera.targetPanY = camera.panY
  }
}

export function handleMouseUp(camera: Camera): void {
  camera.isMouseDown = false
  camera.isDragging = false
}

export function setZoom(camera: Camera, zoom: number): void {
  camera.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom))
  camera.targetZoom = camera.zoom
}

/** Animate camera towards target values. Call each frame. */
export function animateCamera(camera: Camera, dt: number): void {
  if (!camera.isAnimating) return

  const t = 1 - Math.exp(-LERP_SPEED * dt)

  camera.panX += (camera.targetPanX - camera.panX) * t
  camera.panY += (camera.targetPanY - camera.panY) * t
  camera.zoom += (camera.targetZoom - camera.zoom) * t

  if (
    Math.abs(camera.panX - camera.targetPanX) < SNAP_THRESHOLD &&
    Math.abs(camera.panY - camera.targetPanY) < SNAP_THRESHOLD &&
    Math.abs(camera.zoom - camera.targetZoom) < 0.05
  ) {
    camera.panX = camera.targetPanX
    camera.panY = camera.targetPanY
    camera.zoom = camera.targetZoom
    camera.isAnimating = false
  }
}

/** Pan camera to center on a specific room */
export function panToRoom(
  camera: Camera,
  room: Room,
  canvasW: number,
  canvasH: number,
  totalCols: number,
  totalRows: number
): void {
  const roomCenterCol = room.col + room.width / 2
  const roomCenterRow = room.row + room.height / 2

  // Calculate zoom to fit both width and height with small margin
  const roomScreenW = room.width * TD_TILE
  const roomScreenH = room.height * TD_TILE
  const zoomX = canvasW / (roomScreenW * 1.15)
  const zoomY = canvasH / (roomScreenH * 1.15)
  const fitZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.min(zoomX, zoomY)))

  // At fitZoom, where does the room center appear on screen with panX/Y = 0?
  const { originX, originY } = getOrigin(
    canvasW,
    canvasH,
    totalCols,
    totalRows,
    fitZoom,
    0,
    0
  )
  const roomScreen = gridToScreen(
    roomCenterCol,
    roomCenterRow,
    originX,
    originY,
    fitZoom
  )

  // Pan so room center aligns with canvas center
  camera.targetPanX = canvasW / 2 - roomScreen.x
  camera.targetPanY = canvasH / 2 - roomScreen.y
  camera.targetZoom = fitZoom
  camera.isAnimating = true
}

/** Calculate zoom level that fits the entire map in the viewport */
export function fitZoomForMap(
  canvasW: number,
  canvasH: number,
  totalCols: number,
  totalRows: number
): number {
  if (totalCols === 0 || totalRows === 0) return 4
  const mapW = totalCols * TD_TILE
  const mapH = totalRows * TD_TILE
  // Fit: show entire map with minimal margin
  const zoomX = (canvasW * 0.99) / mapW
  const zoomY = (canvasH * 0.99) / mapH
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.min(zoomX, zoomY)))
}

/** Reset camera to center the full map, auto-fitting zoom */
export function panToAll(
  camera: Camera,
  canvasW?: number,
  canvasH?: number,
  totalCols?: number,
  totalRows?: number
): void {
  camera.targetPanX = 0
  camera.targetPanY = 0
  if (canvasW && canvasH && totalCols && totalRows) {
    camera.targetZoom = fitZoomForMap(canvasW, canvasH, totalCols, totalRows)
  } else {
    camera.targetZoom = 4
  }
  camera.isAnimating = true
}
