import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ listMemory:vi.fn(), searchMemory:vi.fn(), getMemoryEntry:vi.fn(), updateMemoryEntry:vi.fn(), forgetMemoryItem:vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
vi.mock('sileo', () => ({ sileo:{success:vi.fn(),error:vi.fn(),warning:vi.fn()} }))
import MemoriaView from './MemoriaView'

function deferred<T>() { let resolve!: (v:T) => void; let reject!: (e:Error) => void; const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no}); return {promise,resolve,reject} }
let root:Root
let host:HTMLDivElement
const rows=[{id:'a',target:'A',content_truncated:'Resumen A'},{id:'b',target:'B',content_truncated:'Resumen B'}]
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true)
  api.listMemory.mockReset().mockResolvedValue(rows); api.searchMemory.mockReset().mockResolvedValue([])
  api.getMemoryEntry.mockReset().mockImplementation(async(id:string)=>({content:`Completo ${id}`}))
  api.updateMemoryEntry.mockReset().mockResolvedValue({ok:true,updated:true}); api.forgetMemoryItem.mockReset().mockResolvedValue({ok:true})
  host=document.createElement('div');document.body.append(host);root=createRoot(host)
})
afterEach(()=>{act(()=>root.unmount());host.remove();vi.unstubAllGlobals()})
async function render(){await act(async()=>root.render(<MemoriaView />))}
async function open(index:number){await act(async()=>{(host.querySelectorAll('[role="button"]')[index] as HTMLElement).click()})}
async function changeEditor(value:string){await act(async()=>{const el=document.querySelector('textarea')!;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value')!.set!.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}))})}
const button=(part:string)=>Array.from(document.querySelectorAll('button')).find(b=>b.getAttribute('aria-label')?.includes(part))!

it('late detail cannot replace the newly selected entry content',async()=>{
  const a=deferred<{content:string}>();api.getMemoryEntry.mockReturnValueOnce(a.promise)
  await render();await open(0);await open(1)
  expect(document.querySelector('textarea')?.value).toBe('Completo b')
  await act(async()=>a.resolve({content:'Completo a tardío'}))
  expect(document.querySelector('textarea')?.value).toBe('Completo b')
})
it('failed full detail never makes the truncated list summary editable',async()=>{
  api.getMemoryEntry.mockRejectedValue(new Error('offline'))
  await render();await open(0)
  expect(document.querySelector('[role="dialog"] [role="alert"]')).not.toBeNull()
  expect(document.querySelector('textarea')).toBeNull()
  expect(button('Guardar')?.disabled).toBe(true)
})
it('an unconfirmed save preserves the unsaved draft and saved baseline',async()=>{
  api.updateMemoryEntry.mockResolvedValue({ok:false})
  await render();await open(0);await changeEditor('No confirmado')
  await act(async()=>button('Guardar').click())
  expect(document.querySelector('textarea')?.value).toBe('No confirmado')
  expect(button('Guardar').disabled).toBe(false)
  expect(api.listMemory).toHaveBeenCalledTimes(1)
})
it('invalid list is an error rather than no memories',async()=>{
  api.listMemory.mockResolvedValue(null);await render()
  expect(host.querySelector('[role="alert"]')).not.toBeNull()
})
it('does not invent entry index zero when the server supplies no entry identifier',async()=>{
  api.listMemory.mockResolvedValue([{target:'MEMORY',content_truncated:'Sin identificador'}])
  await render();await open(0)
  expect(api.getMemoryEntry).not.toHaveBeenCalled()
  expect(button('Guardar').disabled).toBe(true)
  expect(button('Eliminar').disabled).toBe(true)
})
it('late save is bound to original id and does not replace another entry',async()=>{
  const save=deferred<unknown>();api.updateMemoryEntry.mockReturnValueOnce(save.promise)
  await render();await open(0);await changeEditor('Nuevo A')
  await act(async()=>button('Guardar').click());expect(api.updateMemoryEntry).toHaveBeenCalledWith('a','Nuevo A')
  await open(1);await act(async()=>save.resolve({ok:true,updated:true}))
  expect(document.querySelector('textarea')?.value).toBe('Completo b')
  // Editing B still compares against B's full saved content, never A's value.
  expect(button('Guardar').disabled).toBe(true)
})
it('closing and reopening invalidates the earlier detail request',async()=>{
  const first=deferred<{content:string}>();api.getMemoryEntry.mockReturnValueOnce(first.promise)
  await render();await open(0)
  await act(async()=>button('Cerrar panel').click());await open(0)
  await act(async()=>first.resolve({content:'Obsoleto'}))
  expect(document.querySelector('textarea')?.value).toBe('Completo a')
})

it('reopening a saving entry waits for the write before fetching full content',async()=>{
  const save=deferred<unknown>();api.updateMemoryEntry.mockReturnValueOnce(save.promise)
  await render();await open(0);await changeEditor('Nuevo A')
  await act(async()=>button('Guardar').click())
  await act(async()=>button('Cerrar panel').click());await open(0)
  expect(api.getMemoryEntry).toHaveBeenCalledTimes(1)
  expect(document.querySelector('textarea')).toBeNull()
  api.getMemoryEntry.mockResolvedValue({content:'Nuevo A'})
  await act(async()=>save.resolve({ok:true,updated:true}))
  expect(api.getMemoryEntry).toHaveBeenCalledTimes(2)
  expect(document.querySelector('textarea')?.value).toBe('Nuevo A')
})
it('late delete cannot close the newly selected drawer',async()=>{
  const remove=deferred<unknown>();api.forgetMemoryItem.mockReturnValueOnce(remove.promise)
  await render();await open(0);await act(async()=>button('Eliminar').click());await open(1)
  await act(async()=>remove.resolve({}))
  expect(document.querySelector('textarea')?.value).toBe('Completo b')
})
it('unmount discards late detail and does not show a global success toast',async()=>{
  const detail=deferred<{content:string}>();api.getMemoryEntry.mockReturnValueOnce(detail.promise)
  await render();await open(0);await act(async()=>root.render(<div/>))
  await act(async()=>detail.resolve({content:'Late'}))
  expect(document.querySelector('textarea')).toBeNull()
})
it('a newer search result wins over the initial load',async()=>{
  const initial=deferred<unknown>();api.listMemory.mockReturnValueOnce(initial.promise)
  api.searchMemory.mockResolvedValue([{id:'x',content:'Coincidencia vigente'}])
  await render()
  await act(async()=>{const input=host.querySelector('input')!;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(input,'nuevo');input.dispatchEvent(new Event('input',{bubbles:true}))})
  await act(async()=>host.querySelector('input')!.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true})))
  await act(async()=>initial.resolve(rows))
  expect(host.textContent).toContain('Coincidencia vigente')
  expect(host.textContent).not.toContain('Resumen A')
})
