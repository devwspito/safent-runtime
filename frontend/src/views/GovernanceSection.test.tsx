import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// No @testing-library in this project yet — render directly via react-dom
// (mirrors KillSwitchSection.test.tsx / TailnetSection.test.tsx).

// Regression (specs/025-safent-repaso SEG-15): with mfa_on_dangers OFF, the UI
// used to send totp:'' for the toggle ITSELF too — the owner could never turn
// verification back ON from the UI (backend 401, no modal shown to fix it).
// Pins the sovereign rule: the mfa_on_dangers toggle always requires only explicit confirmation
// while MFA is enrolled — turning it ON or OFF, and REGARDLESS of its current
// value — and only skips the prompt when MFA was never enrolled at all.

const { mfaStatus, getPolicies, setPolicyPreset, setPolicyTools, setMfaOnDangers, sileoSuccess, sileoError } =
  vi.hoisted(() => ({
    mfaStatus: vi.fn(),
    getPolicies: vi.fn(),
    setPolicyPreset: vi.fn(),
    setPolicyTools: vi.fn(),
    setMfaOnDangers: vi.fn(),
    sileoSuccess: vi.fn(),
    sileoError: vi.fn(),
  }))

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, mfaStatus, getPolicies, setPolicyPreset, setPolicyTools, setMfaOnDangers }
})
vi.mock('sileo', () => ({ sileo: { success: sileoSuccess, error: sileoError } }))

import { GovernanceSection } from './SeguridadView'

function clickButton(root: ParentNode, matcher: (text: string) => boolean) {
  const button = Array.from(root.querySelectorAll('button')).find(b => matcher(b.textContent ?? ''))
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

function policiesWith(mfaOnDangers: boolean) {
  return { preset: 'equilibrado', tools: {}, overridden: [], mfa_on_dangers: mfaOnDangers, catalog: [] }
}

function confirmChange() {
  expect(document.body.querySelector('input[inputmode="numeric"]')).toBeNull()
  clickButton(document.body, t => t.includes('Confirmar'))
}

describe('GovernanceSection — the mfa_on_dangers toggle is sovereign', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    mfaStatus.mockReset()
    getPolicies.mockReset()
    setPolicyPreset.mockReset()
    setPolicyTools.mockReset()
    setMfaOnDangers.mockReset()
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

  it('MFA enrolled + mfa_on_dangers ON: turning it OFF requires only explicit confirmation', async () => {
    mfaStatus.mockResolvedValue({ enrolled: true })
    getPolicies.mockResolvedValue(policiesWith(true))
    setMfaOnDangers.mockResolvedValue({ ok: true, mfa_on_dangers: false })

    act(() => { root.render(React.createElement(GovernanceSection)) })
    await flush()

    const toggle = container.querySelector<HTMLButtonElement>('#toggle-mfa-dangers')!
    act(() => { toggle.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    await flush()

    // Modal must be up — no direct call yet.
    expect(setMfaOnDangers).not.toHaveBeenCalled()
    confirmChange()
    await flush()

    expect(setMfaOnDangers).toHaveBeenCalledWith(false)
  })

  it('MFA enrolled + mfa_on_dangers OFF: turning it back ON STILL requires only explicit confirmation (the SEG-15 dead-end)', async () => {
    mfaStatus.mockResolvedValue({ enrolled: true })
    getPolicies.mockResolvedValue(policiesWith(false))
    setMfaOnDangers.mockResolvedValue({ ok: true, mfa_on_dangers: true })

    act(() => { root.render(React.createElement(GovernanceSection)) })
    await flush()

    const toggle = container.querySelector<HTMLButtonElement>('#toggle-mfa-dangers')!
    act(() => { toggle.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    await flush()

    // Before the fix this branched on mfaDisabled (true here) and called
    // setMfaOnDangers(true) directly — no modal, backend 401, dead end.
    expect(setMfaOnDangers).not.toHaveBeenCalled()
    expect(document.body.querySelector('.mfa-modal')).not.toBeNull()

    confirmChange()
    await flush()

    expect(setMfaOnDangers).toHaveBeenCalledWith(true)
    expect(sileoSuccess).toHaveBeenCalledTimes(1)
  })

  it('MFA never enrolled: toggling mfa_on_dangers requires confirmation but no enrollment', async () => {
    mfaStatus.mockResolvedValue({ enrolled: false })
    getPolicies.mockResolvedValue(policiesWith(false))
    setMfaOnDangers.mockResolvedValue({ ok: true, mfa_on_dangers: true })

    act(() => { root.render(React.createElement(GovernanceSection)) })
    await flush()

    const toggle = container.querySelector<HTMLButtonElement>('#toggle-mfa-dangers')!
    act(() => { toggle.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    await flush()

    expect(setMfaOnDangers).not.toHaveBeenCalled()
    confirmChange()
    await flush()
    expect(setMfaOnDangers).toHaveBeenCalledWith(true)
  })
})
