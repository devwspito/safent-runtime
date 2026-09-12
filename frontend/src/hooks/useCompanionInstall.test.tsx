import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Same minimal-deps style as the rest of this project — no @testing-library.

const { postInstallRequest, getInstallRequests } = vi.hoisted(() => ({
  postInstallRequest: vi.fn(),
  getInstallRequests: vi.fn(),
}))

vi.mock('../api/client', () => ({ postInstallRequest, getInstallRequests }))

import { useCompanionInstall, deriveCompanionInstallPhase } from './useCompanionInstall'
import type { AdsAvailability } from './useAdsAvailability'
import type { InstallRequestStatus } from '../api/types'

function availability(
  status: AdsAvailability['status'],
  reason: AdsAvailability['reason'] = null,
  refresh: () => void = () => undefined,
): AdsAvailability {
  return { status, reason, refresh }
}

describe('deriveCompanionInstallPhase (pure)', () => {
  it('managed never offers repair/install for an old local install failure', () => {
    expect(deriveCompanionInstallPhase(availability('managed'), { verb: 'install_companion', state: 'failed', expires_at: 't' })).toEqual({ kind: 'hidden' })
  })
  it.each([
    ['ready', null] as const,
    ['loading', null] as const,
  ])('is hidden while availability is %s', (status, reason) => {
    expect(deriveCompanionInstallPhase(availability(status, reason), null)).toEqual({ kind: 'hidden' })
  })

  it('offers install when not_installed and no live request', () => {
    expect(deriveCompanionInstallPhase(availability('unavailable', 'not_installed'), null))
      .toEqual({ kind: 'install' })
  })

  it('offers repair when unreachable (present but down) and no live request', () => {
    expect(deriveCompanionInstallPhase(availability('unavailable', 'unreachable'), null))
      .toEqual({ kind: 'repair' })
  })

  it.each(['unauthorized', 'no_accounts'] as const)(
    'is hidden for %s — onboarding/credentials, not an install concern',
    (reason) => {
      expect(deriveCompanionInstallPhase(availability('unavailable', reason), null))
        .toEqual({ kind: 'hidden' })
    },
  )

  it('is installing while pending, regardless of availability', () => {
    const status: InstallRequestStatus = {
      verb: 'install_companion', state: 'pending', expires_at: 't',
      stage: 'pull_companion', progress: { done: 1, total: 2, unit: 'layers' },
    }
    expect(deriveCompanionInstallPhase(availability('unavailable', 'not_installed'), status))
      .toEqual({ kind: 'installing', stage: 'pull_companion', progress: { done: 1, total: 2, unit: 'layers' } })
  })

  it('applied but the bridge has not confirmed yet stays "installing" — never a fake ready', () => {
    const status: InstallRequestStatus = { verb: 'install_companion', state: 'applied', expires_at: 't' }
    expect(deriveCompanionInstallPhase(availability('unavailable', 'unreachable'), status).kind)
      .toBe('installing')
  })

  it('applied AND the bridge confirms ready — hides the action (AdsNavItem/AdsView own the ready state)', () => {
    const status: InstallRequestStatus = { verb: 'install_companion', state: 'applied', expires_at: 't' }
    expect(deriveCompanionInstallPhase(availability('ready'), status)).toEqual({ kind: 'hidden' })
  })

  it('expired ⇒ expired, failed ⇒ failed with the backend label', () => {
    const expired: InstallRequestStatus = { verb: 'install_companion', state: 'expired', expires_at: 't' }
    expect(deriveCompanionInstallPhase(availability('unavailable', 'not_installed'), expired))
      .toEqual({ kind: 'expired' })

    const failed: InstallRequestStatus = {
      verb: 'repair_companion', state: 'failed', expires_at: 't',
      last_failure: { code: 'companion_network_conflict', label: 'La red ya está en uso', retryable: true },
    }
    expect(deriveCompanionInstallPhase(availability('unavailable', 'unreachable'), failed))
      .toEqual({ kind: 'failed', label: 'La red ya está en uso', retryable: true })
  })
})

interface HarnessProps {
  status: AdsAvailability['status']
  reason?: AdsAvailability['reason']
  refresh?: () => void
}

function Harness({ status, reason = null, refresh = () => undefined }: HarnessProps) {
  const av = useCompanionInstall(availability(status, reason, refresh))
  return React.createElement(
    'div',
    { 'data-phase': av.phase.kind },
    React.createElement('button', { 'data-testid': 'install', onClick: av.install }, 'install'),
    React.createElement('button', { 'data-testid': 'repair', onClick: av.repair }, 'repair'),
    React.createElement('button', { 'data-testid': 'retry', onClick: av.retry }, 'retry'),
  )
}

describe('useCompanionInstall (hook)', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    postInstallRequest.mockReset()
    getInstallRequests.mockReset().mockResolvedValue({ requests: [] })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
  })

  async function flush() {
    await act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve() })
  }

  it('adopts an install request already in flight from the OTHER surface on mount', async () => {
    getInstallRequests.mockResolvedValue({
      requests: [{ verb: 'install_companion', state: 'claimed', expires_at: 't', stage: 'pull_companion' }],
    })

    act(() => { root.render(React.createElement(Harness, { status: 'unavailable', reason: 'not_installed' })) })
    await flush()

    expect(container.querySelector('div')?.getAttribute('data-phase')).toBe('installing')
    expect(postInstallRequest).not.toHaveBeenCalled()
  })

  it('install() posts install_companion exactly once, even on a fast double click', async () => {
    postInstallRequest.mockResolvedValue({
      accepted: true,
      request: { verb: 'install_companion', state: 'pending', expires_at: 't' },
    })

    act(() => { root.render(React.createElement(Harness, { status: 'unavailable', reason: 'not_installed' })) })
    await flush()

    const button = container.querySelector('[data-testid="install"]')!
    await act(async () => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    await flush()

    expect(postInstallRequest).toHaveBeenCalledTimes(1)
    expect(postInstallRequest).toHaveBeenCalledWith('install_companion', { slug: 'safent-ads' })
    expect(container.querySelector('div')?.getAttribute('data-phase')).toBe('installing')
  })

  it('retry() re-fires the SAME verb the failed request used', async () => {
    getInstallRequests.mockResolvedValue({
      requests: [{
        verb: 'repair_companion', state: 'failed', expires_at: 't',
        last_failure: { code: 'companion_migration_failed', label: 'La migración falló', retryable: true },
      }],
    })
    postInstallRequest.mockResolvedValue({
      accepted: true,
      request: { verb: 'repair_companion', state: 'pending', expires_at: 't' },
    })

    act(() => { root.render(React.createElement(Harness, { status: 'unavailable', reason: 'unreachable' })) })
    await flush()
    expect(container.querySelector('div')?.getAttribute('data-phase')).toBe('failed')

    const button = container.querySelector('[data-testid="retry"]')!
    await act(async () => {
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(postInstallRequest).toHaveBeenCalledWith('repair_companion', { slug: 'safent-ads' })
  })

  it('calls availability.refresh() exactly once on the pending→applied edge, not on every render', async () => {
    getInstallRequests.mockResolvedValueOnce({
      requests: [{ verb: 'install_companion', state: 'applied', expires_at: 't' }],
    })
    const refresh = vi.fn()

    act(() => {
      root.render(React.createElement(Harness, { status: 'unavailable', reason: 'unreachable', refresh }))
    })
    await flush()

    expect(refresh).toHaveBeenCalledTimes(1)
  })
})
