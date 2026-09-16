import { afterEach, expect, it, vi } from 'vitest'
import { listWorkspaceFiles } from '../api/client'
import { pickHostDirectory, supportsFolderPicker, syncBridgeToHost, uploadDirectoryToBridge } from './folderBridge'

vi.mock('../api/client', () => ({ listWorkspaceFiles: vi.fn() }))
vi.mock('./token', () => ({ token: () => '' }))
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })
const id = 'a'.repeat(32), batch = 'b'.repeat(32)
function setup(files = [{ path: 'image.svg', size: 3 }]) {
  const manifest = { id, name: 'My Kit', files, total_bytes: files.reduce((n, f) => n + f.size, 0) }
  const invoke = vi.fn(async (command: string) => {
    if (command === 'pick_host_folder') return manifest
    if (command === 'read_host_folder_file') return btoa('abc')
    if (command === 'approve_host_folder_write') return batch
    return null
  })
  vi.stubGlobal('__TAURI__', { core: { invoke } })
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ path: '' }), blob: async () => ({ size: 3, arrayBuffer: async () => new Uint8Array([97, 98, 99]).buffer }) })
  vi.stubGlobal('fetch', fetch)
  return { invoke, fetch, manifest }
}
it('native picker works without Chromium and exposes no host path', async () => {
  const { invoke } = setup()
  expect(supportsFolderPicker()).toBe(true)
  const handle = await pickHostDirectory()
  expect(handle).toEqual({ name: 'My Kit', kind: 'directory' })
  expect(invoke).toHaveBeenCalledExactlyOnceWith('pick_host_folder', undefined)
  expect(JSON.stringify(handle)).not.toContain(id)
})
it('cancel does not import or write anything', async () => {
  const { invoke, fetch } = setup()
  invoke.mockResolvedValueOnce(null)
  expect(await pickHostDirectory()).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})
it('imports every selected file through relative capability calls into a unique workspace', async () => {
  const { invoke, fetch } = setup()
  const handle = (await pickHostDirectory())!
  const selected = await uploadDirectoryToBridge(handle)
  expect(selected.fileCount).toBe(1)
  expect(selected.relBase).toMatch(/^bridge\/My_Kit-/)
  expect(invoke).toHaveBeenCalledWith('read_host_folder_file', { id, path: 'image.svg' })
  expect(fetch).toHaveBeenCalledTimes(1)
  expect((await uploadDirectoryToBridge(handle)).relBase).not.toBe(selected.relBase)
  expect(invoke.mock.calls.some(([command]) => command.includes('write'))).toBe(false)
})
it.each(['../secret', '/etc/passwd', 'sub/../secret', 'sub//file', 'sub\\file'])('rejects unsafe native manifest %s before import', async path => {
  const { fetch } = setup([{ path, size: 3 }])
  await expect(pickHostDirectory()).rejects.toThrow('rutas no admitidas')
  expect(fetch).not.toHaveBeenCalled()
})
it('reports size limits instead of silently skipping files', async () => {
  const { fetch } = setup([{ path: 'large', size: 26 * 1024 * 1024 }])
  await expect(pickHostDirectory()).rejects.toThrow('25 MB')
  expect(fetch).not.toHaveBeenCalled()
})
it('does not echo native errors or try an arbitrary fallback', async () => {
  const { invoke, fetch } = setup()
  invoke.mockRejectedValueOnce(new Error('/private/secret key123'))
  await expect(pickHostDirectory()).rejects.toThrow('Vuelve a seleccionarla')
  expect(invoke).toHaveBeenCalledTimes(1)
  expect(fetch).not.toHaveBeenCalled()
})
it('writes only after native approval and uses the exact batch/relative path', async () => {
  const { invoke } = setup()
  const selected = await uploadDirectoryToBridge((await pickHostDirectory())!)
  vi.mocked(listWorkspaceFiles).mockResolvedValue([{ path: `${selected.relBase}/image.svg`, name: 'image.svg', size: 3 }])
  expect(await syncBridgeToHost(selected)).toBe(1)
  expect(invoke).toHaveBeenCalledWith('approve_host_folder_write', { id, files: [{ path: 'image.svg', size: 3 }] })
  expect(invoke).toHaveBeenCalledWith('write_host_folder_file', { id, batchId: batch, path: 'image.svg', data: 'YWJj' })
})
it('cancelled native write approval leaves host untouched', async () => {
  const { invoke } = setup()
  const selected = await uploadDirectoryToBridge((await pickHostDirectory())!)
  vi.mocked(listWorkspaceFiles).mockResolvedValue([{ path: `${selected.relBase}/image.svg`, name: 'image.svg', size: 3 }])
  invoke.mockResolvedValueOnce(null)
  await expect(syncBridgeToHost(selected)).rejects.toThrow('Guardado cancelado')
  expect(invoke.mock.calls.some(([command]) => command === 'write_host_folder_file')).toBe(false)
})
it('rejects workspace escape without any host write approval', async () => {
  const { invoke } = setup()
  const selected = await uploadDirectoryToBridge((await pickHostDirectory())!)
  vi.mocked(listWorkspaceFiles).mockResolvedValue([{ path: 'other/file', name: 'file', size: 3 }])
  await expect(syncBridgeToHost(selected)).rejects.toThrow('rutas no admitidas')
  expect(invoke.mock.calls.some(([command]) => command.includes('write'))).toBe(false)
})
it('reports partial save count and stops at the first failed file', async () => {
  const { invoke } = setup([{ path: 'one', size: 3 }, { path: 'two', size: 3 }])
  const selected = await uploadDirectoryToBridge((await pickHostDirectory())!)
  vi.mocked(listWorkspaceFiles).mockResolvedValue(['one', 'two'].map(name => ({ path: `${selected.relBase}/${name}`, name, size: 3 })))
  invoke.mockResolvedValueOnce(batch).mockResolvedValueOnce(null).mockRejectedValueOnce(new Error('private detail'))
  await expect(syncBridgeToHost(selected)).rejects.toThrow('Guardados 1 de 2 archivos')
})
it('preserves Chromium picker cancellation without native IPC', async () => {
  vi.stubGlobal('__TAURI__', undefined)
  vi.stubGlobal('showDirectoryPicker', vi.fn().mockRejectedValue(new DOMException('cancel', 'AbortError')))
  expect(supportsFolderPicker()).toBe(true)
  expect(await pickHostDirectory()).toBeNull()
})
