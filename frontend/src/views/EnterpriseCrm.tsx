import { useEffect, useRef, useState } from 'react'
import { Database, RefreshCw } from 'lucide-react'
import { ApiError } from '../api/client'
import { listCrmConnections, readCrm, type CrmInventory } from '../api/crm'
import { Button } from '../components/ui/Button'
import { useT } from '../lib/i18n'
import styles from './EnterpriseCrm.module.css'

export function EnterpriseCrm() {
  const t = useT()
  const [inventory, setInventory] = useState<CrmInventory | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [unpaired, setUnpaired] = useState(false)
  const [selected, setSelected] = useState('')
  const [operation, setOperation] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const generation = useRef(0)
  const pending = useRef<AbortController | null>(null)
  const resultHeading = useRef<HTMLHeadingElement>(null)

  function invalidate() {
    generation.current++
    pending.current?.abort()
    pending.current = null
    setResult(null)
    setBusy(false)
  }

  async function load() {
    invalidate()
    const id = generation.current
    const controller = new AbortController()
    pending.current = controller
    setInventory(null)
    setSelected('')
    setOperation('')
    setError('')
    setUnpaired(false)
    setLoading(true)
    try {
      const value = await listCrmConnections(controller.signal)
      if (id !== generation.current) return
      setInventory(value)
    } catch (failure) {
      if (id !== generation.current) return
      if (failure instanceof ApiError && failure.code === 'crm_not_associated')
        setUnpaired(true)
      else setError(t('crm.error.load'))
    } finally {
      if (id === generation.current) {
        setLoading(false)
        pending.current = null
      }
    }
  }

  useEffect(() => {
    void load()
    const focus = () => {
      void load()
    }
    window.addEventListener('focus', focus)
    return () => {
      generation.current++
      pending.current?.abort()
      window.removeEventListener('focus', focus)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const connection = inventory?.connections.find((item) => item.id === selected)
  async function read() {
    if (!inventory || !connection || !operation || pending.current) return
    const id = ++generation.current
    const controller = new AbortController()
    pending.current = controller
    setBusy(true)
    setResult(null)
    setError('')
    try {
      const value = await readCrm(
        connection,
        operation,
        inventory.context,
        controller.signal,
      )
      if (id !== generation.current) return
      const text = JSON.stringify(value.data, null, 2)
      setResult(
        text.length > 64_000
          ? text.slice(0, 64_000) + '\n… ' + t('crm.truncated')
          : text,
      )
    } catch (failure) {
      if (id !== generation.current) return
      if (
        failure instanceof ApiError &&
        [403, 404, 409].includes(failure.status)
      ) {
        setInventory(null)
        setSelected('')
        setOperation('')
        setError(t('crm.error.changed'))
      } else setError(t('crm.error.read'))
    } finally {
      if (id === generation.current) {
        setBusy(false)
        pending.current = null
      }
    }
  }
  useEffect(() => {
    if (result !== null) resultHeading.current?.focus()
  }, [result])

  return (
    <section className={styles.section} aria-labelledby="enterprise-crm-title">
      <header className={styles.header}>
        <div>
          <h2 id="enterprise-crm-title">
            <Database size={15} aria-hidden />
            {t('crm.title')}
          </h2>
          <p>{t('crm.description')}</p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          disabled={loading}
          onClick={() => void load()}
          aria-label={t('crm.refresh')}
        >
          <RefreshCw size={14} aria-hidden />
        </Button>
      </header>
      {loading && <p role="status">{t('crm.loading')}</p>}
      {unpaired && <p>{t('crm.unpaired')}</p>}
      {error && (
        <div role="alert" className={styles.error}>
          <p>{error}</p>
          <Button size="sm" variant="secondary" onClick={() => void load()}>
            {t('crm.refresh')}
          </Button>
        </div>
      )}
      {inventory && inventory.connections.length === 0 && (
        <p>{t('crm.empty')}</p>
      )}
      {inventory && inventory.connections.length > 0 && (
        <>
          <div className={styles.controls}>
            <label>
              {t('crm.connection')}
              <select
                aria-label={t('crm.connection')}
                value={selected}
                onChange={(event) => {
                  invalidate()
                  setSelected(event.target.value)
                  setOperation('')
                  setError('')
                }}
              >
                <option value="">{t('crm.choose')}</option>
                {inventory.connections.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('crm.operation')}
              <select
                aria-label={t('crm.operation')}
                value={operation}
                disabled={!connection}
                onChange={(event) => {
                  invalidate()
                  setOperation(event.target.value)
                  setError('')
                }}
              >
                <option value="">{t('crm.choose')}</option>
                {connection?.operations.map((op) => (
                  <option key={op.path} value={op.path}>
                    GET {op.path}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className={styles.note}>{t('crm.read_only')}</p>
          <div className={styles.actions}>
            <Button
              size="sm"
              disabled={!operation || busy}
              onClick={() => void read()}
            >
              {busy ? t('crm.reading') : t('crm.read')}
            </Button>
            {busy && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  invalidate()
                  setError(t('crm.cancelled'))
                }}
              >
                {t('crm.cancel')}
              </Button>
            )}
          </div>
        </>
      )}
      {result !== null && (
        <div className={styles.result}>
          <h3 ref={resultHeading} tabIndex={-1}>
            {t('crm.result')}
          </h3>
          <p>{t('crm.untrusted')}</p>
          <pre>{result}</pre>
        </div>
      )}
    </section>
  )
}
