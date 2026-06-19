import { useState, useEffect, useRef } from 'react'
import { Card, Field, Segmented, ResolutionPicker, Check, Button, Banner, Chip, Icon } from '../components.jsx'
import { api, fileUrl } from '../api.js'

const toLines = (v) => Array.isArray(v) ? v.join('\n') : (v || '')
const fromLines = (s) => (s || '').split('\n').map((x) => x.trim()).filter(Boolean)

// "Fully automated mode" is exactly the sum of these per-step toggles: it shows
// ticked only when all of them are, and ticking it turns every one on. Keeping it
// derived means it can never claim to automate a step whose own toggle is off.
const AUTO_FLAGS = [
  'youtube_auto_fetch_evaluate',
  'youtube_auto_approve_comments',
  'youtube_auto_start_job',
  'youtube_auto_approve_script',
  'youtube_auto_ai_ideas',
  'youtube_auto_post',
]

// Live hint for the "Videos per day" cadence input: shows the even spacing it
// implies (2/day → "≈ one every 12h"). 0 = no throttle.
function cadenceHint(perDay) {
  const n = Number(perDay) || 0
  if (n <= 0) return 'No throttle — released as soon as the scheduler runs.'
  const m = Math.round(1440 / n)
  const span = m % 1440 === 0 ? `${m / 1440}d` : m >= 60 ? `${(m / 60).toFixed(m % 60 ? 1 : 0)}h` : `${m}m`
  return `≈ one every ${span}.`
}

// Extract a short display name from a worker URL or hostname
function shortHost(url) {
  try { return new URL(url).hostname } catch { return url }
}

// Compact inline status row shown under each worker textarea. With `load`, each
// server shows its render load (issue #98): green=idle (free for the UI; during a
// render this is the reserved worker), amber=busy rendering, red=down. Without it
// (TTS, which has no queue), each worker shows plain reachability: green=up,
// red=down.
function WorkerStatus({ items, load = false, extra }) {
  if (!items) return <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>Checking…</div>
  if (!items.length) return null
  if (load) {
    return (
      <div className="row gap-6 row--wrap" style={{ marginTop: 6 }}>
        {items.map((w) => {
          const [tone, label] = !w.up ? ['danger', 'down'] : w.busy ? ['warn', 'busy'] : ['ok', 'idle']
          return <Chip key={w.endpoint} tone={tone} dot>{shortHost(w.endpoint)} {label}</Chip>
        })}
        {extra}
      </div>
    )
  }
  return (
    <div className="row gap-6 row--wrap" style={{ marginTop: 6 }}>
      {items.map((w) => (
        <Chip key={w.endpoint} tone={w.up ? 'ok' : 'danger'} dot>{shortHost(w.endpoint)} {w.up ? 'up' : 'down'}</Chip>
      ))}
      {extra}
    </div>
  )
}

// Live state of the dynamic UI-worker reservation (issue #98): whether the UI is
// in use, whether a worker is free for it, or an ETA until one frees.
function UiWorkerStatus({ ui }) {
  if (!ui) return <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>Checking…</div>
  let chip
  if (!ui.active) chip = <Chip tone="neutral" dot>UI idle — no worker reserved</Chip>
  else if (ui.available) chip = <Chip tone="ok" dot>worker ready for UI</Chip>
  else if (ui.eta_text) chip = <Chip tone="warn" dot>worker free in {ui.eta_text}</Chip>
  else chip = <Chip tone="warn" dot>waiting for a worker</Chip>
  return <div className="row gap-6 row--wrap" style={{ marginTop: 6 }}>{chip}</div>
}

// Read a File into a data-URL string (base64) so it can ride in a JSON body.
function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => reject(new Error('Could not read that file.'))
    r.readAsDataURL(file)
  })
}

// Compact play/pause toggle for a voice's reference clip.
function PlayButton({ src }) {
  const ref = useRef(null)
  const [playing, setPlaying] = useState(false)
  return (
    <>
      <button type="button" className="btn btn--quiet" title={playing ? 'Pause' : 'Play'}
        onClick={() => { const a = ref.current; if (a) (a.paused ? a.play() : a.pause()) }}>
        <Icon name={playing ? 'pause' : 'play'} />
      </button>
      <audio ref={ref} src={src} preload="none"
        onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} />
    </>
  )
}

// Audition the style's narrator voice at the current robotic level and speed.
// F5-TTS runs on the backend (a few seconds for one sentence), so show a
// "Generating…" state while waiting. Reads the live, unsaved narrator-voice,
// robotic-level and voice-speed fields so you can dial them in by ear before
// saving. Deliberately no voice picker of its own: a second dropdown here
// looked like the style's voice setting but silently saved nothing.
function VoiceTester({ voice, roboticAmount, speed, onError }) {
  const [busy, setBusy] = useState(false)
  const audioRef = useRef(null)

  const play = async () => {
    onError(''); setBusy(true)
    try {
      const amount = roboticAmount ?? 0.35
      const r = await api.testVoice({ voice: voice || '', robotic: amount > 0, robotic_amount: amount, speed: speed ?? 1 })
      const a = audioRef.current
      // Don't await play(): after a long first generation Chrome may block
      // autoplay (the click's activation window expired), and a blocked play()
      // can hang forever, wedging the button in "Generating…". The sample is
      // cached now — pressing Play again is instant.
      if (a) { a.src = r.url; a.load(); a.play().catch((e) => { if (e.name !== 'NotAllowedError') onError(e.message) }) }
    } catch (e) { onError(e.message) } finally { setBusy(false) }
  }

  const spoken = voice || 'the default narrator'
  return (
    <Field label="Test voice"
      hint={`Plays the narrator voice chosen above — “This is the voice of ${spoken}. What do you think?” at the robotic level and voice speed (cached after the first time).`}>
      <div className="row center gap-10 row--wrap">
        <Button variant="primary" icon="play" disabled={busy} onClick={play}>{busy ? 'Generating…' : 'Play'}</Button>
        <audio ref={audioRef} hidden />
      </div>
    </Field>
  )
}

// Add / rename / replace / delete the reference clips F5-TTS clones. Each
// operation persists immediately (it writes a file), independent of the
// page-level "Save settings" button.
function VoicesManager({ voices, busy, onAdd, onUpdate, onDelete }) {
  const [name, setName] = useState('')
  const [file, setFile] = useState(null)
  const [editing, setEditing] = useState(null)   // name of the voice being edited
  const [editName, setEditName] = useState('')
  const [editFile, setEditFile] = useState(null)
  const addRef = useRef(null)

  const add = async () => {
    try { await onAdd(name.trim(), file) } catch { return }
    setName(''); setFile(null); if (addRef.current) addRef.current.value = ''
  }
  const startEdit = (v) => { setEditing(v.name); setEditName(v.name); setEditFile(null) }
  const cancelEdit = () => { setEditing(null); setEditName(''); setEditFile(null) }
  const saveEdit = async (v) => {
    try { await onUpdate(v.name, editName.trim(), editFile) } catch { return }
    cancelEdit()
  }

  const rowStyle = { padding: '10px 12px', background: 'var(--paper-2)', borderRadius: 'var(--r-md)' }

  return (
    <Card span={12} className="reveal reveal-d2">
      <div className="row center between">
        <span className="label-sm">Voices</span>
        <span className="muted" style={{ fontSize: 11.5 }}>changes save immediately</span>
      </div>
      <div className="field__hint" style={{ marginTop: 6 }}>
        Reference clips (15–30s of clear speech) F5-TTS clones for narration. Pick each style's narrator voice under <strong>Styles</strong>.
      </div>

      <div className="stack gap-10 mt-16">
        {(voices || []).length === 0 && (
          <div className="muted" style={{ fontSize: 13 }}>No voices yet — add one below.</div>
        )}
        {(voices || []).map((v) => (
          <div key={v.name} className="row center gap-10 row--wrap" style={rowStyle}>
            {editing === v.name ? (
              <>
                <input className="input" value={editName} onChange={(e) => setEditName(e.target.value)} style={{ maxWidth: 220 }} />
                <label className="btn btn--ghost">
                  <Icon name="upload" /> {editFile ? editFile.name : 'Replace clip'}
                  <input type="file" accept="audio/*" hidden onChange={(e) => setEditFile(e.target.files?.[0] || null)} />
                </label>
                <div className="grow" />
                <Button variant="primary" icon="check" disabled={busy || !editName.trim()} onClick={() => saveEdit(v)}>Save</Button>
                <Button variant="ghost" onClick={cancelEdit} disabled={busy}>Cancel</Button>
              </>
            ) : (
              <>
                <PlayButton src={fileUrl(v.path)} />
                <span style={{ fontWeight: 600 }}>{v.name}</span>
                <div className="grow" />
                <Button variant="ghost" icon="pen" disabled={busy} onClick={() => startEdit(v)}>Edit</Button>
                <Button variant="danger" icon="trash" disabled={busy} onClick={() => onDelete(v.name)}>Delete</Button>
              </>
            )}
          </div>
        ))}
      </div>

      <div className="row center gap-10 row--wrap mt-16" style={{ borderTop: '1px solid var(--line)', paddingTop: 16 }}>
        <input className="input" placeholder="New voice name" value={name} onChange={(e) => setName(e.target.value)} style={{ maxWidth: 220 }} />
        <label className="btn btn--ghost">
          <Icon name="upload" /> {file ? file.name : 'Choose audio…'}
          <input ref={addRef} type="file" accept="audio/*" hidden onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </label>
        <Button variant="primary" icon="plus" disabled={busy || !name.trim() || !file} onClick={add}>Add voice</Button>
      </div>
    </Card>
  )
}

const TABS = [
  { id: 'infra', label: 'Infrastructure' },
  { id: 'styles', label: 'Styles' },
  { id: 'youtube', label: 'YouTube' },
  { id: 'x', label: 'X' },
  { id: 'automation', label: 'Automation' },
]

// Connected YouTube channels (issue #22). Connecting runs the backend OAuth
// flow (a Google window opens on the server machine); each channel's token is
// stored separately, and styles pick which channel they publish to.
// Languages offered for a channel's uploads (BCP-47 code → label).
const LANGUAGES = {
  en: 'English', es: 'Spanish', pt: 'Portuguese', fr: 'French', de: 'German',
  it: 'Italian', nl: 'Dutch', ja: 'Japanese', ko: 'Korean', zh: 'Chinese',
  hi: 'Hindi', ar: 'Arabic', ru: 'Russian',
}

function ChannelsCard({ onConfigChanged, onError }) {
  const [channels, setChannels] = useState(null)
  const [connecting, setConnecting] = useState(false)
  const [busy, setBusy] = useState('')
  const [expanded, setExpanded] = useState('')   // channel id whose settings editor is open
  const [eng, setEng] = useState({})             // local per-channel settings edits, keyed by channel id
  const [savingEng, setSavingEng] = useState('')
  const [categories, setCategories] = useState({})   // YouTube category name → id, for the picker
  const [defaultCat, setDefaultCat] = useState('22') // global fallback category id
  const pollRef = useRef(null)

  const refresh = () => api.ytChannels()
    .then((r) => { setChannels(r.channels || []); if (r.auth_running) startPolling() })
    .catch((e) => onError(e.message))
  useEffect(() => {
    refresh()
    api.ytPostOptions().then((o) => { setCategories(o.categories || {}); setDefaultCat(o.default_category || '22') }).catch(() => {})
    return () => clearInterval(pollRef.current)
  }, [])

  const startPolling = () => {
    setConnecting(true)
    clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const r = await api.ytAuthPoll()
        if (r.running) return
        clearInterval(pollRef.current)
        setConnecting(false)
        if (r.result && !r.result.success) onError(r.result.error || 'Authorization failed.')
        await refresh()
        onConfigChanged()   // the channel list lives in the config
      } catch { /* keep polling */ }
    }, 2000)
  }

  const connect = async () => {
    onError('')
    try {
      const r = await api.ytAuthStart()
      if (!r.ok) { onError(r.message || 'Could not start the Google authorization.'); return }
      startPolling()
    } catch (e) { onError(e.message) }
  }

  const disconnect = async (ch) => {
    const label = ch.name || ch.id
    if (!window.confirm(`Disconnect “${label}”? Styles publishing to it fall back to the first remaining channel.`)) return
    setBusy(ch.id); onError('')
    try {
      await api.ytDisconnect(ch.id)
      await refresh()
      onConfigChanged()
    } catch (e) { onError(e.message) } finally { setBusy('') }
  }

  // Per-channel settings (default category + engagement, issue #84) save
  // immediately, like connect/disconnect.
  const toggleEng = (ch) => {
    if (expanded === ch.id) { setExpanded(''); return }
    setEng((e) => ({ ...e, [ch.id]: { engagement_prompt: ch.engagement_prompt || '', auto_respond: !!ch.auto_respond, video_category: ch.video_category || '', language: ch.language || 'en', upload_captions: ch.upload_captions !== false, publish_per_day: ch.publish_per_day || 0 } }))
    setExpanded(ch.id)
  }
  const setEngField = (id, k, v) => setEng((e) => ({ ...e, [id]: { ...e[id], [k]: v } }))
  const saveEng = async (ch) => {
    setSavingEng(ch.id); onError('')
    try {
      await api.ytChannelSettings(ch.id, eng[ch.id] || {})
      await refresh()
      setExpanded('')
    } catch (e) { onError(e.message) } finally { setSavingEng('') }
  }

  const rowStyle = { padding: '10px 12px', background: 'var(--paper-2)', borderRadius: 'var(--r-md)' }
  return (
    <Card span={12} className="reveal reveal-d1">
      <div className="row center between">
        <span className="label-sm">Channels</span>
        <Button variant="primary" icon="youtube" disabled={connecting} onClick={connect}>
          {connecting ? 'Waiting for Google…' : 'Connect channel'}
        </Button>
      </div>
      <div className="field__hint" style={{ marginTop: 6 }}>
        Each connected Google login is one channel. Pick the channel a style publishes to under <strong>Styles</strong>; comments and uploads use the right channel automatically.
      </div>
      <div className="stack gap-10 mt-16">
        {channels === null && <div className="muted" style={{ fontSize: 13 }}>Checking…</div>}
        {channels !== null && channels.length === 0 && (
          <div className="muted" style={{ fontSize: 13 }}>No channels connected yet — click <strong>Connect channel</strong> and finish the Google login in the browser window.</div>
        )}
        {(channels || []).map((ch) => (
          <div key={ch.id} className="stack gap-10" style={rowStyle}>
            <div className="row center gap-10 row--wrap">
              <Icon name="youtube" style={{ color: 'var(--accent)' }} />
              <span style={{ fontWeight: 600 }}>{ch.name || ch.id}</span>
              {ch.connected
                ? <Chip tone="ok" dot>connected</Chip>
                : <Chip tone="danger" dot title={ch.error}>not connected</Chip>}
              {!ch.connected && ch.error && <span className="muted" style={{ fontSize: 11.5 }}>{ch.error}</span>}
              {ch.engagement_prompt ? <Chip tone="accent">engagement{ch.auto_respond ? ' · auto' : ''}</Chip> : null}
              <div className="grow" />
              <Button variant="ghost" icon="gear" onClick={() => toggleEng(ch)}>Settings</Button>
              <Button variant="danger" icon="link-slash" disabled={busy === ch.id} onClick={() => disconnect(ch)}>
                {busy === ch.id ? 'Removing…' : 'Disconnect'}
              </Button>
            </div>
            {expanded === ch.id && (
              <div className="stack gap-16" style={{ borderTop: '1px solid var(--line)', paddingTop: 14 }}>
                <Field label="Video category"
                  hint="Default YouTube category for this channel's uploads. Prefilled on the Publish screen and used when this channel auto-publishes.">
                  <select className="select" value={eng[ch.id]?.video_category || defaultCat}
                    onChange={(e) => setEngField(ch.id, 'video_category', e.target.value)}>
                    {Object.entries(categories).map(([name, id]) => <option key={id} value={id}>{name}</option>)}
                  </select>
                </Field>
                <Field label="Video language"
                  hint="Declared as this channel's spoken and metadata language on every upload (used for subtitles and translations).">
                  <select className="select" value={eng[ch.id]?.language || 'en'}
                    onChange={(e) => setEngField(ch.id, 'language', e.target.value)}>
                    {Object.entries(LANGUAGES).map(([code, name]) => <option key={code} value={code}>{name}</option>)}
                  </select>
                </Field>
                <Check checked={eng[ch.id]?.upload_captions !== false} onChange={(v) => setEngField(ch.id, 'upload_captions', v)}
                  label="Attach subtitles from the script — uploads an accurate caption track instead of relying on YouTube's auto-captions" />
                <Field label="Publishing cadence"
                  hint="When scheduled publishing is on, this channel spaces its uploads evenly across the day. 0 = no throttle.">
                  <div className="row gap-16 row--wrap" style={{ alignItems: 'flex-end' }}>
                    <label className="stack gap-8" style={{ fontSize: 12.5 }}><span className="muted">Videos per day</span>
                      <input className="input" type="number" min={0} step="0.5" style={{ width: 120 }} value={eng[ch.id]?.publish_per_day ?? 0}
                        onChange={(e) => setEngField(ch.id, 'publish_per_day', Number(e.target.value) || 0)} /></label>
                    <span className="muted" style={{ fontSize: 12, paddingBottom: 8 }}>{cadenceHint(eng[ch.id]?.publish_per_day)}</span>
                  </div>
                </Field>
                <Field label="Community engagement prompt"
                  hint="How this channel replies to non-request comments — its persona and what to do. Leave empty to disable engagement for this channel.">
                  <textarea className="textarea" rows={5} value={eng[ch.id]?.engagement_prompt || ''}
                    onChange={(e) => setEngField(ch.id, 'engagement_prompt', e.target.value)} />
                </Field>
                <Check checked={!!eng[ch.id]?.auto_respond} onChange={(v) => setEngField(ch.id, 'auto_respond', v)}
                  label="Automatically respond to community comments — post replies immediately instead of waiting for approval" />
                <div className="row center gap-10 row--wrap">
                  <Button variant="primary" icon="floppy-disk" disabled={savingEng === ch.id} onClick={() => saveEng(ch)}>
                    {savingEng === ch.id ? 'Saving…' : 'Save'}
                  </Button>
                  <Button variant="ghost" onClick={() => setExpanded('')}>Cancel</Button>
                  <span className="muted" style={{ fontSize: 11.5 }}>Saves immediately — separate from the main Save settings button.</span>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </Card>
  )
}

// Connected X (Twitter) accounts (issue #107) — the X mirror of ChannelsCard.
// Connecting runs the backend OAuth2 PKCE flow (a browser window opens on the
// server machine); each account's token is stored separately, and styles pick
// which account they publish to. Settings are simpler than YouTube's (no
// category/captions): community-engagement persona + auto-respond + language.
function XAccountsCard({ onConfigChanged, onError }) {
  const [accounts, setAccounts] = useState(null)
  const [connecting, setConnecting] = useState(false)
  const [busy, setBusy] = useState('')
  const [expanded, setExpanded] = useState('')
  const [eng, setEng] = useState({})
  const [savingEng, setSavingEng] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [tok, setTok] = useState({ access: '', refresh: '' })
  const [importBusy, setImportBusy] = useState(false)
  const [importMode, setImportMode] = useState('keys')   // 'keys' (OAuth1, no browser) | 'oauth2'
  const [keys, setKeys] = useState({ api_key: '', api_secret: '', access_token: '', access_secret: '' })
  const [authUrl, setAuthUrl] = useState('')   // surfaced so it can be opened in Incognito
  const pollRef = useRef(null)

  const refresh = () => api.xAccounts()
    .then((r) => { setAccounts(r.accounts || []); if (r.auth_running) startPolling() })
    .catch((e) => onError(e.message))
  useEffect(() => { refresh(); return () => clearInterval(pollRef.current) }, [])

  const startPolling = () => {
    setConnecting(true)
    clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const r = await api.xAuthPoll()
        if (r.running) return
        clearInterval(pollRef.current)
        setConnecting(false); setAuthUrl('')
        if (r.result && !r.result.success) onError(r.result.error || 'Authorization failed.')
        await refresh()
        onConfigChanged()
      } catch { /* keep polling */ }
    }, 2000)
  }

  const connect = async () => {
    onError(''); setAuthUrl('')
    try {
      const r = await api.xAuthStart()
      if (!r.ok) { onError(r.message || 'Could not start the X authorization.'); return }
      setAuthUrl(r.authorize_url || '')
      startPolling()
    } catch (e) { onError(e.message) }
  }

  const disconnect = async (acc) => {
    const label = acc.name || acc.id
    if (!window.confirm(`Disconnect “${label}”? Styles publishing to it fall back to the first remaining account.`)) return
    setBusy(acc.id); onError('')
    try {
      await api.xDisconnect(acc.id)
      await refresh()
      onConfigChanged()
    } catch (e) { onError(e.message) } finally { setBusy('') }
  }

  const toggleEng = (acc) => {
    if (expanded === acc.id) { setExpanded(''); return }
    setEng((e) => ({ ...e, [acc.id]: { engagement_prompt: acc.engagement_prompt || '', auto_respond: !!acc.auto_respond, language: acc.language || 'en', publish_per_day: acc.publish_per_day || 0 } }))
    setExpanded(acc.id)
  }
  const setEngField = (id, k, v) => setEng((e) => ({ ...e, [id]: { ...e[id], [k]: v } }))
  const saveEng = async (acc) => {
    setSavingEng(acc.id); onError('')
    try {
      await api.xAccountSettings(acc.id, eng[acc.id] || {})
      await refresh()
      setExpanded('')
    } catch (e) { onError(e.message) } finally { setSavingEng('') }
  }

  const importTokens = async () => {
    if (!tok.access.trim()) return
    setImportBusy(true); onError('')
    try {
      await api.xImportTokens(tok.access.trim(), tok.refresh.trim())
      setTok({ access: '', refresh: '' }); setShowImport(false)
      await refresh()
      onConfigChanged()
    } catch (e) { onError(e.message) } finally { setImportBusy(false) }
  }

  const importKeys = async () => {
    if (!Object.values(keys).every((v) => v.trim())) return
    setImportBusy(true); onError('')
    try {
      await api.xImportKeys({
        api_key: keys.api_key.trim(), api_secret: keys.api_secret.trim(),
        access_token: keys.access_token.trim(), access_secret: keys.access_secret.trim(),
      })
      setKeys({ api_key: '', api_secret: '', access_token: '', access_secret: '' })
      setShowImport(false)
      await refresh()
      onConfigChanged()
    } catch (e) { onError(e.message) } finally { setImportBusy(false) }
  }

  const rowStyle = { padding: '10px 12px', background: 'var(--paper-2)', borderRadius: 'var(--r-md)' }
  return (
    <Card span={12} className="reveal reveal-d1">
      <div className="row center between">
        <span className="label-sm">Accounts</span>
        <div className="row center gap-10">
          <Button variant="ghost" icon="key" onClick={() => setShowImport((v) => !v)}>Paste tokens</Button>
          <Button variant="primary" icon="x-twitter" brand disabled={connecting} onClick={connect}>
            {connecting ? 'Waiting for X…' : 'Connect X account'}
          </Button>
        </div>
      </div>
      <div className="field__hint" style={{ marginTop: 6 }}>
        Each connected X login is one account. Pick the account a style publishes to under <strong>Styles</strong>. Posting needs the X API client ID below; reading mentions and analytics needs a paid X API tier.
      </div>
      {connecting && authUrl && (
        <div className="stack gap-10" style={{ marginTop: 12, padding: '12px 14px', background: 'var(--paper-2)', borderRadius: 'var(--r-md)' }}>
          <div className="field__hint">
            A browser window opened for X authorization (this grants video-upload permission). If it shows <strong>“redirected you too many times”</strong>, copy this link and open it in an <strong>Incognito/Private window</strong> — a clean session avoids X’s loop, and approving there still finishes the connection here.
          </div>
          <input className="input" readOnly value={authUrl} onFocus={(e) => e.target.select()} style={{ fontSize: 11 }} />
          <div className="row center gap-10 row--wrap">
            <Button variant="primary" icon="copy" onClick={() => { try { navigator.clipboard.writeText(authUrl) } catch { /* select-and-copy fallback */ } }}>Copy authorize link</Button>
            <span className="muted" style={{ fontSize: 11.5 }}>Then paste it into a new Incognito window.</span>
          </div>
        </div>
      )}
      {showImport && (
        <div className="stack gap-10" style={{ marginTop: 12, padding: '12px 14px', background: 'var(--paper-2)', borderRadius: 'var(--r-md)' }}>
          <Segmented value={importMode} onChange={setImportMode} options={[
            { value: 'keys', label: 'API keys (no browser)' },
            { value: 'oauth2', label: 'OAuth 2.0 tokens' },
          ]} />
          {importMode === 'keys' ? (<>
            <div className="field__hint">
              <strong>Recommended.</strong> From the X developer portal → <strong>Keys and tokens</strong>: copy <strong>API Key &amp; Secret</strong> and generate an <strong>Access Token &amp; Secret</strong> (one click — no browser sign-in, no redirect loop). Make sure the app’s user-auth permission is <strong>Read and Write</strong> so it can upload video.
            </div>
            <Field label="API Key">
              <input className="input" value={keys.api_key} onChange={(e) => setKeys((k) => ({ ...k, api_key: e.target.value }))} />
            </Field>
            <Field label="API Key Secret">
              <input className="input" type="password" value={keys.api_secret} onChange={(e) => setKeys((k) => ({ ...k, api_secret: e.target.value }))} />
            </Field>
            <Field label="Access Token">
              <input className="input" value={keys.access_token} onChange={(e) => setKeys((k) => ({ ...k, access_token: e.target.value }))} />
            </Field>
            <Field label="Access Token Secret">
              <input className="input" type="password" value={keys.access_secret} onChange={(e) => setKeys((k) => ({ ...k, access_secret: e.target.value }))} />
            </Field>
            <div className="row center gap-10 row--wrap">
              <Button variant="primary" icon="plug" disabled={importBusy || !Object.values(keys).every((v) => v.trim())} onClick={importKeys}>
                {importBusy ? 'Connecting…' : 'Connect with keys'}
              </Button>
              <Button variant="ghost" onClick={() => setShowImport(false)}>Cancel</Button>
            </div>
          </>) : (<>
            <div className="field__hint">
              Paste OAuth 2.0 tokens you generated for this app. The access token is validated against X; the refresh token keeps it connected after it expires (~2h). Needs the <strong>media.write</strong> scope to upload video — set the Client ID + Secret below first so refresh works.
            </div>
            <Field label="Access token">
              <input className="input" value={tok.access} onChange={(e) => setTok((t) => ({ ...t, access: e.target.value }))} placeholder="OAuth 2.0 access token" />
            </Field>
            <Field label="Refresh token" hint="Optional but recommended — without it the connection drops when the access token expires.">
              <input className="input" value={tok.refresh} onChange={(e) => setTok((t) => ({ ...t, refresh: e.target.value }))} placeholder="OAuth 2.0 refresh token" />
            </Field>
            <div className="row center gap-10 row--wrap">
              <Button variant="primary" icon="plug" disabled={importBusy || !tok.access.trim()} onClick={importTokens}>
                {importBusy ? 'Connecting…' : 'Connect with tokens'}
              </Button>
              <Button variant="ghost" onClick={() => { setShowImport(false); setTok({ access: '', refresh: '' }) }}>Cancel</Button>
            </div>
          </>)}
        </div>
      )}
      <div className="stack gap-10 mt-16">
        {accounts === null && <div className="muted" style={{ fontSize: 13 }}>Checking…</div>}
        {accounts !== null && accounts.length === 0 && (
          <div className="muted" style={{ fontSize: 13 }}>No accounts connected yet — set the X API client ID below, then click <strong>Connect X account</strong>.</div>
        )}
        {(accounts || []).map((acc) => (
          <div key={acc.id} className="stack gap-10" style={rowStyle}>
            <div className="row center gap-10 row--wrap">
              <Icon name="x-twitter" brand style={{ color: 'var(--accent)' }} />
              <span style={{ fontWeight: 600 }}>{acc.name ? `@${acc.name}` : acc.id}</span>
              {acc.connected
                ? <Chip tone="ok" dot>connected</Chip>
                : <Chip tone="danger" dot title={acc.error}>not connected</Chip>}
              {!acc.connected && acc.error && <span className="muted" style={{ fontSize: 11.5 }}>{acc.error}</span>}
              {acc.premium ? <Chip tone="accent">Premium</Chip> : null}
              {acc.engagement_prompt ? <Chip tone="accent">engagement{acc.auto_respond ? ' · auto' : ''}</Chip> : null}
              <div className="grow" />
              <Button variant="ghost" icon="gear" onClick={() => toggleEng(acc)}>Settings</Button>
              <Button variant="danger" icon="link-slash" disabled={busy === acc.id} onClick={() => disconnect(acc)}>
                {busy === acc.id ? 'Removing…' : 'Disconnect'}
              </Button>
            </div>
            {expanded === acc.id && (
              <div className="stack gap-16" style={{ borderTop: '1px solid var(--line)', paddingTop: 14 }}>
                <Field label="Post language"
                  hint="Declared as this account's spoken/metadata language.">
                  <select className="select" value={eng[acc.id]?.language || 'en'}
                    onChange={(e) => setEngField(acc.id, 'language', e.target.value)}>
                    {Object.entries(LANGUAGES).map(([code, name]) => <option key={code} value={code}>{name}</option>)}
                  </select>
                </Field>
                <Field label="Publishing cadence"
                  hint="When scheduled publishing is on, this account spaces its posts evenly across the day. 0 = no throttle.">
                  <div className="row gap-16 row--wrap" style={{ alignItems: 'flex-end' }}>
                    <label className="stack gap-8" style={{ fontSize: 12.5 }}><span className="muted">Videos per day</span>
                      <input className="input" type="number" min={0} step="0.5" style={{ width: 120 }} value={eng[acc.id]?.publish_per_day ?? 0}
                        onChange={(e) => setEngField(acc.id, 'publish_per_day', Number(e.target.value) || 0)} /></label>
                    <span className="muted" style={{ fontSize: 12, paddingBottom: 8 }}>{cadenceHint(eng[acc.id]?.publish_per_day)}</span>
                  </div>
                </Field>
                <Field label="Community engagement prompt"
                  hint="How this account replies to mentions — its persona and what to do. Leave empty to disable engagement. (Reading mentions needs a paid X API tier.)">
                  <textarea className="textarea" rows={5} value={eng[acc.id]?.engagement_prompt || ''}
                    onChange={(e) => setEngField(acc.id, 'engagement_prompt', e.target.value)} />
                </Field>
                <Check checked={!!eng[acc.id]?.auto_respond} onChange={(v) => setEngField(acc.id, 'auto_respond', v)}
                  label="Automatically respond to mentions — post replies immediately instead of waiting for approval" />
                <div className="row center gap-10 row--wrap">
                  <Button variant="primary" icon="floppy-disk" disabled={savingEng === acc.id} onClick={() => saveEng(acc)}>
                    {savingEng === acc.id ? 'Saving…' : 'Save'}
                  </Button>
                  <Button variant="ghost" onClick={() => setExpanded('')}>Cancel</Button>
                  <span className="muted" style={{ fontSize: 11.5 }}>Saves immediately — separate from the main Save settings button.</span>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </Card>
  )
}

export default function Settings({ meta, setMeta, leaveGuardRef }) {
  const [cfg, setCfg] = useState(meta.config || {})
  // True while `cfg` holds Save-required edits not yet persisted. Voice and
  // channel ops auto-save server-side, so they deliberately don't set it.
  // A ref, not state: only the sync effect and leave guards read it.
  const dirtyRef = useRef(false)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const [vbusy, setVbusy] = useState(false)   // a voice operation is in flight
  const [restoring, setRestoring] = useState(false)  // a backup restore is in flight
  const restoreRef = useRef(null)
  const [workers, setWorkers] = useState(null)
  const [tab, setTab] = useState('infra')
  const [styleIdx, setStyleIdx] = useState(0)  // selected style in the Styles tab
  const [newOpen, setNewOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')

  // A fresh server config (initial load, Save, voice/channel op) replaces the
  // working copy — but never over staged edits: a voice op refreshing
  // meta.config used to silently wipe an unsaved narrator-voice change.
  useEffect(() => {
    if (!dirtyRef.current) setCfg(meta.config || {})
  }, [meta.config])

  // Re-sync with the server on every visit. The app fetches config once per
  // tab load, so a long-lived tab would otherwise show (and later Save —
  // clobbering newer values) a stale snapshot here.
  useEffect(() => { api.getConfig().then(setMeta).catch(() => {}) }, [setMeta])

  // Stage a Save-required edit and flag it as unsaved (see dirtyRef).
  const editCfg = (updater) => { dirtyRef.current = true; setCfg(updater) }

  // Warn before leaving with unsaved edits — in-app navigation consults the
  // guard (via App's `go`), reload/close hits beforeunload. Voice and channel
  // ops auto-save, so they never prompt.
  useEffect(() => {
    if (leaveGuardRef) leaveGuardRef.current = () => !dirtyRef.current ||
      window.confirm('You have unsaved settings changes. Leave without saving?')
    const warn = (e) => { if (dirtyRef.current) { e.preventDefault(); e.returnValue = '' } }
    window.addEventListener('beforeunload', warn)
    return () => {
      if (leaveGuardRef) leaveGuardRef.current = null
      window.removeEventListener('beforeunload', warn)
    }
  }, [leaveGuardRef])

  // Poll live cluster status (read-only). Start/stop is via `make start`/`stop`.
  useEffect(() => {
    let alive = true
    const tick = () => api.workerStatus().then((w) => { if (alive) setWorkers(w) }).catch(() => {})
    tick()
    const id = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  const set = (k, v) => editCfg((c) => ({ ...c, [k]: v }))

  // ── Style profiles (issue #66) ──
  // Each style bundles the script/content, render-quality and audio-mix
  // settings; the backend mirrors the default style onto the legacy flat keys.
  const styles = cfg.styles || []
  const st = styles[Math.min(styleIdx, Math.max(0, styles.length - 1))] || {}
  useEffect(() => {
    if (styleIdx >= styles.length && styles.length) setStyleIdx(styles.length - 1)
  }, [styles.length, styleIdx])

  const setStyleField = (k, v) => editCfg((c) => {
    const list = c.styles || []
    const cur = list[styleIdx]
    if (!cur) return c
    const next = { ...c, styles: list.map((s, i) => (i === styleIdx ? { ...s, [k]: v } : s)) }
    // Renaming the default style keeps it the default.
    if (k === 'name' && c.default_style === cur.name) next.default_style = v
    return next
  })
  // Update one field of one size bucket (small/medium/large) on the current style.
  const setSizePreset = (bucket, key, value) => {
    const presets = { ...(st.size_presets || {}) }
    presets[bucket] = { ...(presets[bucket] || {}), [key]: value }
    setStyleField('size_presets', presets)
  }
  // "(none)" is the reserved "No style" option on Create/Queue — not claimable.
  const nameTaken = (n) => n === '(none)' || styles.some((s) => s.name === n)
  const addStyle = () => {
    const name = newName.trim()
    if (!name || nameTaken(name)) return
    // A new style starts from the currently selected one — tweak from there.
    editCfg((c) => ({ ...c, styles: [...(c.styles || []), { ...st, name, description: newDesc.trim() }] }))
    setStyleIdx(styles.length)
    setNewOpen(false); setNewName(''); setNewDesc('')
  }
  const deleteStyle = () => {
    if (styles.length <= 1) return
    if (!window.confirm(`Delete style “${st.name}”? Videos already rendered keep their settings.`)) return
    editCfg((c) => {
      const list = (c.styles || []).filter((_, i) => i !== styleIdx)
      const next = { ...c, styles: list }
      if (c.default_style === st.name) next.default_style = list[0]?.name || ''
      return next
    })
    setStyleIdx(0)
  }
  const makeDefault = () => editCfg((c) => ({ ...c, default_style: st.name }))

  // Master toggle: derived from the per-step flags, and ticking it sets them all.
  const fullyAutomated = AUTO_FLAGS.every((f) => cfg[f])
  const setFullyAutomated = (v) => editCfg((c) => {
    const next = { ...c, youtube_fully_automated: v }
    AUTO_FLAGS.forEach((f) => { next[f] = v })
    return next
  })

  const save = async () => {
    setError(''); setStatus('')
    const names = (cfg.styles || []).map((s) => (s.name || '').trim())
    if (names.some((n) => !n || n === '(none)') || new Set(names).size !== names.length) {
      setError('Each style needs a unique, non-empty name — and “(none)” is reserved.')
      return
    }
    setBusy(true)
    try {
      const out = { ...cfg }
      out.youtube_fully_automated = AUTO_FLAGS.every((f) => cfg[f])
      out.comfy_workers = fromLines(toLines(cfg.comfy_workers))
      out.tts_workers = fromLines(toLines(cfg.tts_workers))
      const r = await api.saveConfig(out)
      setStatus('Settings saved.')
      dirtyRef.current = false   // saved — let the sync effect adopt r.config
      setMeta((m) => ({ ...m, config: r.config }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  // Voice ops persist immediately (each writes a file), separate from the Save
  // button. Merge the returned voice fields into the working copy so unsaved
  // edits to other fields survive; refresh meta so other screens see the change.
  // A rename/delete also rewrites the voice referenced by each saved style, so
  // sync that one field into the staged styles without clobbering other edits.
  const voiceOp = async (run) => {
    setError(''); setStatus(''); setVbusy(true)
    try {
      const r = await run()
      setCfg((c) => ({
        ...c,
        voices: r.config.voices,
        default_voice: r.config.default_voice,
        styles: (c.styles || []).map((s) => {
          const srv = (r.config.styles || []).find((x) => x.name === s.name)
          return srv ? { ...s, voice: srv.voice } : s
        }),
      }))
      setMeta((m) => ({ ...m, config: r.config, voices: r.voices }))
    } catch (e) { setError(e.message); throw e } finally { setVbusy(false) }
  }
  const addVoice = (name, file) => voiceOp(async () =>
    api.addVoice(name, file.name, await fileToDataUrl(file)))
  const updateVoice = (name, newName, file) => voiceOp(async () => {
    const fields = {}
    if (newName && newName !== name) fields.new_name = newName
    if (file) { fields.filename = file.name; fields.data = await fileToDataUrl(file) }
    return api.updateVoice(name, fields)
  })
  const deleteVoice = (name) => {
    if (!window.confirm(`Delete voice “${name}”? This removes its audio file.`)) return Promise.resolve()
    return voiceOp(() => api.deleteVoice(name))
  }

  // Connect/disconnect rewrites the channel list (and may clear style channel
  // refs) server-side — merge just those fields into the staged copy so other
  // unsaved edits survive.
  const reloadChannels = async () => {
    try {
      const r = await api.getConfig()
      setCfg((c) => ({
        ...c,
        youtube_channels: r.config.youtube_channels,
        styles: (c.styles || []).map((s) => {
          const srv = (r.config.styles || []).find((x) => x.name === s.name)
          return srv ? { ...s, channel: srv.channel } : s
        }),
      }))
    } catch { /* the next full load picks it up */ }
  }

  const reloadXAccounts = async () => {
    try {
      const r = await api.getConfig()
      setCfg((c) => ({
        ...c,
        x_accounts: r.config.x_accounts,
        styles: (c.styles || []).map((s) => {
          const srv = (r.config.styles || []).find((x) => x.name === s.name)
          return srv ? { ...s, x_account: srv.x_account } : s
        }),
      }))
    } catch { /* the next full load picks it up */ }
  }

  // Backup & restore (issue #106). Restore overlays the uploaded zip onto the
  // config dir, then we adopt the reloaded config — dropping any staged edits,
  // which the confirm warns about.
  const onRestoreFile = async (file) => {
    if (!file) return
    const ok = window.confirm(
      'Restore from this backup? Files in the backup overwrite the matching '
      + 'settings on this machine, and any unsaved changes here are discarded.')
    if (!ok) { if (restoreRef.current) restoreRef.current.value = ''; return }
    setError(''); setStatus(''); setRestoring(true)
    try {
      const r = await api.restoreSettings(await fileToDataUrl(file))
      dirtyRef.current = false
      setCfg(r.config)
      setMeta((m) => ({ ...m, config: r.config }))
      const n = r.restored?.length || 0
      setStatus(`Restored ${n} file${n === 1 ? '' : 's'} (${r.scope} backup). `
        + 'Restart the server if workers or the LLM backend changed.')
    } catch (e) {
      setError(e.message)
    } finally {
      setRestoring(false)
      if (restoreRef.current) restoreRef.current.value = ''
    }
  }

  const isClaude = cfg.llm_backend === 'claude'

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Settings</span>
          <h1 className="display-md reveal reveal-d1">Studio configuration</h1>
        </div>
        <Button variant="primary" icon="floppy-disk" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save settings'}</Button>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div style={{ marginBottom: 22 }}>
        <Segmented options={TABS.map((t) => ({ value: t.id, label: t.label }))} value={tab} onChange={setTab} />
      </div>

      <div className="bento">

        {tab === 'infra' && (<>
          {/* ── Infrastructure ── */}
          <Card span={6} className="reveal reveal-d1">
            <div className="row center between">
              <span className="label-sm">Infrastructure</span>
              <span className="muted" style={{ fontSize: 11.5 }}>start/stop via <code>make start</code></span>
            </div>
            <div className="stack gap-22 mt-16">
              <Field label="ComfyUI workers" hint="One URL per line. idle = free for the UI (the reserved worker during a render); busy = rendering.">
                <textarea className="textarea" rows={3} value={toLines(cfg.comfy_workers)} onChange={(e) => set('comfy_workers', e.target.value)} />
                <WorkerStatus items={workers?.comfy} load />
              </Field>
              <Field label="TTS workers" hint="One host per line.">
                <textarea className="textarea" rows={2} value={toLines(cfg.tts_workers)} onChange={(e) => set('tts_workers', e.target.value)} />
                <WorkerStatus items={workers?.tts} />
              </Field>
              <Field label="UI worker idle timeout (min)" hint="While the UI is in use, one render worker is kept idle for cover/preview jobs; it rejoins the render pool after the UI has been idle this long.">
                <input className="input" type="number" min={1} step={1}
                  value={Math.max(1, Math.round((cfg.ui_idle_timeout_seconds ?? 300) / 60))}
                  onChange={(e) => set('ui_idle_timeout_seconds', Math.max(1, +e.target.value || 1) * 60)} />
                <UiWorkerStatus ui={workers?.ui} />
              </Field>
            </div>
          </Card>

          {/* ── LLM backend ── */}
          <Card span={6} className="reveal reveal-d1">
            <span className="label-sm">LLM backend</span>
            <div className="stack gap-22 mt-16">
              <Field label="Backend">
                <Segmented value={cfg.llm_backend || 'local'} onChange={(v) => set('llm_backend', v)}
                  options={[{ value: 'local', label: 'Local (vLLM)' }, { value: 'claude', label: 'Claude' }]} />
              </Field>
              {isClaude ? (
                <>
                  <Field label="Claude API key"><input className="input" type="password" value={cfg.claude_api_key || ''} onChange={(e) => set('claude_api_key', e.target.value)} /></Field>
                  <Field label="Claude model"><input className="input" value={cfg.claude_model || ''} onChange={(e) => set('claude_model', e.target.value)} /></Field>
                </>
              ) : (
                <>
                  <Field label="Local LLM URL"><input className="input" value={cfg.local_llm_url || ''} onChange={(e) => set('local_llm_url', e.target.value)} /></Field>
                  <Field label="Local LLM model"><input className="input" value={cfg.local_llm_model || ''} onChange={(e) => set('local_llm_model', e.target.value)} /></Field>
                </>
              )}
            </div>
          </Card>

          {/* ── Voices ── */}
          <VoicesManager voices={cfg.voices} busy={vbusy} onAdd={addVoice} onUpdate={updateVoice} onDelete={deleteVoice} />

          {/* ── Backup & restore (issue #106) ── */}
          <Card span={12} className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm">Backup &amp; restore</span>
              <span className="muted" style={{ fontSize: 11.5 }}>for moving to new hardware</span>
            </div>
            <div className="field__hint" style={{ marginTop: 6 }}>
              <strong>Full backup</strong> bundles config, YouTube login (client secrets + channel tokens), voices, and operational state
              (queue, comments, analytics, AI ideas, engagement model). <strong>Operational state</strong> is just the app-accumulated
              data. Restoring overwrites the matching files on this machine.
            </div>
            <div className="row center gap-10 row--wrap mt-16">
              <a className="btn btn--primary" href={api.backupUrl('full')} download style={{ textDecoration: 'none' }}>
                <Icon name="download" /> Full backup
              </a>
              <a className="btn btn--ghost" href={api.backupUrl('operational')} download style={{ textDecoration: 'none' }}>
                <Icon name="download" /> Operational state only
              </a>
              <div className="grow" />
              <label className="btn btn--ghost" style={{ cursor: restoring ? 'not-allowed' : 'pointer' }}>
                <Icon name="upload" /> {restoring ? 'Restoring…' : 'Restore from backup…'}
                <input ref={restoreRef} type="file" accept=".zip,application/zip" hidden disabled={restoring}
                  onChange={(e) => onRestoreFile(e.target.files?.[0] || null)} />
              </label>
            </div>
          </Card>
        </>)}

        {tab === 'styles' && (<>
          {/* ── Style profiles (issue #66) ── */}
          <Card span={12} className="reveal reveal-d1">
            <div className="row center between">
              <span className="label-sm">Styles</span>
              <span className="muted" style={{ fontSize: 11.5 }}>Each style bundles script, render and audio-mix settings — pick one per video.</span>
            </div>
            <div className="row gap-6 row--wrap mt-16">
              {styles.map((s, i) => (
                <Button key={s.name || i} variant={i === styleIdx ? 'primary' : 'ghost'}
                  icon={cfg.default_style === s.name ? 'star' : undefined}
                  onClick={() => setStyleIdx(i)}>{s.name || '(unnamed)'}</Button>
              ))}
              <Button variant="ghost" icon="plus" onClick={() => setNewOpen((v) => !v)}>New style</Button>
            </div>
            {newOpen && (
              <div className="row center gap-10 row--wrap mt-16" style={{ borderTop: '1px solid var(--line)', paddingTop: 16 }}>
                <input className="input" placeholder="Style name" value={newName}
                  onChange={(e) => setNewName(e.target.value)} style={{ maxWidth: 220 }} />
                <div className="grow">
                  <input className="input" placeholder="Short description — what it looks and sounds like"
                    value={newDesc} onChange={(e) => setNewDesc(e.target.value)} />
                </div>
                <Button variant="primary" icon="plus" disabled={!newName.trim() || nameTaken(newName.trim())}
                  onClick={addStyle}>Create</Button>
              </div>
            )}
            {newOpen && nameTaken(newName.trim()) && (
              <div className="muted" style={{ fontSize: 12, marginTop: 6, color: 'var(--warn)' }}>A style with that name already exists.</div>
            )}
            <div className="field__hint" style={{ marginTop: 10 }}>
              A new style starts as a copy of the selected one. Remember to <strong>Save settings</strong> after editing.
            </div>
          </Card>

          {/* ── Identity ── */}
          <Card span={12} className="reveal reveal-d1">
            <div className="row center between">
              <span className="label-sm">Style — {st.name}</span>
              <div className="row gap-10 row--wrap">
                {cfg.default_style === st.name
                  ? <Chip tone="ok" dot>default style</Chip>
                  : <Button variant="ghost" icon="star" onClick={makeDefault}>Use as default</Button>}
                <Button variant="danger" icon="trash" disabled={styles.length <= 1} onClick={deleteStyle}>Delete</Button>
              </div>
            </div>
            <div className="stack gap-22 mt-16">
              <Field label="Name">
                <input className="input" value={st.name || ''} onChange={(e) => setStyleField('name', e.target.value)} style={{ maxWidth: 320 }} />
              </Field>
              <Field label="Description" hint="What this style is for — shown when choosing a style for a video.">
                <textarea className="textarea" rows={2} value={st.description || ''} onChange={(e) => setStyleField('description', e.target.value)} />
              </Field>
              <Field label="YouTube channel" hint="Where videos in this style are published — connect channels in the YouTube tab.">
                <select className="select" value={st.channel || ''} onChange={(e) => setStyleField('channel', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="">(first connected channel)</option>
                  {(cfg.youtube_channels || []).map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
                </select>
              </Field>
              <Field label="X account" hint="Which X account this style posts to (or none) — connect accounts in the X tab.">
                <select className="select" value={st.x_account || ''} onChange={(e) => setStyleField('x_account', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="">(none — don’t post to X)</option>
                  {(cfg.x_accounts || []).map((a) => <option key={a.id} value={a.id}>{a.name ? `@${a.name}` : a.id}</option>)}
                </select>
              </Field>
              <Check checked={!!st.auto_pick_exclude} onChange={(v) => setStyleField('auto_pick_exclude', v)}
                label="Exclude from auto-picked ideas — automation won’t top up an empty queue with this style (you can still pick it manually on the AI ideas screen)" />
            </div>
          </Card>

          {/* ── Script & content ── */}
          <Card span={12} className="reveal reveal-d2">
            <span className="label-sm">Script & content</span>
            <div className="stack gap-22 mt-16">
              <Field label="Default scenes">
                <input className="input" type="number" value={st.n_scenes ?? ''} onChange={(e) => setStyleField('n_scenes', +e.target.value)} style={{ maxWidth: 160 }} />
              </Field>
              <Field label="Visual style" hint="Applied to every scene's image prompt.">
                <input className="input" value={st.visual_style || ''} onChange={(e) => setStyleField('visual_style', e.target.value)} />
              </Field>
              <Field label="Video / motion style" hint="Steers how each scene moves — camera and subject motion in every scene's video prompt. e.g. “Favour dynamic action and visible movement over static shots and slow pans.”">
                <textarea className="textarea" rows={2} value={st.video_style || ''} onChange={(e) => setStyleField('video_style', e.target.value)} />
              </Field>
              <Field label="Title style" hint="How AI-suggested video titles are worded — e.g. “short and punchy” or “pose an intriguing question”.">
                <textarea className="textarea" rows={2} value={st.title_style || ''} onChange={(e) => setStyleField('title_style', e.target.value)} />
              </Field>
              <Field label="Extra script instructions" hint="Appended to every topic.">
                <textarea className="textarea" rows={8} value={st.extra_instructions || ''} onChange={(e) => setStyleField('extra_instructions', e.target.value)} />
              </Field>
              <Field label="YouTube description suffix" hint="Appended to every generated YouTube description for videos in this style.">
                <textarea className="textarea" rows={3} value={st.description_suffix || ''} onChange={(e) => setStyleField('description_suffix', e.target.value)} />
              </Field>
              {/* Narration — narrator beside the robotic toggle, the dial-in sliders and test right below */}
              <div className="row gap-22 row--wrap" style={{ alignItems: 'flex-end' }}>
                <div className="grow"><Field label="Narrator voice"><select className="select" value={st.voice || ''} onChange={(e) => setStyleField('voice', e.target.value)}>
                  <option value="">(F5-TTS default)</option>
                  {(meta.voices || []).filter((v) => v !== 'Default (F5-TTS)').map((v) => <option key={v} value={v}>{v}</option>)}
                </select></Field></div>
                <div className="grow" style={{ paddingBottom: 12 }}>
                  <Check checked={!!st.voice_robotic} onChange={(v) => setStyleField('voice_robotic', v)}
                    label="Robotic voice — synthetic monotone so it isn't mistaken for a human" />
                </div>
              </div>
              <div className="row gap-22 row--wrap">
                <div className="grow"><Field label={`Voice speed — ×${(st.voice_speed ?? 1).toFixed(2)}`}
                  hint="Narration pace — ×1.00 is natural, lower is slower, higher is faster.">
                  <input className="slider" type="range" min={0.5} max={1.5} step={0.05}
                    value={st.voice_speed ?? 1}
                    onChange={(e) => setStyleField('voice_speed', +e.target.value)} />
                </Field></div>
                <div className="grow"><Field label={`Robotic level — ${Math.round((st.voice_robotic_amount ?? 0.35) * 100)}%`}
                  hint="0% is natural, higher is more synthetic — used when “Robotic voice” is on.">
                  <input className="slider" type="range" min={0} max={1} step={0.05}
                    value={st.voice_robotic_amount ?? 0.35}
                    onChange={(e) => setStyleField('voice_robotic_amount', +e.target.value)} />
                </Field></div>
              </div>
              <VoiceTester voice={st.voice} roboticAmount={st.voice_robotic_amount} speed={st.voice_speed} onError={setError} />
            </div>
          </Card>

          {/* ── Render quality ── */}
          <Card span={6} className="reveal reveal-d3">
            <span className="label-sm">Render quality</span>
            <div className="stack gap-22 mt-16">
              <Field label="Resolution" hint="Orientation, then quality (higher = slower).">
                <ResolutionPicker value={st.resolution || ''} onChange={(r) => setStyleField('resolution', r)} meta={meta} />
              </Field>
              <div className="row gap-22 row--wrap">
                <div className="grow"><Field label="First-pass steps" hint="8 distilled · 20–30 dev model.">
                  <input className="input" type="number" value={st.first_pass_steps ?? ''} onChange={(e) => setStyleField('first_pass_steps', +e.target.value)} /></Field></div>
                <div className="grow"><Field label="Second-pass steps">
                  <input className="input" type="number" value={st.second_pass_steps ?? ''} onChange={(e) => setStyleField('second_pass_steps', +e.target.value)} /></Field></div>
              </div>
              <Field label={`LoRA strength — ${st.lora_strength ?? 0}`}>
                <input className="slider" type="range" min={0} max={1} step={0.05} value={st.lora_strength ?? 0} onChange={(e) => setStyleField('lora_strength', +e.target.value)} />
              </Field>
            </div>
          </Card>

          {/* ── Narrator & audio ── */}
          <Card span={6} className="reveal reveal-d3">
            <span className="label-sm">Narrator & audio</span>
            <div className="stack gap-22 mt-16">
              {[['voice_vol', 'Voice volume', 150], ['music_vol', 'Music volume', 100], ['ambient_vol', 'Ambient volume', 100]].map(([k, label, max]) => (
                <Field key={k} label={`${label} — ${st[k] ?? 0}%`}>
                  <input className="slider" type="range" min={0} max={max} value={st[k] ?? 0} onChange={(e) => setStyleField(k, +e.target.value)} />
                </Field>
              ))}
            </div>
          </Card>

          {/* ── Size presets (Small/Medium/Large) ── */}
          <Card span={12} className="reveal reveal-d3">
            <span className="label-sm">Size presets</span>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
              The Small / Medium / Large one-tap sizes on the AI ideas screen — each sets a scene count and a resolution for this style.
            </div>
            <div className="stack gap-22 mt-16">
              {(meta.size_buckets || ['small', 'medium', 'large']).map((bucket) => {
                const preset = (st.size_presets || {})[bucket] || (meta.default_size_presets || {})[bucket] || {}
                return (
                  <div key={bucket} className="row gap-22 row--wrap" style={{ alignItems: 'flex-end' }}>
                    <div style={{ minWidth: 78 }}>
                      <span className="label-sm" style={{ textTransform: 'capitalize' }}>{bucket}</span>
                    </div>
                    <Field label="Scenes">
                      <input className="input" type="number" min={1} value={preset.scenes ?? ''} style={{ maxWidth: 110 }}
                        onChange={(e) => setSizePreset(bucket, 'scenes', +e.target.value)} />
                    </Field>
                    <div className="grow"><Field label="Resolution">
                      <ResolutionPicker value={preset.resolution || ''} onChange={(r) => setSizePreset(bucket, 'resolution', r)} meta={meta} />
                    </Field></div>
                  </div>
                )
              })}
            </div>
          </Card>
        </>)}

        {tab === 'youtube' && (<>
          {/* ── YouTube channels (issue #22) ── */}
          <ChannelsCard onConfigChanged={reloadChannels} onError={setError} />
          <Card span={12} className="reveal reveal-d2">
            <span className="label-sm">Google API</span>
            <div className="stack gap-22 mt-16">
              <Field label="Client secrets file" hint="Path to the OAuth client JSON from Google Cloud Console — one app shared by every channel.">
                <input className="input" value={cfg.youtube_client_secrets || ''} onChange={(e) => set('youtube_client_secrets', e.target.value)} />
              </Field>
            </div>
          </Card>
        </>)}

        {tab === 'x' && (<>
          {/* ── X (Twitter) accounts (issue #107) ── */}
          <XAccountsCard onConfigChanged={reloadXAccounts} onError={setError} />
          <Card span={12} className="reveal reveal-d2">
            <span className="label-sm">X API</span>
            <div className="stack gap-22 mt-16">
              <Field label="Client ID" hint="OAuth 2.0 Client ID from the X developer portal. Register the redirect URI http://127.0.0.1:8723/callback on the app.">
                <input className="input" value={cfg.x_client_id || ''} onChange={(e) => set('x_client_id', e.target.value)} />
              </Field>
              <Field label="Client secret" hint="Only for confidential X apps. Leave blank for a public (PKCE-only) app.">
                <input className="input" type="password" value={cfg.x_client_secret || ''} onChange={(e) => set('x_client_secret', e.target.value)} />
              </Field>
              <Field label="Default post text" hint="Appended to every tweet (like the YouTube description suffix). Optional.">
                <input className="input" value={cfg.x_post_default_text || ''} onChange={(e) => set('x_post_default_text', e.target.value)} />
              </Field>
            </div>
          </Card>
        </>)}

        {tab === 'automation' && (<>
          {/* ── YouTube automation ── */}
          <Card span={12} className="reveal reveal-d1">
            <span className="label-sm">YouTube automation</span>
            <div className="stack gap-16 mt-16">
              <Check checked={fullyAutomated} onChange={setFullyAutomated} label="⚡ Fully automated mode — turns on every step below" />
              <Check checked={!!cfg.youtube_auto_fetch_evaluate} onChange={(v) => set('youtube_auto_fetch_evaluate', v)} label="Fetch & evaluate comments on a schedule" />
              <Check checked={!!cfg.youtube_auto_approve_comments} onChange={(v) => set('youtube_auto_approve_comments', v)} label="Auto-approve requests above the confidence threshold" />
              <Check checked={!!cfg.youtube_auto_start_job} onChange={(v) => set('youtube_auto_start_job', v)} label="Auto-start the next queue item with a ready script — loops until the queue is empty" />
              <Check checked={!!cfg.youtube_auto_approve_script} onChange={(v) => set('youtube_auto_approve_script', v)} label="Auto-approve scripts — also write missing scripts and render them without review" />
              <Check checked={!!cfg.youtube_auto_ai_ideas} onChange={(v) => set('youtube_auto_ai_ideas', v)} label="Top up the queue with an AI idea when it runs empty (needs auto-approved scripts)" />
              <Check checked={!!cfg.youtube_auto_post} onChange={(v) => set('youtube_auto_post', v)} label="Auto-post to YouTube when a film finishes" />
              <Field label="Default privacy">
                <Segmented value={cfg.youtube_post_privacy || 'private'} onChange={(v) => set('youtube_post_privacy', v)} options={['private', 'unlisted', 'public']} />
              </Field>
            </div>
          </Card>
          {/* ── X automation (issue #107) ── */}
          <Card span={12} className="reveal reveal-d2">
            <span className="label-sm">X automation</span>
            <div className="stack gap-16 mt-16">
              <Check checked={!!cfg.x_auto_fetch_evaluate} onChange={(v) => set('x_auto_fetch_evaluate', v)} label="Fetch & evaluate X mentions on a schedule (needs a paid X API tier)" />
              <Check checked={!!cfg.x_auto_approve_comments} onChange={(v) => set('x_auto_approve_comments', v)} label="Auto-approve X requests above the confidence threshold" />
              <Check checked={!!cfg.x_auto_post} onChange={(v) => set('x_auto_post', v)} label="Auto-post to X when a film finishes — uses the film's style X account; long videos fall back to the YouTube link on non-Premium" />
            </div>
          </Card>
          {/* ── Publishing schedule — decouples publishing from rendering ── */}
          <Card span={12} className="reveal reveal-d3">
            <span className="label-sm">Publishing schedule</span>
            <div className="field__hint" style={{ marginTop: 6 }}>
              Needs auto-post (above) on. When enabled, finished videos enter a publish queue and are released on each channel/account's own cadence — set the per-channel and per-account <strong>Videos per day</strong> in the YouTube and X tabs. Review the queue under <strong>Publishing → Schedule</strong>.
            </div>
            <div className="stack gap-16 mt-16">
              <Check checked={!!cfg.publish_schedule_enabled} onChange={(v) => set('publish_schedule_enabled', v)}
                label="Schedule publishing instead of posting the moment a film finishes" />
              <Check checked={cfg.publish_schedule_skip_comment_requests !== false} disabled={!cfg.publish_schedule_enabled}
                onChange={(v) => set('publish_schedule_skip_comment_requests', v)}
                label="Let comment-requested videos skip the schedule and post immediately" />
            </div>
          </Card>
        </>)}

      </div>
    </div>
  )
}
