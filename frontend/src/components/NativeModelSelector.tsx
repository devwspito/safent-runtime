import { useEffect, useId, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { ApiError, getNativeModelCatalog, selectNativeModel, type NativeModelCatalog } from '../api/client'
import type { Provider } from '../api/types'
import { useFeatures } from '../hooks/useFeatures'
import { useLocale } from '../lib/i18n'
import { Button } from './ui/Button'
import styles from './NativeModelSelector.module.css'

const copy = {
  es: {
    open: 'Cambiar modelo', loading: 'Consultando modelos de tu cuenta…', label: 'Modelo de esta conexión',
    help: 'Conserva tu conexión con ChatGPT. No necesitas volver a iniciar sesión. El cambio se aplica a las próximas tareas.',
    save: 'Usar este modelo', saving: 'Guardando…', cancel: 'Cancelar', retry: 'Reintentar',
    loadError: 'No se pudieron consultar los modelos de tu cuenta. Tu conexión no ha cambiado.',
    saveError: 'No se pudo confirmar el cambio. Vuelve a consultar el modelo activo antes de intentarlo de nuevo.',
    denied: 'Esta sesión no puede cambiar el modelo. Revisa los permisos de Safent.',
    changed: 'La conexión o el modelo activo cambiaron. Vuelve a consultar las opciones.',
    saved: 'Modelo actualizado. Tu conexión se conserva.', refresh: 'Consultar de nuevo',
  },
  en: {
    open: 'Change model', loading: 'Checking models for your account…', label: 'Model for this connection',
    help: 'Keep your ChatGPT connection without signing in again. The change applies to upcoming tasks.',
    save: 'Use this model', saving: 'Saving…', cancel: 'Cancel', retry: 'Retry',
    loadError: 'Could not check the models for your account. Your connection has not changed.',
    saveError: 'Could not confirm the change. Check the active model again before retrying.',
    denied: 'This session cannot change the model. Check your Safent permissions.',
    changed: 'The connection or active model changed. Check the options again.',
    saved: 'Model updated. Your connection is preserved.', refresh: 'Check again',
  },
} as const

export default function NativeModelSelector({ provider, onChanged }: { provider: Provider; onChanged(): void }) {
  const { locale } = useLocale()
  const text = copy[locale]
  const { allowed } = useFeatures()
  const eligible = provider.provider_id === 'openai-codex' && provider.is_active === true && provider.managed_by !== 'cloud' && allowed('proveedores')
  const [open, setOpen] = useState(false)
  const [phase, setPhase] = useState<'idle' | 'loading' | 'ready' | 'saving' | 'load-error' | 'save-error' | 'success'>('idle')
  const [catalog, setCatalog] = useState<NativeModelCatalog | null>(null)
  const [selected, setSelected] = useState('')
  const [errorKind, setErrorKind] = useState<'denied' | 'changed' | null>(null)
  const requestId = useRef(0)
  const busy = useRef(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const selectRef = useRef<HTMLSelectElement>(null)
  const labelId = useId()

  useEffect(() => () => { requestId.current++; busy.current = false }, [])
  useEffect(() => {
    requestId.current++; busy.current = false
    setOpen(false); setPhase('idle'); setCatalog(null)
  }, [provider.provider_id, provider.default_model, eligible])
  useEffect(() => { if (phase === 'ready') selectRef.current?.focus() }, [phase])

  function close() {
    if (phase === 'saving') return
    requestId.current++; busy.current = false
    setOpen(false); setPhase('idle'); setCatalog(null)
    trigger.current?.focus()
  }
  function classify(error: unknown) {
    return error instanceof ApiError && (error.status === 401 || error.status === 403) ? 'denied'
      : error instanceof ApiError && error.status === 409 ? 'changed' : null
  }
  async function load() {
    if (!eligible || busy.current) return
    const request = ++requestId.current
    busy.current = true; setOpen(true); setPhase('loading'); setErrorKind(null); setCatalog(null)
    try {
      const result = await getNativeModelCatalog()
      if (request !== requestId.current) return
      setCatalog(result); setSelected(result.models.includes(result.active_model) ? result.active_model : '')
      setPhase('ready')
    } catch (error) {
      if (request !== requestId.current) return
      setPhase('load-error'); setErrorKind(classify(error))
    } finally { if (request === requestId.current) busy.current = false }
  }
  async function save() {
    if (!eligible || busy.current || !catalog || !catalog.models.includes(selected) || selected === catalog.active_model) return
    const request = ++requestId.current
    busy.current = true; setPhase('saving'); setErrorKind(null)
    try {
      await selectNativeModel({ provider_id: catalog.provider_id, active_model: catalog.active_model, model: selected })
      if (request !== requestId.current) return
      setPhase('success'); setCatalog(null); setOpen(false); onChanged(); trigger.current?.focus()
    } catch (error) {
      if (request !== requestId.current) return
      setPhase('save-error'); setErrorKind(classify(error)); setCatalog(null)
    } finally { if (request === requestId.current) busy.current = false }
  }
  if (!eligible) return null
  const error = errorKind ? text[errorKind] : phase === 'save-error' ? text.saveError : text.loadError
  return <div className={styles.root}>
    <Button ref={trigger} type="button" variant="secondary" size="sm" aria-expanded={open} onClick={() => { if (open) close(); else void load() }} disabled={phase === 'saving'}>{text.open}</Button>
    {phase === 'success' && <p role="status">{text.saved}</p>}
    {open && <div className={styles.panel} onKeyDown={event => { if (event.key === 'Escape' && phase !== 'saving') { event.stopPropagation(); close() } }}>
      <p id={`${labelId}-help`}>{text.help}</p>
      {phase === 'loading' && <p role="status">{text.loading}</p>}
      {(phase === 'load-error' || phase === 'save-error') && <>
        <p role="alert">{error}</p>
        <Button type="button" variant="secondary" size="sm" onClick={() => void load()}>{phase === 'save-error' ? text.refresh : text.retry}</Button>
      </>}
      {catalog && <form onSubmit={event => { event.preventDefault(); void save() }}>
        <label htmlFor={labelId}>{text.label}</label>
        <div className={styles.selectWrap}><select ref={selectRef} id={labelId} value={selected} onChange={event => setSelected(event.target.value)} disabled={phase === 'saving'} aria-describedby={`${labelId}-help`}>
          {!catalog.models.includes(catalog.active_model) && <option value="" disabled>{catalog.active_model || '—'}</option>}
          {catalog.models.map(model => <option key={model} value={model}>{model}</option>)}
        </select><ChevronDown size={16} aria-hidden="true" /></div>
        <Button type="submit" variant="primary" size="sm" loading={phase === 'saving'} disabled={!selected || selected === catalog.active_model}>{phase === 'saving' ? text.saving : text.save}</Button>
      </form>}
      <Button type="button" variant="ghost" size="sm" onClick={close} disabled={phase === 'saving'}>{text.cancel}</Button>
    </div>}
  </div>
}
