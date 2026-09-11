import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// No @testing-library in this project yet — render directly via react-dom
// (mirrors KillSwitchSection.test.tsx / TailnetSection.test.tsx).

// Policy changes require explicit owner confirmation, independent of removed MFA.

const { getPolicies, setPolicyPreset, setPolicyTools, setApprovalOnDangers, sileoSuccess, sileoError } =
  vi.hoisted(() => ({
    getPolicies: vi.fn(),
    setPolicyPreset: vi.fn(),
    setPolicyTools: vi.fn(),
    setApprovalOnDangers: vi.fn(),
    sileoSuccess: vi.fn(),
    sileoError: vi.fn(),
  }))

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, getPolicies, setPolicyPreset, setPolicyTools, setApprovalOnDangers }
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

function policiesWith(approvalOnDangers: boolean) {
  return { preset: 'equilibrado', tools: {}, overridden: [], approval_on_dangers: approvalOnDangers, catalog: [] }
}

function confirmChange() {
  expect(document.body.querySelector('input[inputmode="numeric"]')).toBeNull()
  clickButton(document.body, t => t.includes('Confirmar'))
}

describe('GovernanceSection — the approval_on_dangers toggle is sovereign', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    getPolicies.mockReset()
    setPolicyPreset.mockReset()
    setPolicyTools.mockReset()
    setApprovalOnDangers.mockReset()
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

  it('MFA enrolled + approval_on_dangers ON: turning it OFF requires only explicit confirmation', async () => {
    getPolicies.mockResolvedValue(policiesWith(true))
    setApprovalOnDangers.mockResolvedValue({ ok: true, approval_on_dangers: false })

    act(() => { root.render(React.createElement(GovernanceSection)) })
    await flush()

    const toggle = container.querySelector<HTMLButtonElement>('#toggle-mfa-dangers')!
    act(() => { toggle.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    await flush()

    // Modal must be up — no direct call yet.
    expect(setApprovalOnDangers).not.toHaveBeenCalled()
    confirmChange()
    await flush()

    expect(setApprovalOnDangers).toHaveBeenCalledWith(false)
  })

  it('MFA enrolled + approval_on_dangers OFF: turning it back ON STILL requires only explicit confirmation (the SEG-15 dead-end)', async () => {
    getPolicies.mockResolvedValue(policiesWith(false))
    setApprovalOnDangers.mockResolvedValue({ ok: true, approval_on_dangers: true })

    act(() => { root.render(React.createElement(GovernanceSection)) })
    await flush()

    const toggle = container.querySelector<HTMLButtonElement>('#toggle-mfa-dangers')!
    act(() => { toggle.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    await flush()

    // Before the fix this branched on mfaDisabled (true here) and called
    // setApprovalOnDangers(true) directly — no modal, backend 401, dead end.
    expect(setApprovalOnDangers).not.toHaveBeenCalled()
    expect(document.body.querySelector('.mfa-modal')).not.toBeNull()

    confirmChange()
    await flush()

    expect(setApprovalOnDangers).toHaveBeenCalledWith(true)
    expect(sileoSuccess).toHaveBeenCalledTimes(1)
  })

  it('MFA never enrolled: toggling approval_on_dangers requires confirmation but no enrollment', async () => {
    getPolicies.mockResolvedValue(policiesWith(false))
    setApprovalOnDangers.mockResolvedValue({ ok: true, approval_on_dangers: true })

    act(() => { root.render(React.createElement(GovernanceSection)) })
    await flush()

    const toggle = container.querySelector<HTMLButtonElement>('#toggle-mfa-dangers')!
    act(() => { toggle.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    await flush()

    expect(setApprovalOnDangers).not.toHaveBeenCalled()
    confirmChange()
    await flush()
    expect(setApprovalOnDangers).toHaveBeenCalledWith(true)
  })
})
