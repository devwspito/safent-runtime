/** Room builder — converts departments + agents into a unified office building */

import type {
  FurnitureInstance,
  IsoFurnitureType,
  Room,
  Seat,
  TileType as TileTypeVal,
} from "./types"
import { Direction, TileType } from "./types"

import { ROOM_COLORS } from "./constants"
import { zDepth } from "./iso"

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

// ── Layout constants ──────────────────────────────────
const EMPTY_ZONE_W = 6 // min width for empty department
const EMPTY_ZONE_H = 8 // min height for empty department
const ZONE_PAD = 1 // internal padding (1 tile each side)
const DESK_W = 4 // width per desk column
const ROW_PAIR_H = 4 // height per row-pair (2 agents stacked)
const HEADER_H = 2 // rows for department name
const FOOTER_H = 2 // rows for bottom decorations
const MAX_DESK_COLS = 5 // max desk columns before wrapping to new row-pair
const ZONE_GAP = 1 // gap between cubicles
const BUILDING_PAD = 1 // outer building margin
const MAX_ZONES_PER_ROW = 4 // max departments per row

export interface LayoutFilter {
  departmentIds?: Set<string>
  searchQuery?: string
}

/** Helper to create a furniture instance */
function furn(
  type: IsoFurnitureType,
  gridCol: number,
  gridRow: number,
  variant: number = 0
): FurnitureInstance {
  return { type, gridCol, gridRow, variant, zDepth: zDepth(gridCol, gridRow) }
}

/** Calculate zone dimensions — always room for 1 extra slot (empty desk) */
function calcZoneSize(agentCount: number): {
  width: number
  height: number
  cols: number
  rowPairs: number
} {
  if (agentCount === 0) {
    return { width: EMPTY_ZONE_W, height: EMPTY_ZONE_H, cols: 0, rowPairs: 0 }
  }
  // +1 for the empty desk slot, min 3 for furniture spacing
  const effective = Math.max(agentCount + 1, 3)
  const cols = Math.min(Math.max(1, Math.ceil(effective / 2)), MAX_DESK_COLS)
  const rowPairs = Math.ceil(effective / (cols * 2))
  const width = ZONE_PAD * 2 + cols * DESK_W
  const height = HEADER_H + FOOTER_H + ZONE_PAD * 2 + rowPairs * ROW_PAIR_H
  return { width, height, cols, rowPairs }
}

export function buildOfficeLayout(
  departments: DepartmentInfo[],
  agents: AgentInfo[],
  filter?: LayoutFilter,
  viewportCols?: number,
  viewportRows?: number
): {
  tileMap: TileTypeVal[][]
  rooms: Room[]
  seats: Seat[]
  furniture: FurnitureInstance[]
  totalCols: number
  totalRows: number
} {
  // Apply search filter to agents
  let filteredAgents = agents
  if (filter?.searchQuery) {
    const q = filter.searchQuery.toLowerCase()
    filteredAgents = agents.filter((a) => a.name.toLowerCase().includes(q))
  }

  // Group agents by department
  const agentsByDept = new Map<string | null, AgentInfo[]>()
  for (const agent of filteredAgents) {
    const key = agent.department_id || null
    if (!agentsByDept.has(key)) agentsByDept.set(key, [])
    agentsByDept.get(key)!.push(agent)
  }

  // Apply department filter
  const hasDeptFilter = filter?.departmentIds && filter.departmentIds.size > 0
  const searchDepts = new Set<string>()
  if (filter?.searchQuery && hasDeptFilter) {
    for (const agent of filteredAgents) {
      if (agent.department_id) searchDepts.add(agent.department_id)
    }
  }

  // Sort departments: Director General first, then alphabetical
  const sortedDepartments = [...departments].sort((a, b) => {
    if (a.is_director_dept && !b.is_director_dept) return -1
    if (!a.is_director_dept && b.is_director_dept) return 1
    return (a.display_name || a.name).localeCompare(b.display_name || b.name)
  })

  // Build zone specs with tight-fit sizing
  const zoneSpecs: Array<{
    deptId: string | null
    deptName: string
    role: string | null
    agents: AgentInfo[]
    width: number
    height: number
    cols: number
    rowPairs: number
    isDirector: boolean
  }> = []

  for (const dept of sortedDepartments) {
    if (
      hasDeptFilter &&
      !filter!.departmentIds!.has(dept.id) &&
      !searchDepts.has(dept.id)
    ) {
      agentsByDept.delete(dept.id)
      continue
    }

    const deptAgents = agentsByDept.get(dept.id) || []
    agentsByDept.delete(dept.id)

    const { width, height, cols, rowPairs } = calcZoneSize(deptAgents.length)

    zoneSpecs.push({
      deptId: dept.id,
      deptName: dept.display_name || dept.name,
      role: dept.role,
      agents: deptAgents,
      width,
      height,
      cols,
      rowPairs,
      isDirector: !!dept.is_director_dept,
    })
  }

  // Unassigned agents
  const unassigned = agentsByDept.get(null) || []
  if (unassigned.length > 0) {
    const { width, height, cols, rowPairs } = calcZoneSize(unassigned.length)
    zoneSpecs.push({
      deptId: null,
      deptName: "Unassigned",
      role: null,
      agents: unassigned,
      width,
      height,
      cols,
      rowPairs,
      isDirector: false,
    })
  }

  if (zoneSpecs.length === 0) {
    return {
      tileMap: [[TileType.VOID]],
      rooms: [],
      seats: [],
      furniture: [],
      totalCols: 1,
      totalRows: 1,
    }
  }

  // ── Aspect-ratio-aware packing ──
  // Try different row counts, for each try all greedy strategies,
  // pick the layout closest to wide-screen aspect ratio (~2.2 accounts for sidebar)
  const sorted = [...zoneSpecs].sort(
    (a, b) => b.agents.length - a.agents.length
  )
  const TARGET_ASPECT = 2.2

  type ZoneSpec = (typeof zoneSpecs)[number]
  interface RowLayout {
    zones: ZoneSpec[]
    width: number
    height: number
  }

  function scoreLayout(rows: RowLayout[]): number {
    const totalW = Math.max(...rows.map((r) => r.width))
    const totalH =
      rows.reduce((sum, r) => sum + r.height, 0) + (rows.length - 1) * ZONE_GAP
    if (totalW === 0 || totalH === 0) return Infinity
    return Math.abs(Math.log(totalW / totalH / TARGET_ASPECT))
  }

  function addToRow(row: RowLayout, zone: ZoneSpec) {
    row.zones.push(zone)
    row.width += zone.width + (row.zones.length > 1 ? ZONE_GAP : 0)
    row.height = Math.max(row.height, zone.height)
  }

  let bestRows: RowLayout[] | null = null
  let bestScore = Infinity

  const maxRows = Math.min(sorted.length, MAX_ZONES_PER_ROW)
  for (let numRows = 1; numRows <= maxRows; numRows++) {
    // Strategy 1: add to row with smallest width (balance widths)
    const rows1: RowLayout[] = Array.from({ length: numRows }, () => ({
      zones: [],
      width: 0,
      height: 0,
    }))
    for (const zone of sorted) {
      let target = rows1[0]!
      for (const r of rows1) {
        if (r.width < target.width) target = r
      }
      addToRow(target, zone)
    }
    if (!rows1.some((r) => r.zones.length > MAX_ZONES_PER_ROW)) {
      const s = scoreLayout(rows1)
      if (s < bestScore) {
        bestScore = s
        bestRows = rows1
      }
    }

    // Strategy 2: add to row with largest width (pack big with big)
    const rows2: RowLayout[] = Array.from({ length: numRows }, () => ({
      zones: [],
      width: 0,
      height: 0,
    }))
    for (const zone of sorted) {
      // First fill empty rows, then add to the widest
      const emptyRow = rows2.find((r) => r.zones.length === 0)
      if (emptyRow) {
        addToRow(emptyRow, zone)
      } else {
        let target = rows2[0]!
        for (const r of rows2) {
          if (r.width > target.width) target = r
        }
        addToRow(target, zone)
      }
    }
    if (!rows2.some((r) => r.zones.length > MAX_ZONES_PER_ROW)) {
      const s = scoreLayout(rows2)
      if (s < bestScore) {
        bestScore = s
        bestRows = rows2
      }
    }

    // Strategy 3: round-robin
    const rows3: RowLayout[] = Array.from({ length: numRows }, () => ({
      zones: [],
      width: 0,
      height: 0,
    }))
    for (let i = 0; i < sorted.length; i++) {
      addToRow(rows3[i % numRows]!, sorted[i]!)
    }
    if (!rows3.some((r) => r.zones.length > MAX_ZONES_PER_ROW)) {
      const s = scoreLayout(rows3)
      if (s < bestScore) {
        bestScore = s
        bestRows = rows3
      }
    }
  }

  // Place zones from the best layout
  const startOffset = BUILDING_PAD + 1
  const placedZones: Array<{
    spec: ZoneSpec
    col: number
    row: number
  }> = []

  let curRow = startOffset
  for (const row of bestRows!) {
    let curCol = startOffset
    for (const spec of row.zones) {
      placedZones.push({ spec, col: curCol, row: curRow })
      curCol += spec.width + ZONE_GAP
    }
    curRow += row.height + ZONE_GAP
  }

  // Calculate content dimensions (tight fit)
  const contentMaxCol = Math.max(
    ...placedZones.map((z) => z.col + z.spec.width)
  )
  const contentMaxRow = Math.max(
    ...placedZones.map((z) => z.row + z.spec.height)
  )
  let totalCols = contentMaxCol + BUILDING_PAD + 1
  let totalRows = contentMaxRow + BUILDING_PAD + 1

  // Expand to fill viewport if provided
  if (viewportCols && viewportCols > totalCols) {
    // Distribute extra horizontal space: widen zones proportionally
    const extraCols = viewportCols - totalCols
    const layoutRows = [...new Set(placedZones.map((z) => z.row))]
    for (const layoutRow of layoutRows) {
      const rowZones = placedZones
        .filter((z) => z.row === layoutRow)
        .sort((a, b) => a.col - b.col)
      const zonesInRow = rowZones.length
      if (zonesInRow === 0) continue
      const extraPerZone = Math.floor(extraCols / zonesInRow)
      const extraGap = Math.floor(
        (extraCols - extraPerZone * zonesInRow) / Math.max(1, zonesInRow - 1)
      )
      let colShift = 0
      for (let i = 0; i < rowZones.length; i++) {
        const pz = rowZones[i]!
        pz.col += colShift
        pz.spec = { ...pz.spec, width: pz.spec.width + extraPerZone }
        colShift += extraPerZone + (i < rowZones.length - 1 ? extraGap : 0)
      }
    }
    totalCols = viewportCols
  }

  if (viewportRows && viewportRows > totalRows) {
    // Distribute extra vertical space: increase zone heights
    const extraRows = viewportRows - totalRows
    const layoutRows = [...new Set(placedZones.map((z) => z.row))].sort(
      (a, b) => a - b
    )
    const numLayoutRows = layoutRows.length
    if (numLayoutRows > 0) {
      const extraPerRow = Math.floor(extraRows / numLayoutRows)
      let rowShift = 0
      for (const layoutRow of layoutRows) {
        const rowZones = placedZones.filter((z) => z.row === layoutRow)
        for (const pz of rowZones) {
          pz.row += rowShift
          pz.spec = { ...pz.spec, height: pz.spec.height + extraPerRow }
        }
        rowShift += extraPerRow
      }
    }
    totalRows = viewportRows
  }

  // Create tile map
  const tileMap: TileTypeVal[][] = []
  for (let r = 0; r < totalRows; r++) {
    tileMap.push(new Array(totalCols).fill(TileType.VOID))
  }

  // Fill building interior with FLOOR
  for (let r = 1; r < totalRows - 1; r++) {
    for (let c = 1; c < totalCols - 1; c++) {
      tileMap[r]![c] = TileType.FLOOR
    }
  }

  // Outer building walls
  for (let c = 0; c < totalCols; c++) {
    tileMap[0]![c] = TileType.WALL
    tileMap[totalRows - 1]![c] = TileType.WALL
  }
  for (let r = 0; r < totalRows; r++) {
    tileMap[r]![0] = TileType.WALL
    tileMap[r]![totalCols - 1] = TileType.WALL
  }

  // Internal walls between adjacent zones (grouped by row, sorted by column)
  const zonesByRow = new Map<number, typeof placedZones>()
  for (const pz of placedZones) {
    if (!zonesByRow.has(pz.row)) zonesByRow.set(pz.row, [])
    zonesByRow.get(pz.row)!.push(pz)
  }

  for (const [, rowZones] of zonesByRow) {
    rowZones.sort((a, b) => a.col - b.col)
    for (let i = 0; i < rowZones.length - 1; i++) {
      const curr = rowZones[i]!
      const next = rowZones[i + 1]!
      const wallCol = curr.col + curr.spec.width
      if (wallCol < totalCols - 1) {
        const wallTop = curr.row
        const wallBot = Math.max(
          curr.row + curr.spec.height,
          next.row + next.spec.height
        )
        const doorStart = wallTop + Math.floor((wallBot - wallTop) / 2) - 1
        for (let r = wallTop; r < wallBot; r++) {
          if (r >= doorStart && r < doorStart + 3) continue
          if (r >= 1 && r < totalRows - 1) {
            tileMap[r]![wallCol] = TileType.WALL
          }
        }
      }
    }
  }

  const rooms: Room[] = []
  const seats: Seat[] = []
  const furniture: FurnitureInstance[] = []

  for (const placed of placedZones) {
    const { spec, col: zCol, row: zRow } = placed
    const roleKey = spec.isDirector
      ? "executive"
      : spec.role?.toLowerCase() || "default"
    const floorColor = ROOM_COLORS[roleKey] || ROOM_COLORS["default"]

    // ── Individual workstations ──────────────────────
    const innerStartCol = zCol + ZONE_PAD
    let agentIdx = 0

    for (
      let pair = 0;
      pair < spec.rowPairs && agentIdx < spec.agents.length;
      pair++
    ) {
      const pairBaseY = zRow + HEADER_H + ZONE_PAD + pair * ROW_PAIR_H
      const topChairY = pairBaseY
      const topDeskY = topChairY + 1
      const botChairY = pairBaseY + 2
      const botDeskY = botChairY + 1

      for (
        let col = 0;
        col < spec.cols && agentIdx < spec.agents.length;
        col++
      ) {
        const dc = innerStartCol + col * DESK_W

        // Top row agent
        if (agentIdx < spec.agents.length) {
          furniture.push(furn("desk", dc, topDeskY, agentIdx % 3))
          furniture.push(furn("laptop", dc + 1, topDeskY, agentIdx % 3))
          furniture.push(furn("phone", dc, topDeskY))
          furniture.push(furn("chair", dc, topChairY, agentIdx % 4))
          seats.push({
            uid: `seat-${spec.deptId || "unassigned"}-${agentIdx}`,
            seatCol: dc,
            seatRow: topChairY,
            facingDir: Direction.DOWN,
            assignedTo: spec.agents[agentIdx]!.id,
          })
          agentIdx++
        }

        // Bottom row agent
        if (agentIdx < spec.agents.length) {
          furniture.push(furn("desk", dc, botDeskY, agentIdx % 3))
          furniture.push(furn("laptop", dc + 1, botDeskY, agentIdx % 3))
          furniture.push(furn("phone", dc, botDeskY))
          furniture.push(furn("chair", dc, botChairY, agentIdx % 4))
          seats.push({
            uid: `seat-${spec.deptId || "unassigned"}-${agentIdx}`,
            seatCol: dc,
            seatRow: botChairY,
            facingDir: Direction.DOWN,
            assignedTo: spec.agents[agentIdx]!.id,
          })
          agentIdx++
        }
      }
    }

    // ── Empty desk with "+" for creating new agent ──
    if (spec.deptId && agentIdx < spec.cols * spec.rowPairs * 2) {
      const pair = Math.floor(agentIdx / (spec.cols * 2))
      const posInPair = agentIdx % (spec.cols * 2)
      const isTop = posInPair % 2 === 0
      const col = Math.floor(posInPair / 2)
      const pairBaseY = zRow + HEADER_H + ZONE_PAD + pair * ROW_PAIR_H
      const deskY = isTop ? pairBaseY + 1 : pairBaseY + 3
      const chairY = isTop ? pairBaseY : pairBaseY + 2
      const dc = innerStartCol + col * DESK_W

      const emptyDesk = furn("emptydesk", dc, deskY, 0)
      emptyDesk.departmentId = spec.deptId
      furniture.push(emptyDesk)
      furniture.push(furn("chair", dc, chairY, 0))
    }

    // ── Decorations (adaptive to room size) ──────────
    const w = spec.width
    const h = spec.height

    // ── Distributed decorations & interactive furniture ──
    // Goal: spread items around all walls, not bunched together.
    // Items on desks: TV, printer, router. On wall: bookshelf, whiteboard. Floor: toolbox.
    const midRow = zRow + Math.floor(h / 2)

    // Corner plants (2-4 depending on room size)
    furniture.push(furn("plant", zCol + 1, zRow + 1, 0))
    if (w >= 4) furniture.push(furn("plant", zCol + w - 2, zRow + 1, 1))
    if (h >= 6) {
      furniture.push(furn("plant", zCol + 1, zRow + h - 2, 1))
      if (w >= 4) furniture.push(furn("plant", zCol + w - 2, zRow + h - 2, 0))
    }

    // ── TOP WALL (row+1) — bookshelf left, whiteboard right (avoid center = dept label) ──
    furniture.push(furn("bookshelf", zCol + 2, zRow + 1))
    if (w >= 8) {
      // Position whiteboard away from both the plant (w-2) and the center label
      furniture.push(furn("whiteboard", zCol + w - 4, zRow + 1))
    }

    // ── LEFT WALL — TV (workflows) on side table at midway height ──
    // ── RIGHT WALL — TV (workflows) on side table, midway+1 height ──
    if (h >= 8 && w >= 6) {
      furniture.push(furn("sidetable", zCol + w - 2, midRow + 1))
      furniture.push(furn("tv", zCol + w - 2, midRow))
    }

    // ── BOTTOM WALL — each item well spaced, pushed to last row ──
    if (h >= 6) {
      const tableRow = zRow + h - 1 // last row of room
      const itemRow = zRow + h - 2 // item sits 1 row above table
      // Printer (audit) on side table — left area
      if (w >= 6) {
        furniture.push(furn("sidetable", zCol + 2, tableRow))
        furniture.push(furn("printer", zCol + 2, itemRow))
      }
      // Toolbox (tools) on floor — center
      if (w >= 6) {
        furniture.push(furn("toolbox", zCol + Math.floor(w / 2), tableRow))
      }
      // Router (gateway) on side table — right of center
      if (w >= 8) {
        furniture.push(
          furn("sidetable", zCol + Math.floor(w / 2) + 2, tableRow)
        )
        furniture.push(furn("router", zCol + Math.floor(w / 2) + 2, itemRow))
      }
      // Refreshments — far right corner
      if (w >= 10) {
        furniture.push(furn("cooler", zCol + w - 3, tableRow))
        furniture.push(furn("coffee", zCol + w - 4, tableRow))
      }
    }

    // Floor lamp — right wall upper area
    if (w >= 4 && h >= 6) {
      furniture.push(furn("lamp", zCol + w - 2, zRow + 2, 0))
    }

    rooms.push({
      departmentId: spec.deptId,
      departmentName: spec.deptName,
      col: zCol,
      row: zRow,
      width: spec.width,
      height: spec.height,
      floorColor: floorColor || "#7A6A50",
      agents: spec.agents.map((a) => a.id),
    })
  }

  return { tileMap, rooms, seats, furniture, totalCols, totalRows }
}
