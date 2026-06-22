import { useCallback, useEffect, useRef, useState } from 'react'

import type { LumenAgent, LumenRuntimeStatus } from './engine/office-state'
import { OfficeState } from './engine/office-state'
import { animateCamera, createCamera, fitZoomForMap, handleMouseDown, handleMouseMove, handleMouseUp, panToAll } from './engine/camera'
import { startGameLoop } from './engine/game-loop'
import { loadCharacterSprites, loadWallSprites } from './engine/sprites'

interface Props {
  agents: LumenAgent[]
  runtimeStatus: LumenRuntimeStatus
  onAgentClick?: (agentId: string, agentName: string) => void
}

export function OfficeCanvas({ agents, runtimeStatus, onAgentClick }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const officeRef = useRef<OfficeState | null>(null)
  const initialSizeRef = useRef<{ w: number; h: number } | null>(null)
  const spritesLoadedRef = useRef(false)
  const cleanupRef = useRef<(() => void) | null>(null)
  const wasDraggingRef = useRef(false)

  const cameraRef = useRef((() => {
    const cam = createCamera()
    try {
      const raw = localStorage.getItem('lumen:office:camera')
      if (raw) {
        const saved = JSON.parse(raw) as { panX: number; panY: number }
        cam.panX = saved.panX
        cam.targetPanX = saved.panX
        cam.panY = saved.panY
        cam.targetPanY = saved.panY
      }
    } catch { /* ignore */ }
    return cam
  })())

  const [tooltip, setTooltip] = useState<{
    x: number
    y: number
    name: string
    hint?: string
  } | null>(null)

  // ── Mount: sprites + game loop ─────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const office = new OfficeState()
    officeRef.current = office
    const camera = cameraRef.current

    // alive tracks whether this effect instance is still mounted.
    // If the component unmounts before sprites finish loading we bail out
    // before starting the game loop so no rAF leaks.
    let alive = true

    Promise.all([loadCharacterSprites(), loadWallSprites()]).then(() => {
      if (!alive) return

      spritesLoadedRef.current = true
      if (!initialSizeRef.current) {
        initialSizeRef.current = { w: canvas.width, h: canvas.height }
      }
      const { w, h } = initialSizeRef.current
      office.syncFromApi(agents, runtimeStatus, w, h)

      const initZoom = fitZoomForMap(canvas.width, canvas.height, office.totalCols, office.totalRows)
      camera.zoom = initZoom
      camera.targetZoom = initZoom

      let cameraSaveTimer = 0

      const stop = startGameLoop(canvas, {
        update: (dt) => {
          animateCamera(camera, dt)
          office.update(dt)
          office.resolvePendingParticles(canvas.width, canvas.height, camera.zoom, camera.panX, camera.panY)

          cameraSaveTimer += dt
          if (cameraSaveTimer > 2) {
            cameraSaveTimer = 0
            try {
              localStorage.setItem('lumen:office:camera', JSON.stringify({ panX: camera.panX, panY: camera.panY }))
            } catch { /* ignore */ }
          }
        },
        render: (ctx) => {
          office.render(ctx, canvas.width, canvas.height, camera)
        },
      })

      cleanupRef.current = stop
    })

    return () => {
      alive = false
      cleanupRef.current?.()
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Sync agents + status when props change ─────────────────
  useEffect(() => {
    const office = officeRef.current
    const canvas = canvasRef.current
    if (!office || !spritesLoadedRef.current || !canvas || agents.length === 0) return

    const { w, h } = initialSizeRef.current ?? { w: canvas.width, h: canvas.height }
    office.syncFromApi(agents, runtimeStatus, w, h)
    office.applyRuntimeStatus(runtimeStatus)
  }, [agents, runtimeStatus])

  // ── DPR-aware canvas resize ────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current
    const container = containerRef.current
    if (!canvas || !container) return

    const observer = new ResizeObserver(() => {
      const rect = container.getBoundingClientRect()
      const dpr = window.devicePixelRatio || 1
      canvas.width = rect.width * dpr
      canvas.height = rect.height * dpr
      canvas.style.width = `${rect.width}px`
      canvas.style.height = `${rect.height}px`

      // Re-fit zoom on resize if we have layout
      const office = officeRef.current
      if (office && office.totalCols > 0) {
        const z = fitZoomForMap(canvas.width, canvas.height, office.totalCols, office.totalRows)
        cameraRef.current.zoom = z
        cameraRef.current.targetZoom = z
        panToAll(cameraRef.current, canvas.width, canvas.height, office.totalCols, office.totalRows)
      }
    })

    observer.observe(container)
    const rect = container.getBoundingClientRect()
    const dpr = window.devicePixelRatio || 1
    canvas.width = rect.width * dpr
    canvas.height = rect.height * dpr
    canvas.style.width = `${rect.width}px`
    canvas.style.height = `${rect.height}px`

    return () => observer.disconnect()
  }, [])

  // ── Mouse handlers ─────────────────────────────────────────
  const onMouseDown = useCallback((e: React.MouseEvent) => {
    handleMouseDown(cameraRef.current, e.nativeEvent)
  }, [])

  const onMouseMove = useCallback((e: React.MouseEvent) => {
    handleMouseMove(cameraRef.current, e.nativeEvent)

    const canvas = canvasRef.current
    const office = officeRef.current
    if (!canvas || !office || cameraRef.current.isDragging) {
      setTooltip(null)
      return
    }

    const dpr = window.devicePixelRatio || 1
    const rect = canvas.getBoundingClientRect()
    const x = (e.clientX - rect.left) * dpr
    const y = (e.clientY - rect.top) * dpr
    const cam = cameraRef.current

    const agentHit = office.hitTest(x, y, canvas.width, canvas.height, cam)
    if (agentHit) {
      office.hoveredAgentId = agentHit.id
      office.hoveredRoomId = null
      office.hoveredFurnitureIdx = -1
      setTooltip({ x: e.clientX - rect.left, y: e.clientY - rect.top, name: agentHit.agentName })
      canvas.style.cursor = 'pointer'
      return
    }

    const roomHit = office.hitTestRoom(x, y, canvas.width, canvas.height, cam)
    if (roomHit) {
      office.hoveredAgentId = null
      office.hoveredRoomId = roomHit.departmentId
      office.hoveredFurnitureIdx = -1
      setTooltip({ x: e.clientX - rect.left, y: e.clientY - rect.top, name: roomHit.departmentName })
      canvas.style.cursor = 'default'
      return
    }

    office.hoveredAgentId = null
    office.hoveredRoomId = null
    office.hoveredFurnitureIdx = -1
    setTooltip(null)
    canvas.style.cursor = cameraRef.current.isDragging ? 'grabbing' : 'default'
  }, [])

  const onMouseUp2 = useCallback(() => {
    wasDraggingRef.current = cameraRef.current.isDragging
    handleMouseUp(cameraRef.current)
  }, [])

  const onClick = useCallback((e: React.MouseEvent) => {
    if (wasDraggingRef.current) return

    const canvas = canvasRef.current
    const office = officeRef.current
    if (!canvas || !office) return

    const dpr = window.devicePixelRatio || 1
    const rect = canvas.getBoundingClientRect()
    const x = (e.clientX - rect.left) * dpr
    const y = (e.clientY - rect.top) * dpr
    const cam = cameraRef.current

    const agentHit = office.hitTest(x, y, canvas.width, canvas.height, cam)
    if (agentHit) {
      onAgentClick?.(agentHit.id, agentHit.agentName)
    }
  }, [onAgentClick])

  return (
    <div
      ref={containerRef}
      style={{ position: 'relative', width: '100%', height: '100%', overflow: 'hidden', background: '#1a1a2e' }}
    >
      <canvas
        ref={canvasRef}
        style={{ display: 'block' }}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp2}
        onMouseLeave={() => {
          handleMouseUp(cameraRef.current)
          setTooltip(null)
          if (officeRef.current) officeRef.current.hoveredAgentId = null
        }}
        onClick={onClick}
        aria-label="Vista isométrica de la oficina con los agentes"
        role="img"
      />
      {tooltip && (
        <div
          aria-hidden="true"
          style={{
            position: 'absolute',
            pointerEvents: 'none',
            zIndex: 10,
            left: tooltip.x + 12,
            top: tooltip.y - 30,
            background: 'rgba(0,0,0,.8)',
            color: '#fff',
            fontSize: 12,
            padding: '3px 8px',
            borderRadius: 6,
            whiteSpace: 'nowrap',
          }}
        >
          <span style={{ fontWeight: 600 }}>{tooltip.name}</span>
          {tooltip.hint && <span style={{ marginLeft: 8, opacity: 0.65 }}>{tooltip.hint}</span>}
        </div>
      )}
    </div>
  )
}
