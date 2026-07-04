import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { openRuntimeStream } from "./client"

// A controllable stand-in for the browser's EventSource. Real EventSource
// auto-reconnects on transient network blips (readyState → CONNECTING); it
// only reports CLOSED when it has permanently given up. Tests drive both
// paths explicitly instead of relying on real network/timers.
class FakeEventSource {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2

  static instances: FakeEventSource[] = []

  readyState = FakeEventSource.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null

  constructor(public url: string) {
    FakeEventSource.instances.push(this)
  }

  close() {
    this.readyState = FakeEventSource.CLOSED
  }

  open() {
    this.readyState = FakeEventSource.OPEN
    this.onopen?.()
  }

  failClosed() {
    this.readyState = FakeEventSource.CLOSED
    this.onerror?.()
  }

  failTransient() {
    this.readyState = FakeEventSource.CONNECTING
    this.onerror?.()
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeEventSource.instances = []
  vi.stubGlobal("EventSource", FakeEventSource)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("openRuntimeStream", () => {
  it("does not resubscribe on a transient error (native reconnect handles it)", () => {
    const dispose = openRuntimeStream(() => {})
    expect(FakeEventSource.instances).toHaveLength(1)

    FakeEventSource.instances[0].failTransient()
    vi.advanceTimersByTime(20_000)

    expect(FakeEventSource.instances).toHaveLength(1)
    dispose()
  })

  it("resubscribes with exponential backoff after the source is permanently CLOSED", () => {
    const dispose = openRuntimeStream(() => {})
    expect(FakeEventSource.instances).toHaveLength(1)

    FakeEventSource.instances[0].failClosed()
    vi.advanceTimersByTime(999)
    expect(FakeEventSource.instances).toHaveLength(1) // not yet — backoff is 1s

    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(2) // reconnected at 1s

    FakeEventSource.instances[1].failClosed()
    vi.advanceTimersByTime(1_999)
    expect(FakeEventSource.instances).toHaveLength(2) // backoff doubled to 2s
    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(3)

    dispose()
  })

  it("caps backoff at 15s", () => {
    const dispose = openRuntimeStream(() => {})

    // 1s, 2s, 4s, 8s, 15s(capped) — five consecutive permanent failures.
    const expectedDelaysMs = [1_000, 2_000, 4_000, 8_000, 15_000]
    for (const delay of expectedDelaysMs) {
      const current = FakeEventSource.instances[FakeEventSource.instances.length - 1]
      current.failClosed()
      vi.advanceTimersByTime(delay - 1)
      expect(FakeEventSource.instances).toHaveLength(expectedDelaysMs.indexOf(delay) + 1)
      vi.advanceTimersByTime(1)
    }

    expect(FakeEventSource.instances).toHaveLength(6)
    dispose()
  })

  it("resets backoff to 1s once a connection opens", () => {
    const dispose = openRuntimeStream(() => {})

    FakeEventSource.instances[0].failClosed()
    vi.advanceTimersByTime(1_000) // reconnect #2 at the 1s floor
    expect(FakeEventSource.instances).toHaveLength(2)

    FakeEventSource.instances[1].open() // recovers → backoff resets
    FakeEventSource.instances[1].failClosed()
    vi.advanceTimersByTime(999)
    expect(FakeEventSource.instances).toHaveLength(2) // still 1s floor, not 2s
    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(3)

    dispose()
  })

  it("delivers parsed snapshots via onmessage", () => {
    const onSnapshot = vi.fn()
    const dispose = openRuntimeStream(onSnapshot)

    const snapshot = { runtime: { state: "working", active_task_count: 1 }, stats: { available: true, agents: [] } }
    FakeEventSource.instances[0].onmessage?.({ data: JSON.stringify(snapshot) } as MessageEvent)

    expect(onSnapshot).toHaveBeenCalledWith(snapshot)
    dispose()
  })

  it("stops reconnecting once disposed", () => {
    const dispose = openRuntimeStream(() => {})
    FakeEventSource.instances[0].failClosed()
    dispose()

    vi.advanceTimersByTime(30_000)
    expect(FakeEventSource.instances).toHaveLength(1)
  })
})
