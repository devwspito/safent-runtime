import { act } from 'react-dom/test-utils'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

// No @testing-library in this project yet — render directly via react-dom
// (mirrors GovernanceSection.test.tsx / KillSwitchSection.test.tsx).

// Regression (specs/025-safent-repaso PROV-02): the "Add/Connect" flow sent
// configureNativeProvider({provider_id, api_key}) with NO `model` at all —
// config.yaml ended up with model.provider set and no model.default, and the
// first chat crashed with HermesModelNotConfiguredError. Pins the fix: the
// model field is pre-filled from the catalogue's default_model, is required
// before submitting, and is sent to configureNativeProvider.

const { configureNativeProvider, testProvider, setActiveProvider } = vi.hoisted(() => ({
  configureNativeProvider: vi.fn(),
  testProvider: vi.fn(),
  setActiveProvider: vi.fn(),
}))

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, configureNativeProvider, testProvider, setActiveProvider }
})
vi.mock('sileo', () => ({ sileo: { success: vi.fn(), error: vi.fn(), warn: vi.fn() } }))

import { ProviderRow } from './ProvidersView'
import type { Provider } from '../api/types'

function clickButton(root: ParentNode, matcher: (text: string) => boolean) {
  const button = Array.from(root.querySelectorAll('button')).find(b => matcher(b.textContent ?? ''))
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

const NATIVE_ANTHROPIC: Provider = {
  provider_id: 'anthropic',
  alias: 'Anthropic',
  kind: 'anthropic',
  auth_type: 'api_key',
  default_model: 'claude-sonnet-4-6',
}

function renderRow(provider: Provider, onToast = vi.fn()) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  act(() => {
    root.render(
      React.createElement(ProviderRow, {
        provider,
        isConfigured: false,
        onRefresh: vi.fn(),
        onToast,
        onConfirm: vi.fn().mockResolvedValue(true),
      }),
    )
  })
  return { container, root, onToast }
}

describe('ProviderRow — Add/Connect always sends a model', () => {
  let containers: HTMLDivElement[]
  let roots: Root[]

  beforeEach(() => {
    configureNativeProvider.mockReset()
    testProvider.mockReset()
    setActiveProvider.mockReset()
    containers = []
    roots = []
  })

  afterEach(() => {
    roots.forEach(r => act(() => { r.unmount() }))
    containers.forEach(c => c.remove())
  })

  function track(built: { container: HTMLDivElement; root: Root; onToast: ReturnType<typeof vi.fn> }) {
    containers.push(built.container)
    roots.push(built.root)
    return built
  }

  it('pre-fills the model field from the catalogue default_model', async () => {
    const { container } = track(renderRow(NATIVE_ANTHROPIC))
    clickButton(container, t => t === 'Añadir')
    await flush()

    const modelInput = container.querySelector<HTMLInputElement>('#pv-model-anthropic')
    expect(modelInput).not.toBeNull()
    expect(modelInput!.value).toBe('claude-sonnet-4-6')
  })

  it('refuses to submit with an empty model, even with a valid key', async () => {
    const { container, onToast } = track(renderRow(NATIVE_ANTHROPIC))
    clickButton(container, t => t === 'Añadir')
    await flush()

    typeInto(container.querySelector<HTMLInputElement>('#pv-key-anthropic')!, 'sk-ant-real')
    typeInto(container.querySelector<HTMLInputElement>('#pv-model-anthropic')!, '')
    clickButton(container, t => t === 'Guardar')
    await flush()

    expect(configureNativeProvider).not.toHaveBeenCalled()
    expect(onToast).toHaveBeenCalledWith(expect.any(String), 'warn')
  })

  it('sends the (editable) model to configureNativeProvider on submit', async () => {
    configureNativeProvider.mockResolvedValue({ provider_id: 'anthropic' })
    testProvider.mockResolvedValue({ ok: true })
    setActiveProvider.mockResolvedValue({ ok: true })
    const { container } = track(renderRow(NATIVE_ANTHROPIC))
    clickButton(container, t => t === 'Añadir')
    await flush()

    typeInto(container.querySelector<HTMLInputElement>('#pv-key-anthropic')!, 'sk-ant-real')
    // Owner overrides the pre-filled suggestion — this must be respected.
    typeInto(container.querySelector<HTMLInputElement>('#pv-model-anthropic')!, 'claude-opus-4-7')
    clickButton(container, t => t === 'Guardar')
    await flush()

    expect(configureNativeProvider).toHaveBeenCalledWith({
      provider_id: 'anthropic',
      api_key: 'sk-ant-real',
      model: 'claude-opus-4-7',
      set_active: false,
    })
  })
  it('never activates a provider whose connection test failed',async()=>{
    configureNativeProvider.mockResolvedValue({provider_id:'anthropic'});testProvider.mockResolvedValue({ok:false})
    const {container}=track(renderRow(NATIVE_ANTHROPIC));clickButton(container,t=>t==='Añadir');await flush()
    typeInto(container.querySelector<HTMLInputElement>('#pv-key-anthropic')!,'fictitious-key')
    clickButton(container,t=>t==='Guardar');await flush();expect(setActiveProvider).not.toHaveBeenCalled()
  })
  it('does not activate after leaving while the connection test is pending',async()=>{
    let resolve!:(v:{ok:boolean})=>void
    configureNativeProvider.mockResolvedValue({provider_id:'anthropic'});testProvider.mockReturnValue(new Promise(r=>{resolve=r}))
    const {container,root}=track(renderRow(NATIVE_ANTHROPIC));clickButton(container,t=>t==='Añadir');await flush()
    typeInto(container.querySelector<HTMLInputElement>('#pv-key-anthropic')!,'fictitious-key')
    clickButton(container,t=>t==='Guardar');await flush();act(()=>root.render(null))
    await act(async()=>resolve({ok:true}));expect(setActiveProvider).not.toHaveBeenCalled()
  })
})
