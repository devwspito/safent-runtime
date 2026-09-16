import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, getComposioStatus, getComposioAdsConfig, prepareComposioAds, setComposioApiKey, setupComposioMeta } from '../api/client'
import { getAuthStatus, subscribeAuthStatus, token } from '../lib/token'
import { useLocale } from '../lib/i18n'
import { openAdsSetupLink } from '../lib/adsSetupLinks'
import { Button } from '../components/ui/Button'
import styles from './AdsSetupGuide.module.css'

const CALLBACK = 'https://backend.composio.dev/api/v1/auth-apps/add'
const CATALOG = '/capacidades?tab=integraciones'

const COPY = {
  es: {
    title: 'Configurar', back: 'Volver', cancel: 'Cancelar', catalog: 'Volver a Integraciones', next: 'Continuar', retry: 'Volver a comprobar',
    intro: 'Prepara la conexión una vez. Después autorizarás tu cuenta publicitaria en Anuncios.',
    loading: 'Comprobando la configuración…', session: 'Tu sesión ha caducado. Vuelve a abrir Safent para continuar.', owner: 'Sólo el propietario de Safent puede configurar estas conexiones. Revisa tu sesión.',
    failed: 'No se pudo completar este paso. Revisa los datos y la conexión de Composio e inténtalo de nuevo. Si introdujiste una clave, vuelve a pegarla.',
    checkFailed: 'No se pudo comprobar la configuración. Vuelve a intentarlo; todavía no sabemos si está preparada.', limited: 'Hay demasiados intentos. Espera un momento y vuelve a intentarlo.', saving: 'Guardando…',
    composio: 'Conectar Composio', dashboard: 'Abrir Composio', keyStep: 'Inicia sesión o crea tu cuenta en Composio. Entra en Settings → API Keys y copia la clave de tu proyecto.',
    keyLabel: 'Clave de Composio', keyHelp: 'Pégala aquí. Se envía a Safent para guardarla de forma segura; no se guarda en el almacenamiento del navegador.', keySave: 'Guardar clave y continuar', keyInvalid: 'Pega la clave de Composio para continuar.',
    keyMissing: '¿No encuentro API Keys?', keyMissingHelp: 'Comprueba que has iniciado sesión y seleccionado tu proyecto en el panel de Composio. Abre Settings y busca API Keys. No uses una clave de Google o de Meta.',
    google: 'Preparar Google', googleHelp: 'Esta ruta utiliza la conexión de Google que ofrece Composio. No necesitas crear un cliente OAuth ni pegar un secreto de Google.', googleNext: 'Después, en Anuncios, introduce el número de tu cuenta de Google Ads y autoriza el acceso con Google. Una cuenta de Gmail no concede por sí sola acceso publicitario.',
    metaIntro: 'Conecta Meta a través de Composio. Sólo esta primera vez necesitas el App ID y el App Secret de una app que puedas administrar en Meta.',
    app: 'Elegir tu app de Meta', appOpen: 'Abrir mis apps de Meta', basicOpen: 'Abrir información básica', appHelp: 'Abre o crea tu app de Meta con acceso a la API de Marketing e Inicio de sesión con Facebook para empresas, según las opciones que muestre Meta.',
    appId: 'Identificador de la app (App ID)', appIdHelp: 'Cópialo de Configuración de la app → Información básica. Es el identificador de la app, no el número de una cuenta publicitaria.', appInvalid: 'Introduce el identificador de la app de Meta: de 5 a 30 dígitos.',
    noApp: 'No tengo una app o no encuentro el campo', noAppHelp: 'En Mis apps, elige Crear app y el caso de uso para empresas o anuncios que Meta ofrezca. Si ya existe, pide acceso de administrador a esa app. Entra en Configuración de la app → Información básica; los nombres pueden variar según el tipo de app. No introduzcas aquí la contraseña de Meta.',
    callback: 'Guardar la dirección de retorno', callbackHelp: 'En Meta, abre Inicio de sesión con Facebook para empresas → Configuración → URI de redirección OAuth válidas. Añade exactamente esta dirección y guarda los cambios en Meta.', callbackLabel: 'Dirección de retorno', callbackOpen: 'Abrir configuración de acceso',
    callbackConfirm: 'He añadido esta dirección y guardado los cambios en Meta.', callbackHonest: 'Esta casilla es tu confirmación; Safent no puede verificar esa pantalla de Meta.', callbackInvalid: 'Confirma que has guardado la dirección en Meta antes de continuar.', callbackMissing: 'No veo las URI de redirección', callbackMissingHelp: 'Comprueba que tu app incluye Inicio de sesión con Facebook para empresas. En el panel de Meta, busca ese producto o caso de uso y abre su Configuración. Si no aparece, revisa los productos de la app o pide ayuda a su administrador.',
    secret: 'Guardar la clave de la app', secretHelp: 'Vuelve a Configuración de la app → Información básica → Clave de la app → Mostrar. Si Meta pide tu contraseña, introdúcela sólo en Meta.', secretLabel: 'Clave de la app (App Secret)', privacy: 'La clave de la app se envía a Composio y se guarda allí, no en el navegador. No pegues tu contraseña de Meta.', secretInvalid: 'Pega la clave de la app (App Secret), de hasta 4096 caracteres.', prepareMeta: 'Guardar y preparar Meta',
    ready: 'Configuración preparada', readyHelp: 'La configuración está guardada. Esto no significa que una cuenta publicitaria esté conectada: todavía debes autorizar cada cuenta en Anuncios.', ads: 'Ir a Anuncios y autorizar',
    copy: 'Copiar dirección', copied: 'Dirección copiada.', copyFailed: 'No se pudo copiar. Selecciona la dirección y cópiala manualmente.', openFailed: 'No se pudo abrir el navegador. Copia esta dirección y ábrela en tu navegador.', address: 'Dirección para abrir en el navegador',
  },
  en: {
    title: 'Set up', back: 'Back', cancel: 'Cancel', catalog: 'Back to Integrations', next: 'Continue', retry: 'Check again',
    intro: 'Prepare the connection once. Then authorize your advertising account in Ads.',
    loading: 'Checking configuration…', session: 'Your session expired. Reopen Safent to continue.', owner: 'Only the Safent owner can set up these connections. Check your session.',
    failed: 'Could not complete this step. Check the details and the Composio connection, then try again. If you entered a key, paste it again.', checkFailed: 'Could not check the configuration. Try again; its readiness is still unknown.', limited: 'Too many attempts. Wait a moment and try again.', saving: 'Saving…',
    composio: 'Connect Composio', dashboard: 'Open Composio', keyStep: 'Sign in or create your Composio account. Open Settings → API Keys and copy your project’s key.',
    keyLabel: 'Composio API key', keyHelp: 'Paste it here. It is sent to Safent for secure storage, not saved in browser storage.', keySave: 'Save key and continue', keyInvalid: 'Paste your Composio API key to continue.',
    keyMissing: 'Can’t find API Keys?', keyMissingHelp: 'Make sure you are signed in and have selected your project in the Composio dashboard. Open Settings and look for API Keys. Do not use a Google or Meta key.',
    google: 'Prepare Google', googleHelp: 'This path uses the Google connection provided by Composio. You do not need to create an OAuth client or paste a Google secret.', googleNext: 'Next, enter your Google Ads account number in Ads and authorize access with Google. A Gmail account alone does not grant advertising access.',
    metaIntro: 'Connect Meta through Composio. Just this first time, you need the App ID and App Secret of an app you can administer in Meta.',
    app: 'Choose your Meta app', appOpen: 'Open my Meta apps', basicOpen: 'Open basic settings', appHelp: 'Open or create your Meta app with access to the Marketing API and Facebook Login for Business, following the options Meta shows.',
    appId: 'App ID', appIdHelp: 'Copy it from App settings → Basic. This is the app identifier, not an advertising account number.', appInvalid: 'Enter your Meta App ID: 5 to 30 digits.',
    noApp: 'I don’t have an app or can’t find the field', noAppHelp: 'In My apps, choose Create app and the business or advertising use case Meta offers. For an existing app, request administrator access. Open App settings → Basic; names may vary by app type. Do not enter your Meta password here.',
    callback: 'Save the callback URL', callbackHelp: 'In Meta, open Facebook Login for Business → Settings → Valid OAuth Redirect URIs. Add this exact URL and save the changes in Meta.', callbackLabel: 'Callback URL', callbackOpen: 'Open login settings',
    callbackConfirm: 'I added this URL and saved the changes in Meta.', callbackHonest: 'This checkbox is your confirmation; Safent cannot verify that Meta screen.', callbackInvalid: 'Confirm that you saved the URL in Meta before continuing.', callbackMissing: 'I can’t see the redirect URI field', callbackMissingHelp: 'Check that your app includes Facebook Login for Business. Find that product or use case in Meta and open its Settings. If it is missing, review the app products or ask its administrator for help.',
    secret: 'Save the app secret', secretHelp: 'Return to App settings → Basic → App Secret → Show. If Meta asks for your password, enter it only in Meta.', secretLabel: 'App Secret', privacy: 'The App Secret is sent to and stored in Composio, not in the browser. Do not paste your Meta password.', secretInvalid: 'Paste the App Secret, up to 4096 characters.', prepareMeta: 'Save and prepare Meta',
    ready: 'Configuration prepared', readyHelp: 'The configuration is saved. This does not mean an advertising account is connected: you still need to authorize each account in Ads.', ads: 'Go to Ads and authorize',
    copy: 'Copy URL', copied: 'URL copied.', copyFailed: 'Could not copy. Select the URL and copy it manually.', openFailed: 'Could not open the browser. Copy this URL and open it in your browser.', address: 'URL to open in your browser',
  },
} as const

type Provider = 'google' | 'meta'
type ErrorKey = 'session' | 'owner' | 'failed' | 'checkFailed' | 'limited' | 'keyInvalid' | 'appInvalid' | 'callbackInvalid' | 'secretInvalid'
type Stage = 'loading' | 'error' | 'composio' | 'google' | 'app' | 'ready'

export default function AdsSetupGuide({ provider }: { provider: Provider }) {
  const auth = useSyncExternalStore(subscribeAuthStatus, getAuthStatus)
  const { locale } = useLocale()
  return auth.kind === 'authenticated'
    ? <Guide key={provider} provider={provider} />
    : <div className={styles.body}><p role="alert">{COPY[locale].session}</p><Link to={CATALOG}>{COPY[locale].catalog}</Link></div>
}

function Guide({ provider }: { provider: Provider }) {
  const { locale } = useLocale()
  const copy = COPY[locale]
  const id = useId()
  const [stage, setStage] = useState<Stage>('loading')
  const [error, setError] = useState<ErrorKey | null>(null)
  const [busy, setBusy] = useState(false)
  const [appId, setAppId] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const keyRef = useRef<HTMLInputElement | null>(null)
  const secretRef = useRef<HTMLInputElement | null>(null)
  const bindKey = useCallback((node: HTMLInputElement | null) => { if (!node && keyRef.current) keyRef.current.value = ''; keyRef.current = node }, [])
  const bindSecret = useCallback((node: HTMLInputElement | null) => { if (!node && secretRef.current) secretRef.current.value = ''; secretRef.current = node }, [])
  const appRef = useRef<HTMLInputElement>(null)
  const titleRef = useRef<HTMLHeadingElement>(null)
  const alertRef = useRef<HTMLParagraphElement>(null)
  const generation = useRef(0)
  const inFlight = useRef(false)
  const scope = useRef(token())
  const active = (revision: number) => revision === generation.current && getAuthStatus().kind === 'authenticated' && token() === scope.current

  function finish(revision: number) {
    if (revision !== generation.current) return
    inFlight.current = false; setBusy(false)
    if (!active(revision)) {
      if (keyRef.current) keyRef.current.value = ''
      if (secretRef.current) secretRef.current.value = ''
      setError('session'); setStage('error')
    }
  }

  function fail(failure: unknown, fallback: ErrorKey) {
    const status = failure instanceof ApiError ? failure.status : 0
    setError(status === 401 ? 'session' : status === 403 ? 'owner' : status === 429 ? 'limited' : fallback)
    if (status === 401 || status === 403) setStage('error')
  }

  async function load() {
    if (inFlight.current || !active(generation.current)) return
    const revision = ++generation.current
    inFlight.current = true
    setStage('loading'); setError(null)
    try {
      const status = await getComposioStatus()
      if (!active(revision)) return
      if (!status || typeof status.has_key !== 'boolean') throw new Error('Invalid status')
      // The owner-only read gates even the initial key form. A service failure
      // is never treated as missing configuration or permission to overwrite it.
      const configuration = await getComposioAdsConfig(provider === 'google' ? 'googleads' : 'metaads')
      if (!active(revision)) return
      if (!configuration || typeof configuration.ready !== 'boolean') throw new Error('Invalid configuration')
      setStage(!status.has_key ? 'composio' : configuration.ready ? 'ready' : provider === 'google' ? 'google' : 'app')
    } catch (failure) {
      if (active(revision)) { setStage('error'); fail(failure, 'checkFailed') }
    } finally {
      finish(revision)
    }
  }

  useEffect(() => {
    void load()
    return () => {
      generation.current++
      inFlight.current = false
      if (keyRef.current) keyRef.current.value = ''
      if (secretRef.current) secretRef.current.value = ''
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { if (error) alertRef.current?.focus(); else titleRef.current?.focus() }, [stage, error])

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (inFlight.current || !active(generation.current)) return
    const clientId = appRef.current?.value.trim() ?? ''
    const key = keyRef.current?.value.trim() ?? ''
    const secret = secretRef.current?.value.trim() ?? ''
    if (stage === 'composio' && !key) { setError('keyInvalid'); return }
    if (stage === 'app') {
      if (!/^[0-9]{5,30}$/.test(clientId)) { setError('appInvalid'); return }
      if (!secret || secret.length > 4096) { setError('secretInvalid'); return }
      if (!confirmed) { setError('callbackInvalid'); return }
    }
    if (!['composio', 'app', 'google'].includes(stage)) return
    const revision = generation.current
    inFlight.current = true; setBusy(true); setError(null)
    // Secret values exist only in the input and this request's local scope.
    // Clear before dispatch, including failures and navigations during POST.
    if (keyRef.current) keyRef.current.value = ''
    if (secretRef.current) secretRef.current.value = ''
    try {
      if (stage === 'composio') {
        await setComposioApiKey(key)
        if (active(revision)) { inFlight.current = false; setBusy(false); await load() }
      } else if (stage === 'google') {
        const result = await prepareComposioAds()
        if (active(revision)) {
          if (result.googleads !== true) throw new Error('Google not prepared')
          setStage('ready')
        }
      } else {
        const result = await setupComposioMeta({ client_id: clientId, client_secret: secret })
        if (active(revision)) {
          if (result.ready !== true) throw new Error('Meta not prepared')
          setStage('ready')
        }
      }
    } catch (failure) {
      if (active(revision)) fail(failure, 'failed')
    } finally {
      finish(revision)
    }
  }

  const steps = provider === 'meta' ? ['composio', 'app', 'ready'] as const : ['composio', 'google', 'ready'] as const
  const current = steps.findIndex(value => value === stage)
  const title = stage === 'loading' ? copy.loading : stage === 'error' ? copy.title : copy[stage]
  return <div className={styles.body}>
    <header className={styles.header}><Link to={CATALOG}>{copy.catalog}</Link><h1>{copy.title} {provider === 'google' ? 'Google Ads' : 'Meta Ads'}</h1><p>{copy.intro}</p></header>
    {provider === 'meta' && <p className={styles.intro}>{copy.metaIntro}</p>}
    <ol className={styles.steps} aria-label={locale === 'es' ? 'Pasos de configuración' : 'Setup steps'}>
      {steps.map((step, index) => <li key={step} aria-current={stage === step ? 'step' : undefined} data-complete={current > index || undefined}><span>{index + 1}.</span> {copy[step]}</li>)}
    </ol>
    <section className={styles.card} aria-labelledby={`${id}-title`} aria-busy={stage === 'loading' || busy}>
      <h2 id={`${id}-title`} ref={titleRef} tabIndex={-1}>{title}</h2>
      {stage === 'loading' && <p role="status">{copy.loading}</p>}
      {error && <p ref={alertRef} tabIndex={-1} role="alert" className={styles.error}>{copy[error]}</p>}
      {(stage === 'error' || error === 'failed' || error === 'limited') && <Button disabled={busy} onClick={() => void load()}>{copy.retry}</Button>}
      {stage === 'ready' && <><p role="status">{copy.readyHelp}</p><Link className="cv-btn cv-btn--primary" to={`/anuncios?connect=${provider}`}>{copy.ads}</Link></>}
      {!['loading', 'error', 'ready'].includes(stage) && <form onSubmit={event => void save(event)} noValidate>
        {stage === 'composio' && <>
          <p>{copy.keyStep}</p><ExternalLink url="https://dashboard.composio.dev/" label={copy.dashboard} />
          <label htmlFor={`${id}-key`}>{copy.keyLabel}</label>
          <input id={`${id}-key`} ref={bindKey} type="password" autoComplete="new-password" spellCheck={false} required disabled={busy} aria-describedby={`${id}-privacy`} aria-invalid={error === 'keyInvalid' || undefined} />
          <p id={`${id}-privacy`}>{copy.keyHelp}</p><details><summary>{copy.keyMissing}</summary><p>{copy.keyMissingHelp}</p></details>
        </>}
        {stage === 'google' && <><p>{copy.googleHelp}</p><p>{copy.googleNext}</p></>}
        {stage === 'app' && <>
          <h3>1. {copy.app}</h3><p>{copy.appHelp}</p><ExternalLink url="https://developers.facebook.com/apps/" label={copy.appOpen} />
          <label htmlFor={`${id}-app`}>{copy.appId}</label>
          <input id={`${id}-app`} ref={appRef} defaultValue={appId} onChange={event => {
            const value = event.target.value.trim()
            if (value !== appId) setConfirmed(false)
            setAppId(value)
          }} type="text" inputMode="numeric" autoComplete="off" spellCheck={false} maxLength={30} required disabled={busy} aria-describedby={`${id}-app-help`} aria-invalid={error === 'appInvalid' || undefined} />
          <p id={`${id}-app-help`}>{copy.appIdHelp}</p>
          <label htmlFor={`${id}-secret`}>{copy.secretLabel}</label>
          <input id={`${id}-secret`} ref={bindSecret} type="password" autoComplete="new-password" spellCheck={false} maxLength={4096} required disabled={busy} aria-describedby={`${id}-privacy`} aria-invalid={error === 'secretInvalid' || undefined} />
          <p>{copy.secretHelp}</p><p id={`${id}-privacy`}>{copy.privacy}</p>
          {/^[0-9]{5,30}$/.test(appId) && <ExternalLink url={`https://developers.facebook.com/apps/${appId}/settings/basic/`} label={copy.basicOpen} />}
          <details><summary>{copy.noApp}</summary><p>{copy.noAppHelp}</p></details>
          <h3>2. {copy.callback}</h3><p>{copy.callbackHelp}</p>
          {/^[0-9]{5,30}$/.test(appId) && <ExternalLink url={`https://developers.facebook.com/apps/${appId}/fb-login/settings/`} label={copy.callbackOpen} />}
          <CopyAddress url={CALLBACK} label={copy.callbackLabel} />
          <label className={styles.checkbox}><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />{copy.callbackConfirm}</label>
          <p>{copy.callbackHonest}</p><details><summary>{copy.callbackMissing}</summary><p>{copy.callbackMissingHelp}</p></details>
          <h3>3. {copy.prepareMeta}</h3>
        </>}
        <div className={styles.actions}>
          <Button type="submit" variant="primary" disabled={busy} loading={busy}>{busy ? copy.saving : stage === 'composio' ? copy.keySave : stage === 'google' ? copy.google : copy.prepareMeta}</Button>
        </div>
      </form>}
    </section>
    <Link className={styles.cancel} to={CATALOG}>{copy.cancel}</Link>
  </div>
}

function ExternalLink({ url, label }: { url: string; label: string }) {
  const { locale } = useLocale()
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  return <div className={styles.external}>
    <Button type="button" size="sm" disabled={busy} onClick={async () => {
      if (inFlight.current) return
      inFlight.current = true; setBusy(true); setFailed(false)
      try { await openAdsSetupLink(url) } catch { setFailed(true) }
      finally { inFlight.current = false; setBusy(false) }
    }}>{label}</Button>
    {failed && <p role="alert" className={styles.error}>{COPY[locale].openFailed}</p>}
    <CopyAddress key={url} url={url} label={COPY[locale].address} />
  </div>
}

function CopyAddress({ url, label }: { url: string; label: string }) {
  const { locale } = useLocale()
  const copy = COPY[locale]
  const id = useId()
  const input = useRef<HTMLInputElement>(null)
  const [feedback, setFeedback] = useState<'copied' | 'copyFailed' | null>(null)
  return <div className={styles.address}><label htmlFor={id}>{label}</label><div className={styles.actions}>
    <input id={id} ref={input} value={url} readOnly spellCheck={false} />
    <Button type="button" size="sm" onClick={async () => {
      try { await navigator.clipboard.writeText(url); setFeedback('copied') }
      catch { setFeedback('copyFailed'); input.current?.focus(); input.current?.select() }
    }}>{copy.copy}</Button>
  </div>{feedback && <p role="status">{copy[feedback]}</p>}</div>
}
