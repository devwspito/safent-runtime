import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// No @testing-library in this project yet — render directly via react-dom
// (mirrors TailnetSection.test.tsx).

const { getSshHosts, revokeSshHost, sileoSuccess, sileoError } = vi.hoisted(() => ({
  getSshHosts: vi.fn(),
  revokeSshHost: vi.fn(),
  sileoSuccess: vi.fn(),
  sileoError: vi.fn(),
}))

vi.mock('../api/client', () => ({ getSshHosts, revokeSshHost }))
vi.mock('sileo', () => ({ sileo: { success: sileoSuccess, error: sileoError } }))

// OwnerConfirmation opens a portal; the section under test only
// needs to know it was asked to confirm or cancel — stub it down to two buttons
// so this file stays focused on SshHostsSection's own logic.
vi.mock('../components/OwnerConfirmation', () => ({
  default: ({ title, onConfirm, onCancel }: {
    title: string
    onConfirm: () => void
    onCancel: () => void
  }) =>
    React.createElement(
      'div',
      { 'data-testid': 'owner-confirmation' },
      React.createElement('span', null, title),
      React.createElement(
        'button',
        { type: 'button', onClick: () => onConfirm() },
        'Confirmar',
      ),
      React.createElement('button', { type: 'button', onClick: onCancel }, 'Cancelar modal'),
    ),
}))

import { SshHostsSection } from './SeguridadView'
import type { SshHostEntry } from '../api/types'

const HOSTS: SshHostEntry[] = [
  { host: 'db1.tailxxxx.ts.net', approved_at: '2026-09-01T10:00:00+00:00' },
  { host: 'build-box.tailxxxx.ts.net', approved_at: null },
]

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

describe('SshHostsSection', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    getSshHosts.mockReset()
    revokeSshHost.mockReset()
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

  it('shows the empty state when no hosts are approved', async () => {
    getSshHosts.mockResolvedValue({ hosts: [] })

    act(() => { root.render(React.createElement(SshHostsSection)) })
    await flush()

    expect(container.textContent).toContain('Ningún equipo tiene SSH aprobado todavía')
  })

  it('lists approved hosts with a human-readable approved_at', async () => {
    getSshHosts.mockResolvedValue({ hosts: HOSTS })

    act(() => { root.render(React.createElement(SshHostsSection)) })
    await flush()

    expect(container.textContent).toContain('db1.tailxxxx.ts.net')
    expect(container.textContent).toContain('build-box.tailxxxx.ts.net')
    expect(container.textContent).toContain('Aprobado el')
    // approved_at: null falls back to an honest "unknown" label, never a blank/garbage date
    expect(container.textContent).toContain('Fecha de aprobación desconocida')
  })

  it('shows an error state when the fetch fails', async () => {
    getSshHosts.mockRejectedValue(new Error('network down'))

    act(() => { root.render(React.createElement(SshHostsSection)) })
    await flush()

    expect(container.textContent).toContain('No se pudo cargar la lista de equipos')
  })

  it('opens owner confirmation when Revocar is clicked, and cancel closes it without calling the API', async () => {
    getSshHosts.mockResolvedValue({ hosts: HOSTS })

    act(() => { root.render(React.createElement(SshHostsSection)) })
    await flush()

    clickButton(container, 'Revocar')
    expect(container.querySelector('[data-testid="owner-confirmation"]')).not.toBeNull()

    clickButton(container, 'Cancelar modal')
    expect(container.querySelector('[data-testid="owner-confirmation"]')).toBeNull()
    expect(revokeSshHost).not.toHaveBeenCalled()
  })

  it('confirming calls revokeSshHost with the exact host, then refreshes the list', async () => {
    getSshHosts.mockResolvedValue({ hosts: HOSTS })
    revokeSshHost.mockResolvedValue({
      hosts: [{ host: 'build-box.tailxxxx.ts.net', approved_at: null }],
    })

    act(() => { root.render(React.createElement(SshHostsSection)) })
    await flush()

    clickButton(container, 'Revocar')
    await flush()
    clickButton(container, 'Confirmar')
    await flush()

    expect(revokeSshHost).toHaveBeenCalledWith('db1.tailxxxx.ts.net')
    expect(sileoSuccess).toHaveBeenCalledTimes(1)
    expect(container.textContent).not.toContain('db1.tailxxxx.ts.net')
    expect(container.textContent).toContain('build-box.tailxxxx.ts.net')
  })

  it('shows an error toast when revoke fails and keeps the host in the list', async () => {
    getSshHosts.mockResolvedValue({ hosts: HOSTS })
    revokeSshHost.mockRejectedValue(new Error('código incorrecto'))

    act(() => { root.render(React.createElement(SshHostsSection)) })
    await flush()

    clickButton(container, 'Revocar')
    await flush()
    clickButton(container, 'Confirmar')
    await flush()

    expect(sileoError).toHaveBeenCalledTimes(1)
    expect(container.textContent).toContain('db1.tailxxxx.ts.net')
  })
})
