import { reduceLifecycle, type UiState, type StageId, type FailureCode, type ProgressUnit } from './lifecycle.js'

export interface BootstrapSnapshot {
  sequence: number
  attempt_id: number
  last_stage: StageId | null
  point_of_no_return: boolean
  event:
    | {kind:'stage';stage:StageId}
    | {kind:'progress';stage:StageId;done:number;total:number|null;unit:ProgressUnit}
    | {kind:'done';stage:StageId;duration_ms:number}
    | {kind:'failed';code:FailureCode;retryable:boolean}
    | {kind:'ready'}
    | {kind:'reconnecting';reason:'token_missing'|'engine_restarted'}
}

const LABELS: Record<StageId,string> = {
  preflight:'Comprobando este equipo', runtime_staging:'Preparando la aplicación',
  machine:'Preparando el espacio seguro', pull_engine:'Descargando Safent',
  pull_companion:'Descargando Anuncios', container:'Iniciando el espacio seguro',
  health:'Comprobando la conexión', companion_scaffold:'Preparando Anuncios',
  companion_up:'Iniciando Anuncios', companion_reload:'Conectando Anuncios',
  backup:'Creando respaldo', restore:'Restaurando respaldo', cleanup:'Terminando',
}

/** Replay only the current typed state using the existing lifecycle reducer. */
export function reduceBootstrapSnapshot(state: UiState, snapshot: BootstrapSnapshot): UiState {
  const event=snapshot.event
  const enterStage=(stage:StageId) => {
    state=reduceLifecycle(state,{source:'engine',event:{kind:'stage',stage,label:LABELS[stage] ?? 'Preparando tu espacio',total_bytes:null,point_of_no_return:snapshot.point_of_no_return}})
  }
  // A new renderer may have missed the stage before progress/failure. Restore
  // that context without reactivating a completed stage on each live tick.
  if (snapshot.last_stage && (state.kind!=='preparing' || !state.stages.some(stage=>stage.id===snapshot.last_stage))) enterStage(snapshot.last_stage)
  switch(event.kind){
    case 'stage': enterStage(event.stage); return state
    case 'progress': return reduceLifecycle(state,{source:'engine',event})
    case 'done': return reduceLifecycle(state,{source:'engine',event:{kind:'done',stage:event.stage,ms:event.duration_ms}})
    case 'failed': return reduceLifecycle(state,{source:'engine',event:{kind:'failed',code:event.code,retryable:event.retryable,detail:'El diagnóstico de arranque conserva el código y la etapa; no recopila mensajes privados.'}})
    case 'ready': return {kind:'ready'}
    case 'reconnecting': return reduceLifecycle(state,{source:'reconnect',reason:event.reason})
  }
}
