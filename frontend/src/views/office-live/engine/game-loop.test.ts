import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { startGameLoop } from "./game-loop"

// A fake rAF scheduler we drive by hand — `tick()` runs the oldest pending
// frame, `cancelAnimationFrame` actually removes it (mirrors the browser),
// so we can assert precisely when the loop is/is not scheduling frames.
let scheduled: Map<number, FrameRequestCallback>
let nextId: number

function tick(time: number) {
  const [id] = scheduled.keys()
  if (id === undefined) return
  const cb = scheduled.get(id)!
  scheduled.delete(id)
  cb(time)
}

function fakeCanvas(): HTMLCanvasElement {
  const ctx = { imageSmoothingEnabled: true } as unknown as CanvasRenderingContext2D
  return { getContext: () => ctx } as unknown as HTMLCanvasElement
}

beforeEach(() => {
  scheduled = new Map()
  nextId = 0
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    const id = ++nextId
    scheduled.set(id, cb)
    return id
  })
  vi.stubGlobal("cancelAnimationFrame", (id: number) => {
    scheduled.delete(id)
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  Object.defineProperty(document, "hidden", { value: false, configurable: true })
})

describe("startGameLoop", () => {
  it("keeps invoking later frames after update() throws mid-run", () => {
    const canvas = fakeCanvas()
    let updateCalls = 0

    const stop = startGameLoop(canvas, {
      update: () => {
        updateCalls += 1
        if (updateCalls === 3) throw new Error("boom in update")
      },
      render: () => {},
    })

    for (let frame = 1; frame <= 5; frame += 1) tick(frame * 16)

    expect(updateCalls).toBe(5)
    stop()
  })

  it("keeps invoking later frames after render() throws mid-run", () => {
    const canvas = fakeCanvas()
    let renderCalls = 0

    const stop = startGameLoop(canvas, {
      update: () => {},
      render: () => {
        renderCalls += 1
        if (renderCalls === 3) throw new Error("boom in render")
      },
    })

    for (let frame = 1; frame <= 5; frame += 1) tick(frame * 16)

    expect(renderCalls).toBe(5)
    stop()
  })

  it("stops scheduling while the tab is hidden and resumes with dt=0 on visible", () => {
    const canvas = fakeCanvas()
    const deltas: number[] = []

    const stop = startGameLoop(canvas, {
      update: (dt) => deltas.push(dt),
      render: () => {},
    })

    tick(1000)
    tick(1016)
    expect(scheduled.size).toBe(1) // still scheduling while visible

    Object.defineProperty(document, "hidden", { value: true, configurable: true })
    document.dispatchEvent(new Event("visibilitychange"))
    expect(scheduled.size).toBe(0) // paused: no frame pending while hidden

    Object.defineProperty(document, "hidden", { value: false, configurable: true })
    document.dispatchEvent(new Event("visibilitychange"))
    expect(scheduled.size).toBe(1) // resumed

    tick(60_000) // a huge real-time jump while the tab was backgrounded
    expect(deltas[deltas.length - 1]).toBe(0) // lastTime was reset — no dt spike

    stop()
  })

  it("cancels the pending frame and removes the listener on stop", () => {
    const canvas = fakeCanvas()
    const stop = startGameLoop(canvas, { update: () => {}, render: () => {} })

    expect(scheduled.size).toBe(1)
    stop()
    expect(scheduled.size).toBe(0)

    Object.defineProperty(document, "hidden", { value: true, configurable: true })
    document.dispatchEvent(new Event("visibilitychange"))
    expect(scheduled.size).toBe(0) // stopped loop ignores further visibility events
  })
})
