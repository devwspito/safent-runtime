/** Session-only folder bridge: Chromium handles or native picker-scoped capabilities. */
import { token } from './token'
import { listWorkspaceFiles } from '../api/client'
import type { WorkspaceFile } from '../api/types'

export interface BridgeSelection {
  name: string
  workspacePath: string
  relBase: string
  dirHandle: FileSystemDirectoryHandle
  fileCount: number
}
type Invoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>
type NativeManifest = { id: string; name: string; files: { path: string; size: number }[]; total_bytes: number }
const nativeHandles = new WeakMap<FileSystemDirectoryHandle, NativeManifest>()
const MAX_FILES = 2000
const MAX_FILE_BYTES = 25 * 1024 * 1024
const MAX_TOTAL_BYTES = 256 * 1024 * 1024
const LIMIT = 'Límite de carpeta: 2.000 archivos, 25 MB por archivo y 256 MB en total. Elige una subcarpeta; no se ha importado parcialmente.'
const FAILED = 'No se pudo acceder a la carpeta. Vuelve a seleccionarla.'
const UNSAFE = 'La carpeta contiene enlaces simbólicos, archivos especiales o rutas no admitidas. Elige una carpeta sin enlaces.'
const CONFLICT = 'Un archivo cambió en tu carpeta. No se ha sobrescrito; vuelve a seleccionar la carpeta.'

interface FileEntryHandle {
  kind: 'file'; name: string
  getFile(): Promise<File>
  createWritable(): Promise<{ write(d: Blob): Promise<void>; close(): Promise<void> }>
}
interface DirHandle {
  kind: 'directory'; name: string
  entries(): AsyncIterableIterator<[string, DirHandle | FileEntryHandle]>
  getDirectoryHandle(name: string, opts?: { create?: boolean }): Promise<DirHandle>
  getFileHandle(name: string, opts?: { create?: boolean }): Promise<FileEntryHandle>
  requestPermission?(o: { mode: string }): Promise<string>
}
function nativeInvoke(): Invoke | undefined {
  return (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke
}
async function native(command: string, args?: Record<string, unknown>): Promise<unknown> {
  try {
    const invoke = nativeInvoke()
    if (!invoke) throw new Error(FAILED)
    return await invoke(command, args)
  } catch (error) {
    const message = typeof error === 'string' ? error : error instanceof Error ? error.message : ''
    throw new Error([LIMIT, UNSAFE, CONFLICT].includes(message) ? message : FAILED)
  }
}
function safePath(path: string): boolean {
  return path.length > 0 && path.length <= 1024 && !path.includes('\\')
    && ![...path].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
    && path.split('/').length <= 20
    && path.split('/').every(part => part !== '' && part !== '.' && part !== '..')
}
function validateFiles(files: { path: string; size: number }[]): number {
  let total = 0
  const seen = new Set<string>()
  if (files.length > MAX_FILES) throw new Error(LIMIT)
  for (const file of files) {
    if (!safePath(file.path) || seen.has(file.path)) throw new Error(UNSAFE)
    seen.add(file.path)
    if (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > MAX_FILE_BYTES) throw new Error(LIMIT)
    total += file.size
    if (total > MAX_TOTAL_BYTES) throw new Error(LIMIT)
  }
  return total
}
async function browserFiles(dir: DirHandle, prefix = '', out: { file: File; path: string; size: number }[] = [], visited = { count: 0 }) {
  for await (const [name, handle] of dir.entries()) {
    const path = prefix ? `${prefix}/${name}` : name
    if (!safePath(path)) throw new Error(UNSAFE)
    if (++visited.count > 10000) throw new Error(LIMIT)
    if (handle.kind === 'directory') await browserFiles(handle, path, out, visited)
    else {
      const file = await handle.getFile()
      out.push({ file, path, size: file.size })
      validateFiles(out)
    }
  }
  return out
}
async function uploadOne(file: File, relPath: string): Promise<string> {
  const tok = token()
  const body = new FormData()
  body.append('file', file, file.name)
  body.append('rel_path', relPath)
  const res = await fetch('/api/v1/workspace/files', { method: 'POST', headers: tok ? { Authorization: `Bearer ${tok}` } : {}, body })
  if (!res.ok) throw new Error('No se completó la importación de la carpeta. Puedes volver a intentarlo.')
  const result: unknown = await res.json()
  return typeof result === 'object' && result !== null && 'path' in result && typeof result.path === 'string' ? result.path : ''
}
function bytesFromBase64(value: unknown, expected: number): Uint8Array<ArrayBuffer> {
  if (typeof value !== 'string' || value.length > Math.ceil(MAX_FILE_BYTES / 3) * 4) throw new Error(FAILED)
  const raw = atob(value)
  if (raw.length !== expected) throw new Error(CONFLICT)
  return Uint8Array.from(raw, char => char.charCodeAt(0))
}
function base64FromBytes(bytes: Uint8Array): string {
  let text = ''
  for (let i = 0; i < bytes.length; i += 8192) text += String.fromCharCode(...bytes.subarray(i, i + 8192))
  return btoa(text)
}

export async function uploadDirectoryToBridge(dirHandle: FileSystemDirectoryHandle): Promise<BridgeSelection> {
  const name = (dirHandle.name || 'folder').replace(/[^a-zA-Z0-9._-]/g, '_').slice(0, 60) || 'folder'
  // A unique session directory avoids cross-task collisions between same-named folders.
  const relBase = `bridge/${name}-${crypto.randomUUID()}`
  const selected = nativeHandles.get(dirHandle)
  const files = selected ? selected.files : await browserFiles(dirHandle as unknown as DirHandle)
  validateFiles(files)
  let firstAbs = ''
  for (const entry of files) {
    const file = selected
      ? new File([bytesFromBase64(await native('read_host_folder_file', { id: selected.id, path: entry.path }), entry.size)], entry.path.split('/').pop() || 'file')
      : (entry as unknown as { file: File }).file
    const abs = await uploadOne(file, `${relBase}/${entry.path}`)
    if (!firstAbs) firstAbs = abs
  }
  let workspacePath = `/var/lib/hermes/workspace/${relBase}`
  const marker = `/${relBase}/`
  const index = firstAbs.indexOf(marker)
  if (index >= 0) workspacePath = firstAbs.slice(0, index + relBase.length + 1)
  return { name: dirHandle.name, workspacePath, relBase, dirHandle, fileCount: files.length }
}

async function listBridgeFiles(base: string): Promise<WorkspaceFile[]> {
  const out: WorkspaceFile[] = [], stack = [base], seen = new Set<string>()
  while (stack.length) {
    const dir = stack.pop()!
    if (seen.has(dir) || seen.size >= 10000) throw new Error(UNSAFE)
    seen.add(dir)
    for (const entry of await listWorkspaceFiles(dir)) {
      if (!entry.path.startsWith(base + '/') || !safePath(entry.path.slice(base.length + 1))) throw new Error(UNSAFE)
      if (entry.is_dir) stack.push(entry.path)
      else out.push(entry)
      if (out.length > MAX_FILES) throw new Error(LIMIT)
    }
  }
  validateFiles(out.map(file => ({ path: file.path.slice(base.length + 1), size: file.size })))
  return out
}
async function download(file: WorkspaceFile): Promise<Blob> {
  const tok = token()
  const response = await fetch(`/api/v1/workspace/download?path=${encodeURIComponent(file.path)}`, { headers: tok ? { Authorization: `Bearer ${tok}` } : {} })
  if (!response.ok) throw new Error(FAILED)
  const blob = await response.blob()
  if (blob.size !== file.size || blob.size > MAX_FILE_BYTES) throw new Error(CONFLICT)
  return blob
}

/** Explicit save only. Native writes additionally require a native one-batch confirmation. */
export async function syncBridgeToHost(selection: BridgeSelection): Promise<number> {
  const files = await listBridgeFiles(selection.relBase)
  const selected = nativeHandles.get(selection.dirHandle)
  let batch: unknown
  const root = selection.dirHandle as unknown as DirHandle
  if (selected) {
    batch = await native('approve_host_folder_write', { id: selected.id, files: files.map(file => ({ path: file.path.slice(selection.relBase.length + 1), size: file.size })) })
    if (batch === null) throw new Error('Guardado cancelado. Tu carpeta no se ha modificado.')
    if (typeof batch !== 'string' || !/^[A-Za-z0-9_-]{32}$/.test(batch)) throw new Error(FAILED)
  } else if (root.requestPermission && await root.requestPermission({ mode: 'readwrite' }) !== 'granted') {
    throw new Error('Permiso de escritura denegado sobre la carpeta.')
  }
  let written = 0
  try {
    for (const file of files) {
      const relative = file.path.slice(selection.relBase.length + 1)
      const blob = await download(file)
      if (selected) {
        await native('write_host_folder_file', { id: selected.id, batchId: batch, path: relative, data: base64FromBytes(new Uint8Array(await blob.arrayBuffer())) })
      } else {
        const parts = relative.split('/'), name = parts.pop()!
        let dir = root
        for (const part of parts) dir = await dir.getDirectoryHandle(part, { create: true })
        const writer = await (await dir.getFileHandle(name, { create: true })).createWritable()
        await writer.write(blob); await writer.close()
      }
      written++
    }
  } catch (error) {
    const reason = error instanceof Error && [FAILED, LIMIT, CONFLICT, UNSAFE].includes(error.message) ? error.message : FAILED
    throw new Error(`Guardados ${written} de ${files.length} archivos. ${reason}`)
  }
  return written
}
export function supportsFolderPicker(): boolean {
  return typeof nativeInvoke() === 'function' || typeof (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker === 'function'
}
export async function pickHostDirectory(): Promise<FileSystemDirectoryHandle | null> {
  if (nativeInvoke()) {
    const result = await native('pick_host_folder')
    if (result === null) return null
    if (!result || typeof result !== 'object') throw new Error(FAILED)
    const selected = result as NativeManifest
    if (typeof selected.id !== 'string' || !/^[A-Za-z0-9_-]{32}$/.test(selected.id) || typeof selected.name !== 'string' || !selected.name || !Array.isArray(selected.files)) throw new Error(FAILED)
    for (const file of selected.files) if (!file || typeof file.path !== 'string') throw new Error(FAILED)
    if (validateFiles(selected.files) !== selected.total_bytes) throw new Error(FAILED)
    const handle = Object.freeze({ name: selected.name, kind: 'directory' }) as FileSystemDirectoryHandle
    nativeHandles.set(handle, selected)
    return handle
  }
  const picker = (window as unknown as { showDirectoryPicker?: (o: { mode: string }) => Promise<FileSystemDirectoryHandle> }).showDirectoryPicker
  if (!picker) throw new Error('Seleccionar carpetas requiere Safent para escritorio o Chrome/Edge.')
  try { return await picker({ mode: 'readwrite' }) }
  catch (error) { if ((error as Error).name === 'AbortError') return null; throw new Error(FAILED) }
}
