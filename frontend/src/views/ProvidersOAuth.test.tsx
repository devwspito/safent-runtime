import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ startProviderOAuth:vi.fn(),getProviderOAuthStatus:vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
vi.mock('sileo',()=>({sileo:{success:vi.fn(),error:vi.fn(),warning:vi.fn()}}))
import { useProviderOAuthConnect } from './ProvidersView'
let root:Root, host:HTMLDivElement, connected:ReturnType<typeof vi.fn<() => void>>
function Probe(){const flow=useProviderOAuthConnect(connected);return <><button onClick={()=>void flow.startOAuthConnect('nous','Nous')}>Connect</button><output>{JSON.stringify(flow)}</output></>}
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
