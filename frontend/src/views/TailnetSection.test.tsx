import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// No @testing-library in this project yet — render directly via react-dom
// (mirrors InboundDelegationCard.test.tsx).

const { getTailnetStatus, connectTailnet, disconnectTailnet, sileoSuccess, sileoError } =
  vi.hoisted(() => ({
    getTailnetStatus: vi.fn(),
    connectTailnet: vi.fn(),
    disconnectTailnet: vi.fn(),
    sileoSuccess: vi.fn(),
    sileoError: vi.fn(),
  }))

vi.mock('../api/client', () => ({ getTailnetStatus, connectTailnet, disconnectTailnet }))
vi.mock('sileo', () => ({ sileo: { success: sileoSuccess, error: sileoError } }))

import { TailnetSection } from './SeguridadView'
import type { TailnetStatus } from '../api/types'

const UNCONFIGURED: TailnetStatus = {
  configured: false,
  online: false,
  node_name: null,
  magicdns_suffix: null,
  tailnet: null,
  peers: [],
  last_attempt: null,
}

// 025 hallazgo D: `configured` now means LOGGED IN (== online) — a node
// that's mid-reconnect (a PAST connect succeeded, tailscaled is catching up)
// is `configured: false` too; last_attempt.ok !== false is what still says
// "connecting", not "failed" or "never tried".
const CONNECTING: TailnetStatus = {
  configured: false,
  online: false,
  node_name: 'safent-agent',
  magicdns_suffix: 'tail1234.ts.net',
  tailnet: 'acme.ts.net',
  peers: [],
  last_attempt: { at: '2026-09-10T14:03:00+00:00', ok: true, error_kind: null },
}

const CONNECTED: TailnetStatus = {
  configured: true,
  online: true,
  node_name: 'safent-agent',
  magicdns_suffix: 'tail1234.ts.net',
  tailnet: 'acme.ts.net',
  peers: [
    { name: 'laptop', online: true },
    { name: 'server', online: false },
  ],
  last_attempt: { at: '2026-09-10T14:03:00+00:00', ok: true, error_kind: null },
}

// Regression fixture (matriz 10-sep, hallazgo D): `tailscale up` failed 5/5
// on a rejected key, yet status.json still exists (the watcher writes it as
// soon as tailscaled STARTS, independent of login outcome).
const REJECTED: TailnetStatus = {
  configured: false,
  online: false,
  node_name: '2846102aaf5a',
  magicdns_suffix: '',
  tailnet: '',
  peers: [],
  last_attempt: { at: '2026-09-10T14:03:00+00:00', ok: false, error_kind: 'tailscale_up_failed' },
}

function findInput(container: HTMLElement, testLabel: string): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>(`[aria-label="${testLabel}"]`)
  if (!input) throw new Error(`input aria-label="${testLabel}" not found`)
  return input
}

// React tracks the DOM input's value via a wrapped native setter so it can tell a
// "real" change from a value it just wrote itself; assigning `.value` directly and
// dispatching a plain Event leaves that tracker out of sync and the onChange never
// fires. Going through the native setter first is the standard React-testing
// workaround (no @testing-library/react in this project — see file header).
function typeInto(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    'value',
  )!.set!
  act(() => {
    nativeSetter.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

function clickButton(container: HTMLElement, label: string) {
  const button = Array.from(container.querySelectorAll('button')).find(
    b => b.textContent === label,
  )
  if (!button) throw new Error(`button "${label}" not found`)
  act(() => {
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('TailnetSection', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    getTailnetStatus.mockReset()
    connectTailnet.mockReset()
    disconnectTailnet.mockReset()
    sileoSuccess.mockReset()
    sileoError.mockReset()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
  })

  it('shows the paste-auth-key form when not configured (never prefilled)', async () => {
    getTailnetStatus.mockResolvedValue(UNCONFIGURED)

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    const input = findInput(container, 'Clave de autenticación de la tailnet')
    expect(input.type).toBe('password')
    expect(input.value).toBe('')
    expect(container.textContent).toContain('Conectar')
    expect(container.textContent).not.toContain('Sufijo MagicDNS')
  })

  it('shows "Conectando…" and the MagicDNS suffix sentence while configured but offline', async () => {
    getTailnetStatus.mockResolvedValue(CONNECTING)

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    expect(container.textContent).toContain('Conectando')
    expect(container.textContent).toContain('tail1234.ts.net')
    expect(container.textContent).toContain(
      'Los hosts de la tailnet se conceden como cualquier dominio en Egress.',
    )
  })

  it('shows "Conectado como <node_name> en <tailnet>" and the peers list when online', async () => {
    getTailnetStatus.mockResolvedValue(CONNECTED)

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    expect(container.textContent).toContain('Conectado como safent-agent en acme.ts.net')
    expect(container.textContent).toContain('laptop')
    expect(container.textContent).toContain('server')
    expect(container.textContent).toContain('En línea')
    expect(container.textContent).toContain('Sin conexión')
  })

  it('submits the typed auth key via connectTailnet and reloads status', async () => {
    getTailnetStatus.mockResolvedValueOnce(UNCONFIGURED).mockResolvedValueOnce(CONNECTING)
    connectTailnet.mockResolvedValue({ staged: true })

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    const input = findInput(container, 'Clave de autenticación de la tailnet')
    typeInto(input, 'tskey-auth-kABC123-xyz')
    clickButton(container, 'Conectar')
    await flush()

    expect(connectTailnet).toHaveBeenCalledWith('tskey-auth-kABC123-xyz')
    expect(sileoSuccess).toHaveBeenCalledTimes(1)
    expect(getTailnetStatus).toHaveBeenCalledTimes(2)
  })

  it('clears the auth-key input after a successful connect', async () => {
    getTailnetStatus.mockResolvedValue(UNCONFIGURED)
    connectTailnet.mockResolvedValue({ staged: true })

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    const input = findInput(container, 'Clave de autenticación de la tailnet')
    typeInto(input, 'tskey-auth-kABC123-xyz')
    clickButton(container, 'Conectar')
    await flush()

    expect(input.value).toBe('')
  })

  it('shows an error toast and keeps the form when connect fails', async () => {
    getTailnetStatus.mockResolvedValue(UNCONFIGURED)
    connectTailnet.mockRejectedValue(new Error('clave inválida'))

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    const input = findInput(container, 'Clave de autenticación de la tailnet')
    typeInto(input, 'tskey-auth-bad')
    clickButton(container, 'Conectar')
    await flush()

    expect(sileoError).toHaveBeenCalledTimes(1)
    expect(sileoSuccess).not.toHaveBeenCalled()
  })

  it('submits the device password via disconnectTailnet when connected', async () => {
    getTailnetStatus.mockResolvedValueOnce(CONNECTED).mockResolvedValueOnce(UNCONFIGURED)
    disconnectTailnet.mockResolvedValue({ staged: true })

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    const input = findInput(container, 'Contraseña del dispositivo para desconectar la tailnet')
    expect(input.type).toBe('password')
    typeInto(input, 'mi-contraseña-del-dispositivo')
    clickButton(container, 'Desconectar')
    await flush()

    expect(disconnectTailnet).toHaveBeenCalledWith('mi-contraseña-del-dispositivo')
    expect(sileoSuccess).toHaveBeenCalledTimes(1)
  })

  it('shows "Clave rechazada" instead of success when the last connect attempt failed', async () => {
    getTailnetStatus.mockResolvedValue(REJECTED)

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    expect(container.textContent).toContain('Clave rechazada')
    expect(container.textContent).not.toContain('Conectado')
    expect(container.textContent).not.toContain('Conectando')
  })

  it('re-shows the paste-key form after a rejected key so the owner can retry', async () => {
    getTailnetStatus.mockResolvedValue(REJECTED)

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    const input = findInput(container, 'Clave de autenticación de la tailnet')
    expect(input.value).toBe('')
    expect(container.textContent).toContain('Conectar')
  })

  it('does not show a disconnect form or MagicDNS info after a rejected key', async () => {
    getTailnetStatus.mockResolvedValue(REJECTED)

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    expect(
      container.querySelector('[aria-label="Contraseña del dispositivo para desconectar la tailnet"]'),
    ).toBeNull()
    expect(container.textContent).not.toContain('Sufijo MagicDNS')
  })

  it('does not render the disconnect form when not configured', async () => {
    getTailnetStatus.mockResolvedValue(UNCONFIGURED)

    act(() => {
      root.render(React.createElement(TailnetSection))
    })
    await flush()

    expect(
      container.querySelector('[aria-label="Contraseña del dispositivo para desconectar la tailnet"]'),
    ).toBeNull()
  })
})
