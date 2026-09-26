import { useEffect, useState } from 'react'
import { Banner, Button, Card, Field, Icon } from '../components.jsx'
import { api } from '../api.js'

export default function InstagramAccounts() {
  const [accounts, setAccounts] = useState([])
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const refresh = () => api.instagramAccounts().then((r) => setAccounts(r.accounts || []))
  useEffect(() => { refresh().catch((e) => setError(e.message)) }, [])

  const connect = async () => {
    setBusy(true); setError(''); setMessage('')
    try {
      const r = await api.instagramConnect(token)
      setToken('')
      await refresh()
      setMessage(`Connected @${r.account.name}.`)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  const disconnect = async (account) => {
    setBusy(true); setError(''); setMessage('')
    try { await api.instagramDisconnect(account); await refresh() }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <Card span={12} className="reveal reveal-d2">
      <span className="label-sm"><Icon name="instagram" brand /> Instagram accounts</span>
      <p className="muted mt-10">Publish finished films as Reels from the Publish screen. Connect a Business or Creator account linked to a Facebook Page.</p>
      <Banner tone="danger">{error}</Banner>
      <Banner tone="ok">{message}</Banner>
      <div className="stack gap-16 mt-16">
        {accounts.map((a) => (
          <div className="row center between gap-16 row--wrap" key={a.id}>
            <span><strong>@{a.name}</strong>{a.page_name && <span className="muted"> · {a.page_name}</span>}</span>
            <Button variant="ghost" disabled={busy} onClick={() => disconnect(a.id)}>Disconnect @{a.name}</Button>
          </div>
        ))}
        <Field label="Facebook Page access token" hint="Paste the Page token from your Meta app. The linked Instagram account and publishing access are verified before saving. Reconnecting replaces an expired token.">
          <input className="input" type="password" autoComplete="off" aria-label="Facebook Page access token" value={token}
            disabled={busy} onChange={(e) => setToken(e.target.value)} />
        </Field>
        <div className="row center gap-16 row--wrap">
          <Button variant="primary" disabled={busy || !token.trim()} onClick={connect}>{busy ? 'Working…' : 'Connect Instagram'}</Button>
          <a href="https://pizzato.github.io/stephen_spielbot/instagram_setup/" target="_blank" rel="noreferrer">Instagram setup guide</a>
        </div>
        <p className="muted" style={{ fontSize: 12 }}>Instagram publishing is manual. Choose the account and review the caption on each film; the YouTube and X schedule is separate.</p>
      </div>
    </Card>
  )
}
