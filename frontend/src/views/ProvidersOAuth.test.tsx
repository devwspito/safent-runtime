import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ startProviderOAuth:vi.fn(),getProviderOAuthStatus:vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
vi.mock('sileo',()=>({sileo:{success:vi.fn(),error:vi.fn(),warning:vi.fn()}}))
import { OAuthNotice, useProviderOAuthConnect } from './ProvidersView'
import { CODEX_DEVICE_URL } from '../lib/providerOAuth'
let root:Root, host:HTMLDivElement, connected:ReturnType<typeof vi.fn<() => void>>
function Probe(){const flow=useProviderOAuthConnect(connected);return <><button onClick={()=>void flow.startOAuthConnect('nous','Nous')}>Connect</button><output>{JSON.stringify(flow)}</output><OAuthNotice notice={flow.notice} onOpen={flow.openOAuthPage} openingBrowser={flow.openingBrowser}/></>}
const flow=()=>JSON.parse(host.querySelector('output')!.textContent!)
const response={session_id:'fake',verification_url:'https://auth.example.test/verify',user_code:'TEST-CODE',poll_interval:2,expires_in:60}
beforeEach(()=>{
  vi.useFakeTimers();vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true);vi.spyOn(window,'open').mockReturnValue(null)
  api.startProviderOAuth.mockReset().mockResolvedValue(response);api.getProviderOAuthStatus.mockReset().mockResolvedValue({status:'pending'})
  connected=vi.fn();host=document.createElement('div');document.body.append(host);root=createRoot(host)
})
afterEach(()=>{act(()=>root.unmount());host.remove();vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals()})
async function start(){await act(async()=>root.render(<Probe/>));await act(async()=>host.querySelector('button')!.click())}
it('keeps a visible URL and device code even if the popup is blocked',async()=>{
  await start();expect(flow().notice.url).toBe(response.verification_url);expect(flow().notice.code).toBe('TEST-CODE')
})
it('rejects an unsafe authorization URL without opening it',async()=>{
  api.startProviderOAuth.mockResolvedValue({...response,verification_url:'javascript:alert(1)'});await start()
  expect(window.open).not.toHaveBeenCalled();expect(flow().notice.error).toBe(true)
})
it('singleflights repeated clicks while authorization is pending',async()=>{
  await start();await act(async()=>host.querySelector('button')!.click());expect(api.startProviderOAuth).toHaveBeenCalledTimes(1)
})
it('does not open a late start response after leaving the view',async()=>{
  let resolve!:(v:unknown)=>void;api.startProviderOAuth.mockReturnValue(new Promise(r=>{resolve=r}))
  await start();act(()=>root.render(null));await act(async()=>resolve(response));expect(window.open).not.toHaveBeenCalled()
})
it('does not refresh another view after late polling approval',async()=>{
  let resolve!:(v:unknown)=>void;api.getProviderOAuthStatus.mockReturnValue(new Promise(r=>{resolve=r}))
  await start();await act(async()=>vi.advanceTimersByTimeAsync(2000));act(()=>root.render(null))
  await act(async()=>resolve({status:'approved'}));expect(connected).not.toHaveBeenCalled()
})
it('polling failure stops honestly without raw upstream text or unhandled rejection',async()=>{
  api.getProviderOAuthStatus.mockRejectedValue(new Error('secret upstream'));await start()
  await act(async()=>vi.advanceTimersByTimeAsync(2000));expect(flow().connectingId).toBeNull()
  expect(flow().notice.error).toBe(true);expect(host.textContent).not.toContain('secret upstream')
  await act(async()=>vi.advanceTimersByTimeAsync(10000));expect(api.getProviderOAuthStatus).toHaveBeenCalledTimes(1)
})
it('only a confirmed approval refreshes the provider list',async()=>{
  api.getProviderOAuthStatus.mockResolvedValue({status:'approved'});await start();await act(async()=>vi.advanceTimersByTimeAsync(2000))
  expect(connected).toHaveBeenCalledTimes(1);expect(flow().connectingId).toBeNull()
})
it('opens the Codex login through its dedicated native permission',async()=>{
  const invoke=vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('__TAURI__',{core:{invoke}})
  api.startProviderOAuth.mockResolvedValue({...response,verification_url:CODEX_DEVICE_URL})
  await start()
  expect(invoke).toHaveBeenCalledExactlyOnceWith('open_provider_oauth',{url:CODEX_DEVICE_URL})
  expect(window.open).not.toHaveBeenCalled()
  expect(flow().notice.error).toBe(false)
})
it('offers a native retry without restarting device auth or leaking launcher errors',async()=>{
  const invoke=vi.fn().mockRejectedValueOnce(new Error('sensitive native error')).mockResolvedValue(undefined)
  vi.stubGlobal('__TAURI__',{core:{invoke}})
  api.startProviderOAuth.mockResolvedValue({...response,verification_url:CODEX_DEVICE_URL})
  await start()
  expect(flow().notice.error).toBe(true)
  expect(flow().notice.code).toBe('TEST-CODE')
  expect(host.textContent).not.toContain('sensitive native error')
  const click=new MouseEvent('click',{bubbles:true,cancelable:true})
  await act(async()=>{host.querySelector('a')!.dispatchEvent(click)})
  expect(click.defaultPrevented).toBe(true)
  expect(invoke).toHaveBeenCalledTimes(2)
  expect(api.startProviderOAuth).toHaveBeenCalledTimes(1)
  expect(window.open).not.toHaveBeenCalled()
  expect(flow().notice.error).toBe(false)
  await act(async()=>vi.advanceTimersByTimeAsync(2000))
  expect(api.getProviderOAuthStatus).toHaveBeenCalledTimes(1)
})
it('singleflights the continuation link while the native launcher is running',async()=>{
  let resolve!:()=>void
  const invoke=vi.fn().mockReturnValue(new Promise<void>(r=>{resolve=r}))
  vi.stubGlobal('__TAURI__',{core:{invoke}})
  api.startProviderOAuth.mockResolvedValue({...response,verification_url:CODEX_DEVICE_URL})
  await start()
  expect(host.querySelector('a')!.getAttribute('aria-busy')).toBe('true')
  await act(async()=>{host.querySelector('a')!.click();host.querySelector('a')!.click()})
  expect(invoke).toHaveBeenCalledTimes(1)
  await act(async()=>resolve())
  expect(flow().openingBrowser).toBe(false)
})
it('a late launcher failure cannot overwrite confirmed device authorization',async()=>{
  let reject!:(reason:Error)=>void
  vi.stubGlobal('__TAURI__',{core:{invoke:vi.fn().mockReturnValue(new Promise((_,r)=>{reject=r}))}})
  api.startProviderOAuth.mockResolvedValue({...response,verification_url:CODEX_DEVICE_URL})
  api.getProviderOAuthStatus.mockResolvedValue({status:'approved'})
  await start();await act(async()=>vi.advanceTimersByTimeAsync(2000))
  const notice=flow().notice
  await act(async()=>reject(new Error('late sensitive error')))
  expect(flow().notice).toEqual(notice)
  expect(flow().notice.error).toBe(false)
  expect(connected).toHaveBeenCalledTimes(1)
})
