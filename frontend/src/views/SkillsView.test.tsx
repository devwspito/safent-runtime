import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// No @testing-library in this project yet — render directly via react-dom
// (mirrors TailnetSection.test.tsx / InboundDelegationCard.test.tsx).

// One recorded owner decision yields one exact-action grant. Community never
// requests MFA; removing the factor must not remove the approval capability.

const {
  listSkills, searchSkillsHub, listHubSkills, installSkill, getHubOpStatus,
  uninstallHubSkill, promoteSkill, getSkillDetails, scanInstall, recordSecurityDecision,
  sileoSuccess, sileoError, sileoWarning,
} = vi.hoisted(() => ({
  listSkills: vi.fn(),
  searchSkillsHub: vi.fn(),
  listHubSkills: vi.fn(),
  installSkill: vi.fn(),
  getHubOpStatus: vi.fn(),
  uninstallHubSkill: vi.fn(),
  promoteSkill: vi.fn(),
  getSkillDetails: vi.fn(),
  scanInstall: vi.fn(),
  recordSecurityDecision: vi.fn(),
  sileoSuccess: vi.fn(),
  sileoError: vi.fn(),
  sileoWarning: vi.fn(),
}))

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    listSkills, searchSkillsHub, listHubSkills, installSkill, getHubOpStatus,
    uninstallHubSkill, promoteSkill, getSkillDetails, scanInstall, recordSecurityDecision,
  }
})
vi.mock('sileo', () => ({ sileo: { success: sileoSuccess, error: sileoError, warning: sileoWarning } }))
vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
  useOutletContext: () => ({ startNew: vi.fn(), sendMessage: vi.fn() }),
}))

import SkillsView from './SkillsView'
import type { HubSkillResult, InstallScanResponse } from '../api/types'

const RESULT: HubSkillResult = {
  identifier: 'official/research/gitnexus-explorer',
  name: 'gitnexus-explorer',
  source: 'clawhub',
}

const FAIL_SCAN: InstallScanResponse = {
  scan_id: 'scan-1',
  verdict: 'FAIL',
  score: 30,
  engine: 'heuristic',
  engine_label: 'heuristic',
  requires_owner_approval: true,
  risks: [{ category: 'network', severity: 'HIGH', message: 'contacta un host desconocido' }],
}

function clickButton(container: HTMLElement, matcher: (text: string) => boolean) {
  const button = Array.from(container.querySelectorAll('button')).find(
    b => matcher(b.textContent ?? ''),
  )
  if (!button) throw new Error('button not found')
  act(() => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
}

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

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('SkillsView — hub install force (owner confirmation)', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    for (const fn of [
      listSkills, searchSkillsHub, listHubSkills, installSkill, getHubOpStatus,
      uninstallHubSkill, promoteSkill, getSkillDetails, scanInstall, recordSecurityDecision,
      sileoSuccess, sileoError, sileoWarning,
    ]) fn.mockReset()

    listSkills.mockResolvedValue([])
    listHubSkills.mockResolvedValue([])
    searchSkillsHub.mockResolvedValue({ results: [RESULT] })
    scanInstall.mockResolvedValue(FAIL_SCAN)
    recordSecurityDecision.mockResolvedValue({ ok: true, approval_grant: 'grant-abc123' })
    installSkill.mockResolvedValue({ op_id: 'op-1', status: 'pending' })

    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
  })

  it('approving a FAIL scan installs via a single-use owner grant without MFA', async () => {
    act(() => { root.render(React.createElement(SkillsView)) })
    await flush()

    const searchInput = container.querySelector<HTMLInputElement>('#hub-search')
    if (!searchInput) throw new Error('search input not found')
    typeInto(searchInput, 'gitnexus')
    clickButton(container, t => t === 'Buscar')
    await flush()

    clickButton(container, t => t === 'Instalar')
    await flush()

    expect(scanInstall).toHaveBeenCalledWith('skill', 'official/research/gitnexus-explorer')
    // InstallScanModal renders via createPortal(document.body) —
    // outside `container` — so scope those lookups to document.body.
    clickButton(document.body, t => t === 'Aprobar e instalar')
    await flush()

    expect(document.body.querySelector('input[inputmode="numeric"]')).toBeNull()
    expect(recordSecurityDecision).toHaveBeenCalledTimes(1)
    expect(recordSecurityDecision).toHaveBeenCalledWith(
      expect.objectContaining({ identifier: 'official/research/gitnexus-explorer' }),
    )

    // The install retry carries force=true and the approval grant — it is the
    // ONLY installSkill call.
    expect(installSkill).toHaveBeenCalledTimes(1)
    expect(installSkill).toHaveBeenCalledWith(
      'official/research/gitnexus-explorer',
      true,
      'grant-abc123',
    )
  })
})
