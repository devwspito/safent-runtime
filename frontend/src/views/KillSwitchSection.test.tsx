import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// No @testing-library in this project yet — render directly via react-dom
// (mirrors TailnetSection.test.tsx).

// Regression (matriz 10-sep, hallazgo C): the emergency-brake release dialog
// always asked for a TOTP, even when MFA was never enrolled — a dead end
// (403 mfa_not_enrolled, no path forward). Pins the fixed UI: the release
// dialog shows WHICH proof it's asking for, based on GET /mfa/status.

const { getKillSwitch, engageKillSwitch, releaseKillSwitch, mfaStatus, sileoSuccess, sileoError } =
  vi.hoisted(() => ({
    getKillSwitch: vi.fn(),
    engageKillSwitch: vi.fn(),
    releaseKillSwitch: vi.fn(),
    mfaStatus: vi.fn(),
    sileoSuccess: vi.fn(),
    sileoError: vi.fn(),
  }))

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, getKillSwitch, engageKillSwitch, releaseKillSwitch, mfaStatus }
})
vi.mock('sileo', () => ({ sileo: { success: sileoSuccess, error: sileoError } }))

import { KillSwitchSection } from './SeguridadView'
import type { KillSwitchStatus } from '../api/types'

const ENGAGED: KillSwitchStatus = {
  engaged: true, reason: 'freno de prueba', changed_by: 'owner', changed_at: '2026-09-10T10:00:00Z',
}

function clickButton(container: ParentNode, matcher: (text: string) => boolean) {
  const button = Array.from(container.querySelectorAll('button')).find(
    b => matcher(b.textContent ?? ''),
  )
  if (!button) throw new Error('button not found')
  act(() => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('KillSwitchSection — release dialog shows which proof is asked', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    getKillSwitch.mockReset()
    engageKillSwitch.mockReset()
    releaseKillSwitch.mockReset()
    mfaStatus.mockReset()
    sileoSuccess.mockReset()
    sileoError.mockReset()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
    document.body.querySelectorAll('.mfa-modal-backdrop').forEach(el => el.remove())
  })

  it.each([false, true])('releases only after owner confirmation, irrespective of legacy enrollment %s', async enrolled => {
    getKillSwitch.mockResolvedValue(ENGAGED)
    mfaStatus.mockResolvedValue({ enrolled })
    releaseKillSwitch.mockResolvedValue({ ok: true })
    act(() => { root.render(React.createElement(KillSwitchSection)) })
    await flush()
    expect(container.textContent).not.toMatch(/TOTP|contraseña/)
    clickButton(container, t => t.includes('Liberar'))
    await flush()
    expect(releaseKillSwitch).not.toHaveBeenCalled()
    expect(document.body.querySelector('.mfa-modal input')).toBeNull()
    clickButton(document.body, t => t === 'Confirmar')
    await flush()
    expect(releaseKillSwitch).toHaveBeenCalledExactlyOnceWith()
    expect(mfaStatus).not.toHaveBeenCalled()
    expect(sileoSuccess).toHaveBeenCalledTimes(1)
  })

  it('shows unknown state on error, permits stopping, and retries without offering release', async () => {
    getKillSwitch.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(ENGAGED)
    act(() => { root.render(React.createElement(KillSwitchSection)) })
    await flush()
    expect(container.textContent).toContain('Estado del freno desconocido')
    expect(container.textContent).not.toContain('Todo en marcha')
    expect(container.textContent).not.toContain('Liberar freno')
    expect(container.textContent).toContain('Activar freno')
    clickButton(container, text => text === 'Reintentar')
    await flush()
    expect(container.textContent).toContain('Liberar freno')
    expect(releaseKillSwitch).not.toHaveBeenCalled()
  })
})
