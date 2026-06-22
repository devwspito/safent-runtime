/** Character FSM — adapted from pixel-agents (MIT) */

import type { Character, Seat, TileType as TileTypeVal } from "./types"
import { CharacterState, Direction, TILE_SIZE } from "./types"

import {
  BREAK_COFFEE_MAX_SEC,
  BREAK_COFFEE_MIN_SEC,
  BREAK_IDLE_THRESHOLD_SEC,
  CELEBRATE_DURATION_SEC,
  ERROR_SHAKE_DURATION_SEC,
  SEAT_REST_MAX_SEC,
  SEAT_REST_MIN_SEC,
  THINK_FRAME_DURATION_SEC,
  TYPE_FRAME_DURATION_SEC,
  WALK_FRAME_DURATION_SEC,
  WALK_SPEED_PX_PER_SEC,
  WANDER_MOVES_BEFORE_REST_MAX,
  WANDER_MOVES_BEFORE_REST_MIN,
  WANDER_PAUSE_MAX_SEC,
  WANDER_PAUSE_MIN_SEC,
} from "./constants"
import { findPath } from "./pathfinder"

function tileCenter(col: number, row: number): { x: number; y: number } {
  return {
    x: col * TILE_SIZE + TILE_SIZE / 2,
    y: row * TILE_SIZE + TILE_SIZE / 2,
  }
}

function directionBetween(
  fromCol: number,
  fromRow: number,
  toCol: number,
  toRow: number
): Direction {
  const dc = toCol - fromCol
  const dr = toRow - fromRow
  if (dc > 0) return Direction.RIGHT
  if (dc < 0) return Direction.LEFT
  if (dr > 0) return Direction.DOWN
  return Direction.UP
}

function randomRange(min: number, max: number): number {
  return min + Math.random() * (max - min)
}

function randomInt(min: number, max: number): number {
  return min + Math.floor(Math.random() * (max - min + 1))
}

export function createCharacter(
  id: string,
  agentName: string,
  departmentId: string | null,
  palette: number,
  seat: Seat | null,
  status: "online" | "busy" | "offline",
  isOrchestrator = false
): Character {
  const col = seat ? seat.seatCol : 1
  const row = seat ? seat.seatRow : 1
  const center = tileCenter(col, row)
  const isActive = status === "online" || status === "busy"

  return {
    id,
    agentName,
    departmentId,
    state: CharacterState.IDLE,
    dir: seat ? seat.facingDir : Direction.DOWN,
    x: center.x,
    y: center.y,
    tileCol: col,
    tileRow: row,
    path: [],
    moveProgress: 0,
    palette,
    hueShift: palette >= 6 ? 45 + Math.random() * 270 : 0,
    frame: 0,
    frameTimer: 0,
    wanderTimer: randomRange(WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC),
    wanderCount: 0,
    wanderLimit: randomInt(
      WANDER_MOVES_BEFORE_REST_MIN,
      WANDER_MOVES_BEFORE_REST_MAX
    ),
    isActive,
    seatId: seat?.uid || null,
    status,
    seatTimer: 0,
    meetTarget: null,
    shakeOffset: 0,
    shakeTimer: 0,
    bounceOffset: 0,
    bounceTimer: 0,
    breakPhase: null,
    breakTimer: 0,
    coffeeTarget: null,
    intensityMultiplier: 1,
    tintColor: null,
    tintAlpha: 0,
    breakIdleTimer: 0,
    isOrchestrator,
  }
}

export function updateCharacter(
  ch: Character,
  dt: number,
  walkableTiles: Array<{ col: number; row: number }>,
  seats: Map<string, Seat>,
  tileMap: TileTypeVal[][],
  blockedTiles: Set<string>
): void {
  ch.frameTimer += dt

  switch (ch.state) {
    case CharacterState.ERROR: {
      ch.shakeTimer -= dt
      ch.shakeOffset = Math.sin(ch.shakeTimer * 30) * 2
      ch.tintColor = "#FF0000"
      ch.tintAlpha = 0.3 * Math.max(0, ch.shakeTimer / ERROR_SHAKE_DURATION_SEC)
      if (ch.shakeTimer <= 0) {
        ch.shakeOffset = 0
        ch.shakeTimer = 0
        ch.tintColor = null
        ch.tintAlpha = 0
        ch.state = ch.isActive ? CharacterState.IDLE : CharacterState.IDLE
        ch.frame = 0
        ch.frameTimer = 0
      }
      break
    }

    case CharacterState.THINK: {
      if (ch.frameTimer >= THINK_FRAME_DURATION_SEC) {
        ch.frameTimer -= THINK_FRAME_DURATION_SEC
        ch.frame = (ch.frame + 1) % 2
      }
      // Transition out is handled externally by handleActivityChange
      break
    }

    case CharacterState.CELEBRATE: {
      ch.bounceTimer -= dt
      ch.bounceOffset =
        Math.sin(ch.bounceTimer * 8) * Math.max(0, ch.bounceTimer) * -4
      if (ch.bounceTimer <= 0) {
        ch.bounceOffset = 0
        ch.bounceTimer = 0
        ch.state = ch.isActive ? CharacterState.TYPE : CharacterState.IDLE
        ch.frame = 0
        ch.frameTimer = 0
      }
      break
    }

    case CharacterState.MEET: {
      // Same animation as TYPE but stays until A2A ends (handled externally)
      if (ch.frameTimer >= TYPE_FRAME_DURATION_SEC) {
        ch.frameTimer -= TYPE_FRAME_DURATION_SEC
        ch.frame = (ch.frame + 1) % 2
      }
      // Exit MEET when meetTarget is cleared externally
      if (!ch.meetTarget) {
        ch.state = ch.isActive ? CharacterState.IDLE : CharacterState.IDLE
        ch.frame = 0
        ch.frameTimer = 0
      }
      break
    }

    case CharacterState.TYPE: {
      const typeDuration =
        TYPE_FRAME_DURATION_SEC / (ch.intensityMultiplier || 1)
      if (ch.frameTimer >= typeDuration) {
        ch.frameTimer -= typeDuration
        ch.frame = (ch.frame + 1) % 2
      }
      if (!ch.isActive) {
        if (ch.seatTimer > 0) {
          ch.seatTimer -= dt
          break
        }
        ch.seatTimer = 0
        ch.state = CharacterState.IDLE
        ch.frame = 0
        ch.frameTimer = 0
        ch.wanderTimer = randomRange(WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC)
        ch.wanderCount = 0
        ch.wanderLimit = randomInt(
          WANDER_MOVES_BEFORE_REST_MIN,
          WANDER_MOVES_BEFORE_REST_MAX
        )
      }
      break
    }

    case CharacterState.IDLE: {
      ch.frame = 0

      // Break: at coffee machine, wait then walk back
      if (ch.breakPhase === "at_coffee") {
        ch.breakTimer -= dt
        if (ch.breakTimer <= 0) {
          ch.breakPhase = "walk_back"
          ch.breakTimer = 0
          if (ch.seatId) {
            const seat = seats.get(ch.seatId)
            if (seat) {
              const path = findPath(
                ch.tileCol,
                ch.tileRow,
                seat.seatCol,
                seat.seatRow,
                tileMap,
                blockedTiles
              )
              if (path.length > 0) {
                ch.path = path
                ch.moveProgress = 0
                ch.state = CharacterState.WALK
                ch.frame = 0
                ch.frameTimer = 0
                break
              }
            }
          }
          ch.breakPhase = null
          ch.coffeeTarget = null
        }
        break
      }

      if (ch.isActive) {
        // Active agents stay IDLE at their seat until an activity event
        // triggers TYPE/THINK via handleActivityChange. Walk to seat if not there.
        if (ch.seatId) {
          const seat = seats.get(ch.seatId)
          if (
            seat &&
            (ch.tileCol !== seat.seatCol || ch.tileRow !== seat.seatRow)
          ) {
            const path = findPath(
              ch.tileCol,
              ch.tileRow,
              seat.seatCol,
              seat.seatRow,
              tileMap,
              blockedTiles
            )
            if (path.length > 0) {
              ch.path = path
              ch.moveProgress = 0
              ch.state = CharacterState.WALK
              ch.frame = 0
              ch.frameTimer = 0
            }
          } else if (seat) {
            ch.dir = seat.facingDir
          }
        }
        break
      }
      // Break idle timer: offline agents go get coffee after threshold
      if (!ch.isActive && !ch.breakPhase) {
        ch.breakIdleTimer += dt
        if (ch.breakIdleTimer >= BREAK_IDLE_THRESHOLD_SEC && ch.coffeeTarget) {
          ch.breakIdleTimer = 0
          ch.breakPhase = "walk_to_coffee"
          const path = findPath(
            ch.tileCol,
            ch.tileRow,
            ch.coffeeTarget.col,
            ch.coffeeTarget.row,
            tileMap,
            blockedTiles
          )
          if (path.length > 0) {
            ch.path = path
            ch.moveProgress = 0
            ch.state = CharacterState.WALK
            ch.frame = 0
            ch.frameTimer = 0
            break
          } else {
            ch.breakPhase = null
          }
        }
      }

      ch.wanderTimer -= dt
      if (ch.wanderTimer <= 0) {
        if (ch.wanderCount >= ch.wanderLimit && ch.seatId) {
          const seat = seats.get(ch.seatId)
          if (seat) {
            const path = findPath(
              ch.tileCol,
              ch.tileRow,
              seat.seatCol,
              seat.seatRow,
              tileMap,
              blockedTiles
            )
            if (path.length > 0) {
              ch.path = path
              ch.moveProgress = 0
              ch.state = CharacterState.WALK
              ch.frame = 0
              ch.frameTimer = 0
              break
            }
          }
        }
        if (walkableTiles.length > 0) {
          const target =
            walkableTiles[Math.floor(Math.random() * walkableTiles.length)]!
          const path = findPath(
            ch.tileCol,
            ch.tileRow,
            target.col,
            target.row,
            tileMap,
            blockedTiles
          )
          if (path.length > 0) {
            ch.path = path
            ch.moveProgress = 0
            ch.state = CharacterState.WALK
            ch.frame = 0
            ch.frameTimer = 0
            ch.wanderCount++
          }
        }
        ch.wanderTimer = randomRange(WANDER_PAUSE_MIN_SEC, WANDER_PAUSE_MAX_SEC)
      }
      break
    }

    case CharacterState.WALK: {
      if (ch.frameTimer >= WALK_FRAME_DURATION_SEC) {
        ch.frameTimer -= WALK_FRAME_DURATION_SEC
        ch.frame = (ch.frame + 1) % 4
      }

      if (ch.path.length === 0) {
        const center = tileCenter(ch.tileCol, ch.tileRow)
        ch.x = center.x
        ch.y = center.y

        // Break: arrived at coffee machine
        if (ch.breakPhase === "walk_to_coffee") {
          ch.breakPhase = "at_coffee"
          ch.breakTimer = randomRange(
            BREAK_COFFEE_MIN_SEC,
            BREAK_COFFEE_MAX_SEC
          )
          ch.state = CharacterState.IDLE
          ch.frame = 0
          ch.frameTimer = 0
          break
        }

        // Break: arrived back at seat
        if (ch.breakPhase === "walk_back") {
          ch.breakPhase = null
          ch.coffeeTarget = null
          ch.breakIdleTimer = 0
        }

        // If walking to a meeting point, transition to MEET
        if (ch.meetTarget) {
          ch.state = CharacterState.MEET
          ch.frame = 0
          ch.frameTimer = 0
          break
        }

        if (ch.isActive) {
          if (!ch.seatId) {
            ch.state = CharacterState.TYPE
          } else {
            const seat = seats.get(ch.seatId)
            if (
              seat &&
              ch.tileCol === seat.seatCol &&
              ch.tileRow === seat.seatRow
            ) {
              ch.state = CharacterState.TYPE
              ch.dir = seat.facingDir
            } else {
              ch.state = CharacterState.IDLE
            }
          }
        } else {
          if (ch.seatId) {
            const seat = seats.get(ch.seatId)
            if (
              seat &&
              ch.tileCol === seat.seatCol &&
              ch.tileRow === seat.seatRow
            ) {
              ch.state = CharacterState.TYPE
              ch.dir = seat.facingDir
              ch.seatTimer = randomRange(SEAT_REST_MIN_SEC, SEAT_REST_MAX_SEC)
              ch.wanderCount = 0
              ch.wanderLimit = randomInt(
                WANDER_MOVES_BEFORE_REST_MIN,
                WANDER_MOVES_BEFORE_REST_MAX
              )
              ch.frame = 0
              ch.frameTimer = 0
              break
            }
          }
          ch.state = CharacterState.IDLE
          ch.wanderTimer = randomRange(
            WANDER_PAUSE_MIN_SEC,
            WANDER_PAUSE_MAX_SEC
          )
        }
        ch.frame = 0
        ch.frameTimer = 0
        break
      }

      const nextTile = ch.path[0]!
      ch.dir = directionBetween(
        ch.tileCol,
        ch.tileRow,
        nextTile.col,
        nextTile.row
      )

      ch.moveProgress += (WALK_SPEED_PX_PER_SEC / TILE_SIZE) * dt

      const fromCenter = tileCenter(ch.tileCol, ch.tileRow)
      const toCenter = tileCenter(nextTile.col, nextTile.row)
      const t = Math.min(ch.moveProgress, 1)
      ch.x = fromCenter.x + (toCenter.x - fromCenter.x) * t
      ch.y = fromCenter.y + (toCenter.y - fromCenter.y) * t

      if (ch.moveProgress >= 1) {
        ch.tileCol = nextTile.col
        ch.tileRow = nextTile.row
        ch.x = toCenter.x
        ch.y = toCenter.y
        ch.path.shift()
        ch.moveProgress = 0
      }

      if (ch.isActive && ch.seatId) {
        const seat = seats.get(ch.seatId)
        if (seat) {
          const lastStep = ch.path[ch.path.length - 1]
          if (
            !lastStep ||
            lastStep.col !== seat.seatCol ||
            lastStep.row !== seat.seatRow
          ) {
            const newPath = findPath(
              ch.tileCol,
              ch.tileRow,
              seat.seatCol,
              seat.seatRow,
              tileMap,
              blockedTiles
            )
            if (newPath.length > 0) {
              ch.path = newPath
              ch.moveProgress = 0
            }
          }
        }
      }
      break
    }
  }
}

// ── Trigger helpers (called externally by office-state) ──────

export function triggerError(ch: Character): void {
  ch.state = CharacterState.ERROR
  ch.shakeTimer = ERROR_SHAKE_DURATION_SEC
  ch.shakeOffset = 0
  ch.tintColor = "#FF0000"
  ch.tintAlpha = 0.3
  ch.frame = 0
  ch.frameTimer = 0
  ch.intensityMultiplier = 1
}

export function triggerCelebrate(ch: Character): void {
  ch.state = CharacterState.CELEBRATE
  ch.bounceTimer = CELEBRATE_DURATION_SEC
  ch.bounceOffset = 0
  ch.frame = 0
  ch.frameTimer = 0
  ch.intensityMultiplier = 1
}

export function triggerThink(ch: Character): void {
  ch.state = CharacterState.THINK
  ch.frame = 0
  ch.frameTimer = 0
  ch.intensityMultiplier = 1
}

export { directionBetween }
