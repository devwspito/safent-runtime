/** Game loop — adapted from pixel-agents (MIT) */

import { MAX_DELTA_TIME_SEC } from "./constants"

export interface GameLoopCallbacks {
  update: (dt: number) => void
  render: (ctx: CanvasRenderingContext2D) => void
}

export function startGameLoop(
  canvas: HTMLCanvasElement,
  callbacks: GameLoopCallbacks
): () => void {
  const ctx = canvas.getContext("2d")!
  ctx.imageSmoothingEnabled = false

  let lastTime = 0
  let rafId = 0
  let stopped = false
  // Tab hidden ⇒ we stop scheduling frames (nothing visible to render, and it
  // avoids a huge dt spike from an unthrottled background rAF on resume).
  let paused = typeof document !== "undefined" && document.hidden

  const frame = (time: number) => {
    if (stopped) return
    const dt =
      lastTime === 0
        ? 0
        : Math.min((time - lastTime) / 1000, MAX_DELTA_TIME_SEC)
    lastTime = time

    // Any single frame's update/render must never kill the loop: a bad
    // live-event shape (a newly-appeared agent, a null in a status field, a
    // missing sprite) is logged and skipped, not fatal. Without this guard an
    // uncaught throw here would leave the canvas frozen forever (the
    // recurring "pixel-art disconnects from events" bug).
    try {
      callbacks.update(dt)
    } catch (err) {
      console.debug("[game-loop] update() threw — skipping frame", err)
    }

    try {
      ctx.imageSmoothingEnabled = false
      callbacks.render(ctx)
    } catch (err) {
      console.debug("[game-loop] render() threw — skipping frame", err)
    }

    if (!stopped && !paused) {
      rafId = requestAnimationFrame(frame)
    }
  }

  const handleVisibilityChange = () => {
    if (stopped) return
    if (document.hidden) {
      paused = true
      cancelAnimationFrame(rafId)
    } else if (paused) {
      paused = false
      lastTime = 0 // discard the time spent hidden — avoid a huge-dt jump
      rafId = requestAnimationFrame(frame)
    }
  }

  document.addEventListener("visibilitychange", handleVisibilityChange)

  if (!paused) {
    rafId = requestAnimationFrame(frame)
  }

  return () => {
    stopped = true
    cancelAnimationFrame(rafId)
    document.removeEventListener("visibilitychange", handleVisibilityChange)
  }
}
