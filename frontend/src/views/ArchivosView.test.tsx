import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const api=vi.hoisted(()=>({listWorkspaceFiles:vi.fn(),uploadWorkspaceFile:vi.fn()}))
vi.mock('../api/client',async()=>({...await vi.importActual('../api/client'),...api}))
import ArchivosView from './ArchivosView'
function deferred<T>(){let resolve!:(v:T)=>void;const promise=new Promise<T>(r=>{resolve=r});return{promise,resolve}}
let root:Root;let host:HTMLDivElement
beforeEach(()=>{
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true)
  api.listWorkspaceFiles.mockReset().mockResolvedValue([{name:'a.txt',path:'a.txt',kind:'text'},{name:'b.txt',path:'b.txt',kind:'text'}]);api.uploadWorkspaceFile.mockReset()
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response('Contenido seguro')))
  host=document.createElement('div');document.body.append(host);root=createRoot(host)
})
afterEach(()=>{act(()=>root.unmount());host.remove();vi.unstubAllGlobals()})
async function render(){await act(async()=>root.render(<ArchivosView/>))}
async function open(index:number){await act(async()=>{(host.querySelectorAll('[role="button"]')[index] as HTMLElement).click()})}
it('HTTP preview errors are not shown as file content and offer retry',async()=>{
  vi.mocked(fetch).mockResolvedValue(new Response('Private HTTP error body',{status:403}))
  await render();await open(0)
  expect(document.querySelector('[role="dialog"] [role="alert"]')).not.toBeNull()
  expect(document.body.textContent).not.toContain('Private HTTP error body')
})
it('a preview finishing after selection changes cannot leak into the new file',async()=>{
  const first=deferred<Response>();vi.mocked(fetch).mockReturnValueOnce(first.promise)
  await render();await open(0);await open(1)
  await act(async()=>first.resolve(new Response('Obsoleto A')))
  expect(document.querySelector('pre')?.textContent).toBe('Contenido seguro')
})
it('invalid browse data is recoverable error, not an empty folder',async()=>{
  api.listWorkspaceFiles.mockResolvedValue(null);await render()
  expect(host.querySelector('[role="alert"]')).not.toBeNull()
})
it('empty text preview is distinguishable from failed preview and retries recover',async()=>{
  vi.mocked(fetch).mockResolvedValueOnce(new Response('error',{status:500})).mockResolvedValueOnce(new Response(''))
  await render();await open(0)
  const retry=Array.from(document.querySelectorAll('[role="dialog"] button')).find(b=>b.textContent==='Reintentar') as HTMLButtonElement
  expect(retry).toBeDefined();await act(async()=>retry.click())
  expect(document.querySelector('[role="dialog"] [role="alert"]')).toBeNull()
  expect(document.body.textContent).toContain('Este archivo está vacío.')
})
it('stale browse cannot replace the folder selected meanwhile',async()=>{
  const folder=deferred<unknown>()
  api.listWorkspaceFiles.mockResolvedValueOnce([{name:'Carpeta',path:'folder',kind:'directory'}]).mockReturnValueOnce(folder.promise).mockResolvedValueOnce([{name:'new.txt',path:'new.txt'}])
  await render();await open(0)
  const rootButton=host.querySelector('nav button') as HTMLButtonElement
  await act(async()=>rootButton.click())
  await act(async()=>folder.resolve([{name:'stale.txt',path:'folder/stale.txt'}]))
  expect(host.textContent).toContain('new.txt');expect(host.textContent).not.toContain('stale.txt')
})
it('an upload completion does not navigate away from a folder selected during upload',async()=>{
  const upload=deferred<unknown>();api.uploadWorkspaceFile.mockReturnValueOnce(upload.promise)
  api.listWorkspaceFiles.mockResolvedValueOnce([{name:'Carpeta',path:'folder',kind:'directory'}]).mockResolvedValueOnce([{name:'inside.txt',path:'folder/inside.txt'}])
  await render()
  const input=host.querySelector('input[type="file"]')!
  Object.defineProperty(input,'files',{value:[new File(['x'],'upload.txt')],configurable:true})
  await act(async()=>input.dispatchEvent(new Event('change',{bubbles:true})))
  await open(0);await act(async()=>upload.resolve({}))
  expect(host.textContent).toContain('inside.txt');expect(api.listWorkspaceFiles).toHaveBeenCalledTimes(2)
})
it('unmount aborts preview and prevents later state resurrection',async()=>{
  const preview=deferred<Response>();vi.mocked(fetch).mockReturnValueOnce(preview.promise)
  await render();await open(0)
  const signal=vi.mocked(fetch).mock.calls[0][1]?.signal
  await act(async()=>root.render(<div/>))
  expect(signal?.aborted).toBe(true)
  await act(async()=>preview.resolve(new Response('Late')))
  expect(document.querySelector('pre')).toBeNull()
})
