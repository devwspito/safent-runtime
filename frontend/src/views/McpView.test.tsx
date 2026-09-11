import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// Same minimal-deps style as the rest of this project — no @testing-library.
// Scoped to the Ads card (029 SC-001): by default there is NO connection
// field on screen, only "Instalar"; the legacy self-host URL field exists
// only behind the "Avanzado" disclosure, closed by default.

const {
  listMcpServers, listManagedRemoteEndpoints, postInstallRequest, getInstallRequests,
  scanInstall, addMcpServer,
} = vi.hoisted(() => ({
  listMcpServers: vi.fn(),
  listManagedRemoteEndpoints: vi.fn(),
  postInstallRequest: vi.fn(),
  getInstallRequests: vi.fn(),
  scanInstall: vi.fn(), addMcpServer:vi.fn(),
}))

const { useAdsAvailability } = vi.hoisted(() => ({ useAdsAvailability: vi.fn() }))

vi.mock('../hooks/useAdsAvailability', () => ({ useAdsAvailability }))

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    listMcpServers,
    listManagedRemoteEndpoints,
    postInstallRequest,
    getInstallRequests,
    scanInstall, addMcpServer,
  }
})

import McpView from './McpView'

describe('McpView — Ads card (029 SC-001)', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    listMcpServers.mockReset().mockResolvedValue([])
    scanInstall.mockReset();addMcpServer.mockReset()
    listManagedRemoteEndpoints.mockReset().mockResolvedValue({ endpoints: {} })
    postInstallRequest.mockReset()
    getInstallRequests.mockReset().mockResolvedValue({ requests: [] })
    useAdsAvailability.mockReset().mockReturnValue({
      status: 'unavailable', reason: 'not_installed', refresh: vi.fn(),
    })
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => { root.unmount() })
    container.remove()
  })

  async function render() {
    await act(async () => {
      root.render(React.createElement(McpView))
      for (let i = 0; i < 5; i++) await Promise.resolve()
    })
  }
  it.each(['offline','unknown','empty-pass'])('does not request MCP install on unverifiable scan: %s',async mode=>{
    if(mode==='offline') scanInstall.mockRejectedValue(new Error('offline'))
    else if(mode==='empty-pass') scanInstall.mockResolvedValue({verdict:'PASS',scan_id:'',requires_owner_approval:false})
    else scanInstall.mockResolvedValue({verdict:'UNKNOWN'})
    await render()
    const card=Array.from(container.querySelectorAll('div')).find(el=>el.className.includes('catalogCard') && el.textContent?.includes('Context7') && el.querySelector('button'))
    const install=Array.from(card?.querySelectorAll('button')??[]).find(el=>el.textContent==='Añadir')
    expect(install).toBeTruthy()
    await act(async()=>{install!.click();for(let i=0;i<6;i++)await Promise.resolve()})
    expect(scanInstall).toHaveBeenCalled();expect(addMcpServer).not.toHaveBeenCalled()
  })
  it('does not add MCP when a scan arrives after leaving',async()=>{
    let resolve!:(value:unknown)=>void;scanInstall.mockReturnValue(new Promise(r=>{resolve=r}))
    await render()
    const card=Array.from(container.querySelectorAll('div')).find(el=>el.className.includes('catalogCard') && el.textContent?.includes('Context7') && el.querySelector('button'))!
    const button=Array.from(card.querySelectorAll('button')).find(el=>el.textContent==='Añadir')!
    await act(async()=>button.click());act(()=>root.render(null))
    await act(async()=>resolve({verdict:'PASS',requires_owner_approval:false}))
    expect(addMcpServer).not.toHaveBeenCalled()
  })

  it('shows ONLY "Instalar" by default — zero connection/URL fields on screen', async () => {
    await render()

    expect(container.querySelector('input#mcp-managed-ads-url')).toBeNull()
    const installBtn = Array.from(container.querySelectorAll('button'))
      .find(b => b.textContent === 'Instalar')
    expect(installBtn).not.toBeUndefined()
  })

  it('the self-host URL field appears ONLY after opening "Avanzado", and stays out of the default path', async () => {
    await render()

    const toggle = Array.from(container.querySelectorAll('button'))
      .find(b => b.textContent?.includes('Avanzado'))!
    expect(toggle.getAttribute('aria-expanded')).toBe('false')

    await act(async () => {
      toggle.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const urlField = container.querySelector('input#mcp-managed-ads-url')
    expect(urlField).not.toBeNull()
    // Its own validation/connect affordance survives intact behind the disclosure.
    expect(Array.from(container.querySelectorAll('button')).some(b => b.textContent === 'Conectar')).toBe(true)
  })

  it('clicking "Instalar" drives the SAME install-request flow as the sidebar (029 FR-001)', async () => {
    postInstallRequest.mockResolvedValue({
      accepted: true,
      request: { verb: 'install_companion', state: 'pending', expires_at: 't' },
    })
    await render()

    const installBtn = Array.from(container.querySelectorAll('button'))
      .find(b => b.textContent === 'Instalar')!
    await act(async () => {
      installBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })

    expect(postInstallRequest).toHaveBeenCalledWith('install_companion', { slug: 'safent-ads' })
  })
})
