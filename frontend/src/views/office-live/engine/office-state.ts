/**
 * Lumen office-state — bridges Lumen's Agent/RuntimeStatus API to the
 * game engine.  All engine logic is unchanged; only the data wiring differs.
 */

import type { ActivityBubble } from "./activity-bubbles"
import type { Camera } from "./camera"
import type { Emote } from "./emotes"
import type {
  Character,
  FurnitureInstance,
  Room,
  Seat,
  TileType as TileTypeVal,
} from "./types"
import { CharacterState, TILE_SIZE } from "./types"

import { updateBubble } from "./activity-bubbles"
import {
  createCharacter,
  triggerCelebrate,
  triggerError,
  triggerThink,
  updateCharacter,
} from "./characters"
import { createEmote, updateEmote } from "./emotes"
import { TD_TILE, getOrigin, gridToScreen } from "./iso"
import { ParticlePool } from "./particles"
import { getWalkableTiles } from "./pathfinder"
import {
  hitTestCharacter,
  hitTestFurniture,
  hitTestRoomLabel,
  renderFrame,
} from "./renderer"
import { buildOfficeLayout } from "./room-builder"

// ── Lumen API shapes (subset of what we need) ──────────────────

export interface LumenAgent {
  id: string
  name: string
  role: string
  primary_mission: string
  color: string
  is_default: boolean
  autonomy_level: string
  /** The real department id from the roster (e.g. "cerebro", "ruflo-escritura", …) */
  department_id: string
  /** The department kind from the roster — drives room role/color */
  department_kind: "cerebro" | "factory" | "custom"
  /** Human-readable department name for the room label */
  department_name: string
}

export interface LumenRuntimeStatus {
  state: string
  active_task_count: number
  active_agent_id?: string
  activity?: Array<{ agent_id: string; tool?: string }>
  ruflo_active?: boolean
  /** Real, still-live delegation edges the backend emits when delegate_task fires. */
  delegations?: Array<{ from: string; to: string }>
}

// ── Internal agent info shape the engine expects ───────────────

interface AgentInfo {
  id: string
  name: string
  department_id: string | null
  status: "online" | "busy" | "offline"
}

interface DepartmentInfo {
  id: string
  name: string
  display_name?: string | null
  role: string | null
  is_director_dept?: boolean
}

// ── Map roster department kind → engine room role ──────────────

const KIND_TO_ROLE: Record<string, string> = {
  cerebro: "executive",
  factory: "research",
  custom: "operations",
}

// ── Derive departments and agent infos from the real roster ────
// One DepartmentInfo per unique department_id in the agent list.
// CEO comes first; the rest are sorted alphabetically by name.

function buildDepartmentsFromAgents(agents: LumenAgent[]): DepartmentInfo[] {
  const seen = new Map<string, DepartmentInfo>()
  for (const a of agents) {
    if (!seen.has(a.department_id)) {
      seen.set(a.department_id, {
        id: a.department_id,
        name: a.department_name,
        role: KIND_TO_ROLE[a.department_kind] ?? "operations",
        is_director_dept: a.department_kind === "cerebro",
      })
    }
  }
  return Array.from(seen.values())
}

function toAgentInfos(
  agents: LumenAgent[],
  status: LumenRuntimeStatus,
): AgentInfo[] {
  const activeIds = new Set<string>()
  if (status.active_agent_id) activeIds.add(status.active_agent_id)
  for (const a of status.activity ?? []) activeIds.add(a.agent_id)

  return agents.map((a) => ({
    id: a.id,
    name: a.name,
    department_id: a.department_id,
    status: activeIds.has(a.id) ? "busy" : "online",
  }))
}

// ── OfficeState (Lumen edition) ────────────────────────────────

export class OfficeState {
  tileMap: TileTypeVal[][] = []
  rooms: Room[] = []
  seats: Map<string, Seat> = new Map()
  furniture: FurnitureInstance[] = []
  characters: Map<string, Character> = new Map()
  walkableTiles: Array<{ col: number; row: number }> = []
  blockedTiles: Set<string> = new Set()
  totalCols = 0
  totalRows = 0
  hoveredAgentId: string | null = null
  hoveredRoomId: string | null = null
  hoveredFurnitureIdx: number = -1
  bubbles: Map<string, ActivityBubble> = new Map()
  emotes: Map<string, Emote> = new Map()
  particlePool = new ParticlePool()
  frameCount = 0
  furnitureBadges: Map<number, number> = new Map()
  furnitureLabels: Record<string, string> | undefined = undefined

  private paletteCounter = 0
  _activeA2A: Map<string, { fromId: string; toId: string }> = new Map()
  /** Orchestrator (cerebro) agent id — fallback origin for delegation edges. */
  _brainId: string | null = null
  private _coffeeTiles: Map<string, { col: number; row: number }> = new Map()
  _pendingParticles: Array<{
    agentId: string
    type: "confetti" | "spark"
    gridCol: number
    gridRow: number
  }> = []

  /** Rebuild layout from Lumen API data */
  syncFromApi(
    agents: LumenAgent[],
    runtimeStatus: LumenRuntimeStatus,
    canvasWidth?: number,
    canvasHeight?: number,
  ): void {
    const depts = buildDepartmentsFromAgents(agents)
    const agentInfos = toAgentInfos(agents, runtimeStatus)
    // Remember the orchestrator so delegation edges can fall back to it when the
    // backend's from-id isn't a seated character (same tolerance as the swarm).
    this._brainId = agents.find((a) => a.department_kind === 'cerebro')?.id ?? null

    // Two-pass layout: tight → viewport-expanded
    const tightLayout = buildOfficeLayout(depts, agentInfos)
    let vpCols: number | undefined
    let vpRows: number | undefined
    if (canvasWidth && canvasHeight && tightLayout.totalCols > 0 && tightLayout.totalRows > 0) {
      const mapW = tightLayout.totalCols * TILE_SIZE
      const mapH = tightLayout.totalRows * TILE_SIZE
      const fitZoom = Math.min(canvasWidth / mapW, canvasHeight / mapH) * 0.99
      vpCols = Math.floor(canvasWidth / (TILE_SIZE * fitZoom)) - 2
      vpRows = Math.floor(canvasHeight / (TILE_SIZE * fitZoom)) - 3
    }
    const layout =
      vpCols && vpRows
        ? buildOfficeLayout(depts, agentInfos, undefined, vpCols, vpRows)
        : tightLayout

    this.tileMap = layout.tileMap
    this.rooms = layout.rooms
    this.furniture = layout.furniture
    this.totalCols = layout.totalCols
    this.totalRows = layout.totalRows

    this.seats.clear()
    for (const seat of layout.seats) this.seats.set(seat.uid, seat)

    this.blockedTiles.clear()
    const blockingTypes = new Set([
      "desk", "bookshelf", "tv", "whiteboard", "coffee", "printer", "cooler", "plant",
    ])
    for (const f of this.furniture) {
      if (blockingTypes.has(f.type)) this.blockedTiles.add(`${f.gridCol},${f.gridRow}`)
    }
    this.walkableTiles = getWalkableTiles(this.tileMap, this.blockedTiles)

    this._coffeeTiles.clear()
    for (const room of this.rooms) {
      const key = room.departmentId || "__none"
      if (this._coffeeTiles.has(key)) continue
      for (const f of this.furniture) {
        if (
          (f.type === "coffee" || f.type === "cooler") &&
          f.gridCol >= room.col &&
          f.gridCol < room.col + room.width &&
          f.gridRow >= room.row &&
          f.gridRow < room.row + room.height
        ) {
          const adj = this._findAdjacentWalkable(f.gridCol, f.gridRow)
          if (adj) { this._coffeeTiles.set(key, adj); break }
        }
      }
    }

    const seatedAgentIds = new Set(layout.seats.map((s) => s.assignedTo))
    const visibleAgents = agentInfos.filter((a) => seatedAgentIds.has(a.id))
    const currentIds = new Set(visibleAgents.map((a) => a.id))
    for (const id of this.characters.keys()) {
      if (!currentIds.has(id)) this.characters.delete(id)
    }

    const orchestratorIds = new Set(agents.filter((a) => a.is_default).map((a) => a.id))

    for (const agentInfo of visibleAgents) {
      const existing = this.characters.get(agentInfo.id)
      if (existing) {
        const newActive = agentInfo.status === "online" || agentInfo.status === "busy"
        if (existing.isActive !== newActive) existing.isActive = newActive
        existing.status = agentInfo.status
        existing.agentName = agentInfo.name
        existing.isOrchestrator = orchestratorIds.has(agentInfo.id)
      } else {
        let assignedSeat: Seat | null = null
        for (const seat of this.seats.values()) {
          if (seat.assignedTo === agentInfo.id) { assignedSeat = seat; break }
        }
        const palette = this.paletteCounter % 6
        this.paletteCounter++
        const character = createCharacter(
          agentInfo.id,
          agentInfo.name,
          agentInfo.department_id,
          palette,
          assignedSeat,
          agentInfo.status,
          orchestratorIds.has(agentInfo.id),
        )
        character.coffeeTarget =
          this._coffeeTiles.get(agentInfo.department_id || "__none") || null
        this.characters.set(agentInfo.id, character)
      }
    }
  }

  /** Drive character working animation from live runtime status.
   *  Call this every time runtimeStatus changes (poll interval). */
  applyRuntimeStatus(status: LumenRuntimeStatus): void {
    const activeIds = new Set<string>()
    if (status.active_agent_id) activeIds.add(status.active_agent_id)
    for (const a of status.activity ?? []) activeIds.add(a.agent_id)

    const toolByAgent = new Map<string, string>()
    for (const a of status.activity ?? []) {
      if (a.tool) toolByAgent.set(a.agent_id, a.tool)
    }

    for (const [id, ch] of this.characters) {
      if (activeIds.has(id)) {
        // Ensure character is active
        if (!ch.isActive) { ch.isActive = true; ch.status = "busy" }
        ch.intensityMultiplier = 2.0
        if (ch.state !== CharacterState.TYPE && ch.state !== CharacterState.CELEBRATE &&
            ch.state !== CharacterState.ERROR) {
          ch.state = CharacterState.TYPE
          ch.frame = 0
          ch.frameTimer = 0
        }
        // Activity bubble
        const tool = toolByAgent.get(id)
        const existing = this.bubbles.get(id)
        const shouldRefresh = !existing || (Date.now() - existing.createdAt) > 3000
        this.bubbles.set(id, {
          agentId: id,
          type: "tool_call",
          text: tool ?? "working",
          createdAt: shouldRefresh ? Date.now() : (existing?.createdAt ?? Date.now()),
          opacity: existing?.opacity ?? 1,
        })
      } else {
        if (ch.status === "busy") {
          ch.status = "online"
          ch.intensityMultiplier = 1
          triggerCelebrate(ch)
          this.emotes.set(id, createEmote(id, "star"))
          this._emitParticlesAtAgent(id, "confetti")
        }
        this.bubbles.delete(id)
      }
    }

    // ── A2A delegation edges ────────────────────────────────────────────────
    // Primary source: status.delegations — the REAL orchestrator→specialist edges
    // the backend emits at the moment delegate_task fires (short TTL). Draw the
    // connection line + phone emotes between them while the edge is live. Both
    // ends must exist on the floor (match_specialist can only return roster ids,
    // but guard anyway). Edge keyed "<fromId>-><toId>" for dedup.
    this._activeA2A.clear()
    for (const d of status.delegations ?? []) {
      if (!d.to || !this.characters.has(d.to)) continue // target must be on the floor
      // Same tolerance as the swarm: if the backend's from-id isn't a seated
      // character (internal id, unseated agent…), route the edge from the brain.
      const from = d.from && this.characters.has(d.from)
        ? d.from
        : (this._brainId && this.characters.has(this._brainId) ? this._brainId : null)
      if (!from || from === d.to) continue
      this._activeA2A.set(`${from}->${d.to}`, { fromId: from, toId: d.to })
    }
    // Legacy fallback: infer edges from active_agent_id (orchestrator) → other
    // active agents. Kept for backends that populate it; a no-op otherwise.
    const orchestratorId = status.active_agent_id
    if (orchestratorId) {
      for (const entry of status.activity ?? []) {
        const specialistId = entry.agent_id
        if (specialistId && specialistId !== orchestratorId) {
          const edgeKey = `${orchestratorId}->${specialistId}`
          if (!this._activeA2A.has(edgeKey)) {
            this._activeA2A.set(edgeKey, { fromId: orchestratorId, toId: specialistId })
          }
        }
      }
    }
  }

  update(dt: number): void {
    this.frameCount++
    for (const ch of this.characters.values()) {
      updateCharacter(ch, dt, this.walkableTiles, this.seats, this.tileMap, this.blockedTiles)
    }
    const now = Date.now()
    for (const [agentId, bubble] of this.bubbles) {
      if (!updateBubble(bubble, now)) this.bubbles.delete(agentId)
    }
    for (const [agentId, emote] of this.emotes) {
      if (!updateEmote(emote, now)) this.emotes.delete(agentId)
    }
    for (const [, call] of this._activeA2A) {
      if (!this.emotes.has(call.fromId)) this.emotes.set(call.fromId, createEmote(call.fromId, "phone"))
      if (!this.emotes.has(call.toId)) this.emotes.set(call.toId, createEmote(call.toId, "phone"))
    }
    this.particlePool.update(dt)
  }

  render(ctx: CanvasRenderingContext2D, canvasWidth: number, canvasHeight: number, camera: Camera): void {
    renderFrame(
      ctx, canvasWidth, canvasHeight, this.tileMap, this.furniture,
      Array.from(this.characters.values()), this.rooms,
      camera.zoom, camera.panX, camera.panY,
      this.hoveredAgentId, this.bubbles, this.frameCount,
      this.emotes, this.particlePool, this._activeA2A,
      this.hoveredRoomId, this.hoveredFurnitureIdx, this.furnitureBadges,
      this.furnitureLabels,
    )
  }

  hitTest(canvasX: number, canvasY: number, canvasWidth: number, canvasHeight: number, camera: Camera): Character | null {
    return hitTestCharacter(canvasX, canvasY, Array.from(this.characters.values()),
      canvasWidth, canvasHeight, this.totalCols, this.totalRows, camera.zoom, camera.panX, camera.panY)
  }

  hitTestRoom(canvasX: number, canvasY: number, canvasWidth: number, canvasHeight: number, camera: Camera): Room | null {
    return hitTestRoomLabel(canvasX, canvasY, this.rooms, canvasWidth, canvasHeight,
      this.totalCols, this.totalRows, camera.zoom, camera.panX, camera.panY)
  }

  hitTestInteractiveFurniture(canvasX: number, canvasY: number, canvasWidth: number, canvasHeight: number, camera: Camera): { furniture: FurnitureInstance; room: Room | null } | null {
    return hitTestFurniture(canvasX, canvasY, this.furniture, this.rooms,
      canvasWidth, canvasHeight, this.totalCols, this.totalRows, camera.zoom, camera.panX, camera.panY)
  }

  private _emitParticlesAtAgent(agentId: string, type: "confetti" | "spark"): void {
    const ch = this.characters.get(agentId)
    if (!ch) return
    const gridCol = ch.x / TILE_SIZE
    const gridRow = ch.y / TILE_SIZE
    this._pendingParticles.push({ agentId, type, gridCol, gridRow })
  }

  resolvePendingParticles(canvasWidth: number, canvasHeight: number, zoom: number, panX: number, panY: number): void {
    if (this._pendingParticles.length === 0) return
    const { originX, originY } = getOrigin(canvasWidth, canvasHeight, this.totalCols, this.totalRows, zoom, panX, panY)
    for (const p of this._pendingParticles) {
      const screen = gridToScreen(p.gridCol, p.gridRow, originX, originY, zoom)
      const cx = screen.x + (TD_TILE * zoom) / 2
      const cy = screen.y
      this.particlePool.emit(p.type, cx, cy)
    }
    this._pendingParticles = []
  }

  private _findAdjacentWalkable(col: number, row: number): { col: number; row: number } | null {
    const offsets = [{ dc: 1, dr: 0 }, { dc: -1, dr: 0 }, { dc: 0, dr: 1 }, { dc: 0, dr: -1 }]
    for (const { dc, dr } of offsets) {
      const c = col + dc
      const r = row + dr
      const key = `${c},${r}`
      if (
        r >= 0 && r < this.tileMap.length &&
        c >= 0 && c < (this.tileMap[0]?.length || 0) &&
        this.tileMap[r]?.[c] === 1 &&
        !this.blockedTiles.has(key)
      ) return { col: c, row: r }
    }
    return null
  }

  triggerThinkForAgent(agentId: string): void {
    const ch = this.characters.get(agentId)
    if (ch) triggerThink(ch)
  }

  triggerErrorForAgent(agentId: string): void {
    const ch = this.characters.get(agentId)
    if (ch) {
      triggerError(ch)
      this.emotes.set(agentId, createEmote(agentId, "error"))
      this._emitParticlesAtAgent(agentId, "spark")
    }
  }
}
