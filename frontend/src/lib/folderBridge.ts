/**
 * folderBridge — browser-mediated, per-task "bridge" to a real host folder.
 *
 * The container is in podman's VM and can't kernel-mount a host folder chosen at
 * runtime, and the browser is sandboxed on the user's machine. So a per-task
 * "bridge" is mediated by the browser via the File System Access API (Chromium):
 *
 *   1. showDirectoryPicker() → the OS folder picker returns a read-WRITE handle.
 *   2. uploadDirectoryToBridge() recursively uploads the folder into
 *      workspace/bridge/<name>/… so the agent works on it this task.
 *   3. syncBridgeToHost() writes the (possibly agent-modified) files back to the
 *      real host folder through the handle → no lasting duplicate version.
 *
 * Chromium-only (Chrome/Edge). Requires a secure context (localhost is fine).
 */
import { token } from './token'
import { listWorkspaceFiles } from '../api/client'
import type { WorkspaceFile } from '../api/types'

export interface BridgeSelection {
  /** Sanitised folder name (used as the workspace subdir). */
  name: string
  /** Absolute path in the container (what the agent reads), e.g.
   *  /var/lib/hermes/workspace/bridge/<name>. */
  workspacePath: string
  /** Relative base under the workspace: bridge/<name>. */
  relBase: string
  /** The picked directory handle (kept for write-back this session). */
  dirHandle: FileSystemDirectoryHandle
  fileCount: number
}

const MAX_FILES = 2000
const MAX_FILE_BYTES = 25 * 1024 * 1024
// Directories that are noise / huge and should never be bridged.
const SKIP_DIRS = new Set([
  '.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build',
  '.next', '.turbo', '.cache', 'target', '.idea', '.DS_Store',
])

// The DOM lib doesn't ship the full File System Access API surface across TS
// versions; describe the bits we use as a standalone structural type and bridge
// with `as unknown as DirHandle` at the boundaries.
interface FileEntryHandle {
  kind: 'file'
  name: string
  getFile(): Promise<File>
  createWritable(): Promise<{ write(d: Blob): Promise<void>; close(): Promise<void> }>
}
interface DirHandle {
  kind: 'directory'
  name: string
  entries(): AsyncIterableIterator<[string, DirHandle | FileEntryHandle]>
  getDirectoryHandle(name: string, opts?: { create?: boolean }): Promise<DirHandle>
  getFileHandle(name: string, opts?: { create?: boolean }): Promise<FileEntryHandle>
  queryPermission?(o: { mode: string }): Promise<string>
  requestPermission?(o: { mode: string }): Promise<string>
}

async function* walk(
  dir: DirHandle,
  prefix = '',
): AsyncGenerator<{ file: File; rel: string }> {
  for await (const [name, handle] of dir.entries()) {
    const rel = prefix ? `${prefix}/${name}` : name
    if (handle.kind === 'directory') {
      if (SKIP_DIRS.has(name)) continue
      yield* walk(handle, rel)
    } else {
      const file = await handle.getFile()
      yield { file, rel }
    }
  }
}

async function uploadOne(file: File, relPath: string): Promise<string> {
  const tok = token()
  const body = new FormData()
  body.append('file', file, file.name)
  body.append('rel_path', relPath)
  const headers: Record<string, string> = {}
  if (tok) headers['Authorization'] = `Bearer ${tok}`
  const res = await fetch('/api/v1/workspace/files', { method: 'POST', headers, body })
  if (!res.ok) throw new Error(`upload ${relPath}: HTTP ${res.status}`)
  const j = (await res.json()) as { path?: string }
  return j.path ?? ''
}

function sanitizeName(raw: string): string {
  return (raw || 'folder').replace(/[^a-zA-Z0-9._-]/g, '_').slice(0, 60) || 'folder'
}

/** Upload the picked folder into workspace/bridge/<name>/… preserving structure. */
export async function uploadDirectoryToBridge(
  dirHandle: FileSystemDirectoryHandle,
): Promise<BridgeSelection> {
  const name = sanitizeName(dirHandle.name)
  const relBase = `bridge/${name}`
  let count = 0
  let firstAbs = ''
  for await (const { file, rel } of walk(dirHandle as unknown as DirHandle)) {
    if (count >= MAX_FILES) break
    if (file.size > MAX_FILE_BYTES) continue
    const relPath = `${relBase}/${rel}`
    const abs = await uploadOne(file, relPath)
    if (!firstAbs && abs) firstAbs = abs
    count++
  }
  // Derive the absolute bridge base from a returned absolute path (honours
  // HERMES_WORKSPACE_DIR); fall back to the documented default.
  let workspacePath = `/var/lib/hermes/workspace/${relBase}`
  const marker = `/${relBase}/`
  const idx = firstAbs.indexOf(marker)
  if (idx >= 0) workspacePath = firstAbs.slice(0, idx + `/${relBase}`.length)
  return { name, workspacePath, relBase, dirHandle, fileCount: count }
}

async function ensureDir(root: DirHandle, parts: string[]): Promise<DirHandle> {
  let cur = root
  for (const p of parts) cur = await cur.getDirectoryHandle(p, { create: true })
  return cur
}

/** Recursively list every FILE under a workspace subdir (via the REST listing). */
async function listWorkspaceFilesRecursive(relBase: string): Promise<WorkspaceFile[]> {
  const out: WorkspaceFile[] = []
  const stack = [relBase]
  while (stack.length) {
    const dir = stack.pop() as string
    const entries = await listWorkspaceFiles(dir)
    for (const e of entries) {
      if (e.is_dir) stack.push(e.path)
      else out.push(e)
    }
  }
  return out
}

/**
 * Write the (agent-modified) bridge files back to the real host folder through
 * the picked handle. Overwrites files in place → the user's folder ends up with
 * the results, no duplicate version. Returns the number of files written.
 */
export async function syncBridgeToHost(sel: BridgeSelection): Promise<number> {
  const root = sel.dirHandle as unknown as DirHandle
  // Ask for write permission if not already granted.
  if (root.requestPermission) {
    const perm = await root.requestPermission({ mode: 'readwrite' })
    if (perm !== 'granted') throw new Error('Permiso de escritura denegado sobre la carpeta.')
  }

  const files = await listWorkspaceFilesRecursive(sel.relBase)
  const tok = token()
  let written = 0
  for (const f of files) {
    // f.path is relative to the workspace, e.g. bridge/<name>/src/index.ts.
    const relToBridge = f.path.startsWith(sel.relBase + '/')
      ? f.path.slice(sel.relBase.length + 1)
      : f.name
    const parts = relToBridge.split('/')
    const fileName = parts.pop() as string
    const dir = parts.length ? await ensureDir(root, parts) : root

    const url = `/api/v1/workspace/download?path=${encodeURIComponent(f.path)}`
    const res = await fetch(url, {
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
    })
    if (!res.ok) continue
    const blob = await res.blob()
    const fh = await dir.getFileHandle(fileName, { create: true })
    const w = await fh.createWritable()
    await w.write(blob)
    await w.close()
    written++
  }
  return written
}

/** True when the browser supports the native folder picker (Chromium). */
export function supportsFolderPicker(): boolean {
  return typeof (window as unknown as { showDirectoryPicker?: unknown }).showDirectoryPicker === 'function'
}

/** Open the OS folder picker (read-write). Returns null if the user cancels. */
export async function pickHostDirectory(): Promise<FileSystemDirectoryHandle | null> {
  const fn = (window as unknown as {
    showDirectoryPicker?: (o?: { mode?: string }) => Promise<FileSystemDirectoryHandle>
  }).showDirectoryPicker
  if (!fn) throw new Error('Tu navegador no soporta seleccionar carpetas (usa Chrome/Edge).')
  try {
    return await fn({ mode: 'readwrite' })
  } catch (e) {
    if ((e as Error).name === 'AbortError') return null
    throw e
  }
}
