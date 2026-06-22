/** Pixel art office engine types — adapted from pixel-agents (MIT) */

export const TILE_SIZE = 16

/** 2D array of hex color strings (or '' for transparent). [row][col] */
export type SpriteData = string[][]

export const TileType = { WALL: 0, FLOOR: 1, VOID: 2 } as const
export type TileType = (typeof TileType)[keyof typeof TileType]

export const CharacterState = {
  IDLE: "idle",
  WALK: "walk",
  TYPE: "type",
  MEET: "meet",
  ERROR: "error",
  CELEBRATE: "celebrate",
  THINK: "think",
} as const
export type CharacterState =
  (typeof CharacterState)[keyof typeof CharacterState]

export const Direction = { DOWN: 0, LEFT: 1, RIGHT: 2, UP: 3 } as const
export type Direction = (typeof Direction)[keyof typeof Direction]

export interface Character {
  id: string
  agentName: string
  departmentId: string | null
  state: CharacterState
  dir: Direction
  x: number
  y: number
  tileCol: number
  tileRow: number
  path: Array<{ col: number; row: number }>
  moveProgress: number
  palette: number
  hueShift: number
  frame: number
  frameTimer: number
  wanderTimer: number
  wanderCount: number
  wanderLimit: number
  isActive: boolean
  seatId: string | null
  status: "online" | "busy" | "offline"
  seatTimer: number
  meetTarget: string | null
  shakeOffset: number
  shakeTimer: number
  bounceOffset: number
  bounceTimer: number
  breakPhase: "walk_to_coffee" | "at_coffee" | "walk_back" | null
  breakTimer: number
  coffeeTarget: { col: number; row: number } | null
  intensityMultiplier: number
  tintColor: string | null
  tintAlpha: number
  breakIdleTimer: number
  isOrchestrator: boolean
}

export interface Room {
  departmentId: string | null
  departmentName: string
  col: number
  row: number
  width: number
  height: number
  floorColor: string
  agents: string[]
}

export interface Seat {
  uid: string
  seatCol: number
  seatRow: number
  facingDir: Direction
  assignedTo: string | null
}

/** Isometric furniture types for procedural drawing */
export type IsoFurnitureType =
  | "desk"
  | "chair"
  | "laptop"
  | "phone"
  | "plant"
  | "bookshelf"
  | "whiteboard"
  | "tv"
  | "coffee"
  | "printer"
  | "router"
  | "cooler"
  | "lamp"
  | "painting"
  | "toolbox"
  | "sidetable"
  | "emptydesk"

export interface FurnitureInstance {
  type: IsoFurnitureType
  gridCol: number
  gridRow: number
  variant: number
  zDepth: number
  departmentId?: string
}

export interface CharacterSprites {
  walk: Record<Direction, SpriteData[]>
  typing: Record<Direction, SpriteData[]>
  thinking: Record<Direction, SpriteData[]>
}
