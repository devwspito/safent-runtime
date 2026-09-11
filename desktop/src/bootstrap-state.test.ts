// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { subscribeToBootstrapState } from './ipc.js'
import { requestCancel, requestRetry } from './ipc.js'
import { reduceBootstrapSnapshot, type BootstrapSnapshot } from './bootstrap-state.js'
import { initialState, reduceLifecycle, type UiState } from './lifecycle.js'
afterEach(()=>vi.unstubAllGlobals())
const failure:BootstrapSnapshot={sequence:1,attempt_id:1,last_stage:null,point_of_no_return:false,event:{kind:'failed',code:'runtime_hash_mismatch',retryable:true}}

it('replays a failure emitted before the renderer listener existed',async()=>{
  const listen=vi.fn().mockResolvedValue(()=>{})
  const invoke=vi.fn().mockResolvedValue(failure)
  vi.stubGlobal('__TAURI__',{event:{listen},core:{invoke}})
  let state:UiState=initialState
  await subscribeToBootstrapState(snapshot=>{state=reduceBootstrapSnapshot(state,snapshot)})
  expect(state).toMatchObject({kind:'failed',code:'runtime_hash_mismatch'})
  expect(listen.mock.invocationCallOrder[0]).toBeLessThan(invoke.mock.invocationCallOrder[0])
})
it('a live retry stage wins over an old failed snapshot still in flight',async()=>{
  let handler!:(msg:{payload:BootstrapSnapshot})=>void
  let resolve!:(value:BootstrapSnapshot)=>void
  const invoke=vi.fn(()=>new Promise<BootstrapSnapshot>(yes=>{resolve=yes}))
  vi.stubGlobal('__TAURI__',{event:{listen:vi.fn(async(_channel,fn)=>{handler=fn;return()=>{}})},core:{invoke}})
  const accept=vi.fn()
  const pending=subscribeToBootstrapState(accept)
  await Promise.resolve()
  handler({payload:{sequence:3,attempt_id:2,last_stage:'preflight',point_of_no_return:false,event:{kind:'stage',stage:'preflight'}}})
  resolve(failure);await pending
  handler({payload:failure})
  expect(accept).toHaveBeenCalledTimes(1)
  expect(accept.mock.calls[0][0].sequence).toBe(3)
})
it('snapshot error removes its listener and is not a healthy empty result',async()=>{
  const unlisten=vi.fn()
  vi.stubGlobal('__TAURI__',{event:{listen:vi.fn().mockResolvedValue(unlisten)},core:{invoke:vi.fn().mockRejectedValue(new Error('offline'))}})
  await expect(subscribeToBootstrapState(()=>{})).rejects.toThrow('offline')
  expect(unlisten).toHaveBeenCalledOnce()
})
it('progress replay restores native cancellation gate and real counters',()=>{
  const state=reduceBootstrapSnapshot(initialState,{sequence:7,attempt_id:1,last_stage:'container',point_of_no_return:true,event:{kind:'progress',stage:'container',done:2,total:4,unit:'steps'}})
  expect(state).toMatchObject({kind:'preparing',cancelable:false,stages:[{id:'container',done:2,total:4,unit:'steps'}]})
})
it('new failure finishes a pending retry without inventing success',()=>{
  const failed=reduceBootstrapSnapshot(initialState,failure)
  const retry=reduceLifecycle(failed,{source:'retry-requested'})
  expect(retry).toMatchObject({kind:'failed',retrying:true})
  expect(reduceBootstrapSnapshot(retry,{...failure,sequence:2})).toMatchObject({kind:'failed',retrying:false})
})

it('cancel and retry IPC are bound to the rendered attempt id',async()=>{
  const invoke=vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('__TAURI__',{core:{invoke}})
  await requestCancel(41); await requestRetry(41); await requestCancel(42)
  expect(invoke.mock.calls).toEqual([
    ['cancel_bootstrap',{attemptId:41}],['retry_bootstrap',{attemptId:41}],['cancel_bootstrap',{attemptId:42}],
  ])
})
it('honoured cancellation allows explicit retry and a second cancellation',()=>{
  let state=reduceBootstrapSnapshot(initialState,{...failure,event:{kind:'failed',code:'cancelled_by_owner',retryable:true}})
  expect(state).toMatchObject({kind:'failed',retryable:true,retrying:false})
  state=reduceLifecycle(state,{source:'retry-requested'})
  state=reduceBootstrapSnapshot(state,{sequence:2,attempt_id:2,last_stage:'machine',point_of_no_return:false,event:{kind:'stage',stage:'machine'}})
  expect(state).toMatchObject({kind:'preparing',cancelable:true})
  state=reduceBootstrapSnapshot(state,{sequence:3,attempt_id:2,last_stage:'machine',point_of_no_return:false,event:{kind:'failed',code:'cancelled_by_owner',retryable:true}})
  expect(state).toMatchObject({kind:'failed',code:'cancelled_by_owner',retryable:true,stageId:'machine'})
})
