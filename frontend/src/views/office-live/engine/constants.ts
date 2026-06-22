/** Engine constants — adapted from pixel-agents (MIT) */

export const MAX_DELTA_TIME_SEC = 0.1
export const WALK_SPEED_PX_PER_SEC = 48
export const WALK_FRAME_DURATION_SEC = 0.15
export const TYPE_FRAME_DURATION_SEC = 0.3
export const WANDER_PAUSE_MIN_SEC = 2
export const WANDER_PAUSE_MAX_SEC = 6
export const WANDER_MOVES_BEFORE_REST_MIN = 2
export const WANDER_MOVES_BEFORE_REST_MAX = 5
export const SEAT_REST_MIN_SEC = 3
export const SEAT_REST_MAX_SEC = 8
export const CHARACTER_SITTING_OFFSET_PX = 6
export const CHARACTER_Z_SORT_OFFSET = 4

export const ROOM_COLORS: Record<string, string> = {
  operations: "#A08040",
  engineering: "#4A7090",
  support: "#907050",
  sales: "#B09040",
  marketing: "#B06040",
  finance: "#408060",
  hr: "#906060",
  research: "#407868",
  design: "#905888",
  executive: "#D97706",
  default: "#887060",
  unassigned: "#706858",
}

export const LABEL_COLOR = "#FFFFFF"

// ── New animation constants ──
export const THINK_FRAME_DURATION_SEC = 0.5
export const ERROR_SHAKE_DURATION_SEC = 1.5
export const CELEBRATE_DURATION_SEC = 1.2
export const BREAK_IDLE_THRESHOLD_SEC = 10
export const BREAK_COFFEE_MIN_SEC = 3
export const BREAK_COFFEE_MAX_SEC = 5
export const EMOTE_LIFETIME_MS = 2500
export const EMOTE_FADE_MS = 400
export const EMOTE_POP_MS = 200
