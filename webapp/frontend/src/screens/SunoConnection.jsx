import { useEffect, useState } from 'react'
import { Button, Chip } from '../components.jsx'
import { api } from '../api.js'

export default function SunoConnection({ account, onChange, inDialog = false }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const pending = account?.status === 'pending'

  useEffect(() => {
    if (!pending) return undefined
    let active = true
    let polling = false
    const timer = setInterval(async () => {
      if (polling) return
      polling = true
      try {
        const result = await api.sunoAuthStatus()
        if (active) { onChange(result); setError('') }
      } catch (e) {
        if (active) setError(e.message)
      } finally { polling = false }
    }, 2000)
    return () => { active = false; clearInterval(timer) }
  }, [pending, onChange])

  const refresh = async () => {
    setBusy(true); setError('')
    try { onChange(await api.sunoAuthStatus()) }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }
  const connect = async () => {
    setBusy(true); setError('')
    try {
      const result = await api.sunoConnect()
      onChange(result.status === 'connected' ? await api.sunoStatus() : result)
      if (result.authorize_url) window.open(result.authorize_url, '_blank', 'noopener,noreferrer')
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }
  const disconnect = async () => {
    setBusy(true); setError('')
    try {
      await api.sunoDisconnect()
      onChange({ connected: false })
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="stack gap-10" style={inDialog ? undefined : { borderTop: '1px solid var(--line)', paddingTop: 14 }}>
      <div className="row center gap-8 row--wrap" role="status">
        <strong>Suno account</strong>
        {account?.connected && <Chip tone="ok" dot>{account.plan || 'Connected'}</Chip>}
        {!account && <span className="muted">Checking connection…</span>}
        {account && !account.connected && <Chip tone="warn">
          {pending ? 'Waiting for sign-in' : account.error ? 'Connection unavailable' : 'Sign-in required'}
        </Chip>}
      </div>
      {account?.connected && (
        <div className="muted" style={{ fontSize: 12 }}>
          Generation credits: <strong>{account.credits_remaining ?? 'Unavailable'}</strong>
          {' · '}Download credits: <strong>{account.download_credits_remaining ?? 'Unavailable'}</strong>
          {!!account.allowed_models?.length && <> · Models: {account.allowed_models.join(', ')}</>}
        </div>
      )}
      <div className="row center gap-8 row--wrap">
        {!account?.connected && <Button variant="primary" icon="link" disabled={busy || pending || !account} onClick={connect}>
          {busy ? 'Connecting…' : !account ? 'Checking connection…' : pending ? 'Waiting for Suno…' : 'Sign in to Suno'}
        </Button>}
        <Button variant="ghost" icon="refresh" disabled={busy} onClick={refresh}>Refresh account</Button>
        {(account?.connected || pending) && <Button variant="ghost" icon="link-slash" disabled={busy} onClick={disconnect}>
          {pending ? 'Cancel sign-in' : 'Disconnect'}
        </Button>}
      </div>
      {pending && account.authorize_url && (
        <div className="stack gap-8">
          <a href={account.authorize_url} target="_blank" rel="noopener noreferrer">Open Suno sign-in</a>
          <input className="input" readOnly aria-label="Suno sign-in link" value={account.authorize_url}
            onFocus={(e) => e.target.select()} />
          <div className="field__hint">Complete sign-in in a browser on the controller; this screen updates when connected. You can copy this link into your default browser. A browser on another machine needs the callback port forwarded to the controller.</div>
        </div>
      )}
      {(error || account?.error) && <div role="alert" style={{ color: 'var(--danger)', fontSize: 12 }}>{error || account.error}</div>}
      {!inDialog && <div className="field__hint">
        Suno runs in your account. Choose Generate on Suno in the Song tab or film editor to pick a voice and configure the request. Generate two takes uses generation credits. Importing your chosen take may use one download credit.
      </div>}
    </div>
  )
}
