import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'

const api = vi.hoisted(() => ({
  listCrmConnections: vi.fn(),
  readCrm: vi.fn(),
}))
vi.mock('../api/crm', () => api)
import { EnterpriseCrm } from './EnterpriseCrm'

const context = 'a'.repeat(64)
const connection = {
  id: 'crm-a',
  name: 'CRM compartido',
  read_only: true,
  operations: [{ method: 'GET', path: '/contacts' }],
}
let container: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  api.listCrmConnections
    .mockReset()
    .mockResolvedValue({ context, connections: [connection], limit: 100 })
  api.readCrm
    .mockReset()
    .mockResolvedValue({
      data: { customer: 'fixture' },
      context,
      untrusted_external_data: true,
      operation: connection.operations[0],
    })
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})
async function render() {
  await act(async () => root.render(<EnterpriseCrm />))
}
async function choose() {
  for (const [index, value] of [
    [0, 'crm-a'],
    [1, '/contacts'],
  ] as const) {
    await act(async () => {
      const select = container.querySelectorAll('select')[index]
      select.value = value
      select.dispatchEvent(new Event('change', { bubbles: true }))
    })
  }
}
async function click(text: string) {
  const button = Array.from(container.querySelectorAll('button')).find(
    (item) =>
      item.textContent === text || item.getAttribute('aria-label') === text,
  )
  expect(button).toBeDefined()
  await act(async () => button!.click())
}

it('requires explicit connection and operation; displays untrusted data only as text', async () => {
  api.readCrm.mockResolvedValueOnce({
    data: '<script>window.fake=true</script>',
  })
  await render()
  expect(
    Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent === 'Consultar CRM',
    )?.disabled,
  ).toBe(true)
  await choose()
  await click('Consultar CRM')
  expect(api.readCrm).toHaveBeenCalledWith(
    connection,
    '/contacts',
    context,
    expect.any(AbortSignal),
  )
  expect(container.querySelector('script')).toBeNull()
  expect(container.querySelector('pre')?.textContent).toContain('<script>')
  expect(document.activeElement).toBe(container.querySelector('h3'))
})

it('load failure is not empty and retry recovers', async () => {
  api.listCrmConnections.mockRejectedValueOnce(
    new Error('private upstream text'),
  )
  await render()
  expect(container.textContent).not.toContain('private upstream')
  expect(container.textContent).not.toContain('no tiene conexiones')
  expect(container.querySelector('[role="alert"]')).not.toBeNull()
  await click('Actualizar conexiones CRM')
  expect(container.querySelectorAll('select')).toHaveLength(2)
})

it('unpaired is explicit, not a successful empty inventory', async () => {
  api.listCrmConnections.mockRejectedValueOnce(
    new ApiError('ignored', 409, { detail: { code: 'crm_not_associated' } }),
  )
  await render()
  expect(container.textContent).toContain('Conecta esta instancia a Enterprise')
  expect(container.querySelector('select')).toBeNull()
})

it('revocation discards selection and requires fresh human review', async () => {
  api.readCrm.mockRejectedValueOnce(new ApiError('secret not echoed', 403, {}))
  await render()
  await choose()
  await click('Consultar CRM')
  expect(container.textContent).toContain(
    'El acceso o la conexión han cambiado',
  )
  expect(container.querySelector('select')).toBeNull()
  expect(api.readCrm).toHaveBeenCalledTimes(1)
})

it('stop waiting aborts transport and a late response cannot reappear', async () => {
  let resolve!: (value: unknown) => void
  api.readCrm.mockReturnValue(
    new Promise((done) => {
      resolve = done
    }),
  )
  await render()
  await choose()
  await click('Consultar CRM')
  const signal = api.readCrm.mock.calls[0][3] as AbortSignal
  await click('Dejar de esperar')
  expect(signal.aborted).toBe(true)
  await act(async () => resolve({ data: 'late secret' }))
  expect(container.textContent).not.toContain('late secret')
  expect(container.textContent).toContain('puede continuar en Enterprise')
})

it('refresh during read discards old context and ignores late data', async () => {
  let resolve!: (value: unknown) => void
  api.readCrm.mockReturnValue(
    new Promise((done) => {
      resolve = done
    }),
  )
  await render()
  await choose()
  await click('Consultar CRM')
  await click('Actualizar conexiones CRM')
  await act(async () => resolve({ data: 'old organisation' }))
  expect(container.textContent).not.toContain('old organisation')
  expect(container.querySelector<HTMLSelectElement>('select')?.value).toBe('')
})

it('unmount aborts pending inventory', async () => {
  api.listCrmConnections.mockReturnValue(new Promise(() => {}))
  await render()
  const signal = api.listCrmConnections.mock.calls[0][0] as AbortSignal
  act(() => root.render(null))
  expect(signal.aborted).toBe(true)
})
