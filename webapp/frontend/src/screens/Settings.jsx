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
  'youtube_auto_post',
]

// Extract a short display name from a worker URL or hostname
function shortHost(url) {
  try { return new URL(url).hostname } catch { return url }
}

// Compact inline status row shown under each worker textarea
function WorkerStatus({ items, probed = true, extra }) {
  if (!items) return <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>Checking…</div>
  if (!items.length) return null
  if (!probed) {
    return (
      <div className="row gap-6 row--wrap" style={{ marginTop: 6 }}>
        {items.map((w) => <Chip key={w.host} tone="neutral">{w.host}</Chip>)}
        <span className="muted" style={{ fontSize: 11 }}>not probed</span>
      </div>
    )
  }
  const down = items.filter((w) => !w.up)
  return (
    <div className="row gap-6 row--wrap" style={{ marginTop: 6 }}>
      {down.length === 0
        ? <Chip tone="ok" dot>all up</Chip>
        : down.map((w) => <Chip key={w.endpoint} tone="danger" dot>{shortHost(w.endpoint)} down</Chip>)}
      {extra}
    </div>
  )
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

// Audition a voice at the current robotic level. F5-TTS runs on the backend
// (a few seconds for one sentence), so show a "Generating…" state while waiting.
// Reads the live, unsaved robotic-level slider so you can dial it in by ear
// before saving. 0% = natural voice.
function VoiceTester({ voices, defaultVoice, roboticAmount, onError }) {
  const [voice, setVoice] = useState(defaultVoice || '')
  const [busy, setBusy] = useState(false)
  const audioRef = useRef(null)

  const play = async () => {
    onError(''); setBusy(true)
    try {
      const amount = roboticAmount ?? 0.35
      const r = await api.testVoice({ voice, robotic: amount > 0, robotic_amount: amount })
      const a = audioRef.current
      if (a) { a.src = r.url; a.load(); await a.play() }
    } catch (e) { onError(e.message) } finally { setBusy(false) }
  }

  const spoken = voice || 'the default narrator'
  return (
    <Field label="Test voice"
      hint={`Generates “This is the voice of ${spoken}. What do you think?” at the robotic level above (cached after the first time).`}>
      <div className="row center gap-10 row--wrap">
        <select className="select" value={voice} onChange={(e) => setVoice(e.target.value)} style={{ maxWidth: 220 }}>
          <option value="">(F5-TTS default)</option>
          {(voices || []).filter((v) => v !== 'Default (F5-TTS)').map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
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
  { id: 'automation', label: 'Automation' },
]

// Connected YouTube channels (issue #22). Connecting runs the backend OAuth
// flow (a Google window opens on the server machine); each channel's token is
// stored separately, and styles pick which channel they publish to.
function ChannelsCard({ onConfigChanged, onError }) {
  const [channels, setChannels] = useState(null)
  const [connecting, setConnecting] = useState(false)
  const [busy, setBusy] = useState('')
  const pollRef = useRef(null)

  const refresh = () => api.ytChannels()
    .then((r) => { setChannels(r.channels || []); if (r.auth_running) startPolling() })
    .catch((e) => onError(e.message))
  useEffect(() => { refresh(); return () => clearInterval(pollRef.current) }, [])

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
          <div key={ch.id} className="row center gap-10 row--wrap" style={rowStyle}>
            <Icon name="youtube" style={{ color: 'var(--accent)' }} />
            <span style={{ fontWeight: 600 }}>{ch.name || ch.id}</span>
            {ch.connected
              ? <Chip tone="ok" dot>connected</Chip>
              : <Chip tone="danger" dot title={ch.error}>not connected</Chip>}
            {!ch.connected && ch.error && <span className="muted" style={{ fontSize: 11.5 }}>{ch.error}</span>}
            <div className="grow" />
            <Button variant="danger" icon="link-slash" disabled={busy === ch.id} onClick={() => disconnect(ch)}>
              {busy === ch.id ? 'Removing…' : 'Disconnect'}
            </Button>
          </div>
        ))}
      </div>
    </Card>
  )
}

export default function Settings({ meta, setMeta }) {
  const [cfg, setCfg] = useState(meta.config || {})
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const [vbusy, setVbusy] = useState(false)   // a voice operation is in flight
  const [workers, setWorkers] = useState(null)
  const [tab, setTab] = useState('infra')
  const [styleIdx, setStyleIdx] = useState(0)  // selected style in the Styles tab
  const [newOpen, setNewOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')

  useEffect(() => { setCfg(meta.config || {}) }, [meta.config])

  // Poll live cluster status (read-only). Start/stop is via `make start`/`stop`.
  useEffect(() => {
    let alive = true
    const tick = () => api.workerStatus().then((w) => { if (alive) setWorkers(w) }).catch(() => {})
    tick()
    const id = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  const set = (k, v) => setCfg((c) => ({ ...c, [k]: v }))

  // ── Style profiles (issue #66) ──
  // Each style bundles the script/content, render-quality and audio-mix
  // settings; the backend mirrors the default style onto the legacy flat keys.
  const styles = cfg.styles || []
  const st = styles[Math.min(styleIdx, Math.max(0, styles.length - 1))] || {}
  useEffect(() => {
    if (styleIdx >= styles.length && styles.length) setStyleIdx(styles.length - 1)
  }, [styles.length, styleIdx])

  const setStyleField = (k, v) => setCfg((c) => {
    const list = c.styles || []
    const cur = list[styleIdx]
    if (!cur) return c
    const next = { ...c, styles: list.map((s, i) => (i === styleIdx ? { ...s, [k]: v } : s)) }
    // Renaming the default style keeps it the default.
    if (k === 'name' && c.default_style === cur.name) next.default_style = v
    return next
  })
  // "(none)" is the reserved "No style" option on Create/Queue — not claimable.
  const nameTaken = (n) => n === '(none)' || styles.some((s) => s.name === n)
  const addStyle = () => {
    const name = newName.trim()
    if (!name || nameTaken(name)) return
    // A new style starts from the currently selected one — tweak from there.
    setCfg((c) => ({ ...c, styles: [...(c.styles || []), { ...st, name, description: newDesc.trim() }] }))
    setStyleIdx(styles.length)
    setNewOpen(false); setNewName(''); setNewDesc('')
  }
  const deleteStyle = () => {
    if (styles.length <= 1) return
    if (!window.confirm(`Delete style “${st.name}”? Videos already rendered keep their settings.`)) return
    setCfg((c) => {
      const list = (c.styles || []).filter((_, i) => i !== styleIdx)
      const next = { ...c, styles: list }
      if (c.default_style === st.name) next.default_style = list[0]?.name || ''
      return next
    })
    setStyleIdx(0)
  }
  const makeDefault = () => setCfg((c) => ({ ...c, default_style: st.name }))

  // Master toggle: derived from the per-step flags, and ticking it sets them all.
  const fullyAutomated = AUTO_FLAGS.every((f) => cfg[f])
  const setFullyAutomated = (v) => setCfg((c) => {
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
      out.ui_workers = fromLines(toLines(cfg.ui_workers))
      const r = await api.saveConfig(out)
      setStatus('Settings saved.')
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
              <Field label="ComfyUI workers" hint="One URL per line.">
                <textarea className="textarea" rows={3} value={toLines(cfg.comfy_workers)} onChange={(e) => set('comfy_workers', e.target.value)} />
                <WorkerStatus items={workers?.comfy} />
              </Field>
              <Field label="TTS workers" hint="One host per line.">
                <textarea className="textarea" rows={2} value={toLines(cfg.tts_workers)} onChange={(e) => set('tts_workers', e.target.value)} />
                <WorkerStatus items={workers?.tts} probed={false} />
              </Field>
              <Field label="UI workers" hint="ComfyUI URLs for cover-image regeneration. One per line.">
                <textarea className="textarea" rows={2} value={toLines(cfg.ui_workers)} onChange={(e) => set('ui_workers', e.target.value)} />
                <WorkerStatus items={workers?.ui}
                  extra={workers && (workers.ui_worker_running
                    ? <Chip tone="ok" dot>worker running</Chip>
                    : <Chip tone="warn" dot>worker not running</Chip>)} />
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
            </div>
          </Card>

          {/* ── Script & content ── */}
          <Card span={12} className="reveal reveal-d2">
            <span className="label-sm">Script & content</span>
            <div className="stack gap-22 mt-16">
              <div className="row gap-22 row--wrap">
                <div className="grow"><Field label="Default scenes"><input className="input" type="number" value={st.n_scenes ?? ''} onChange={(e) => setStyleField('n_scenes', +e.target.value)} /></Field></div>
                <div className="grow"><Field label="Narrator voice"><select className="select" value={st.voice || ''} onChange={(e) => setStyleField('voice', e.target.value)}>
                  <option value="">(F5-TTS default)</option>
                  {(meta.voices || []).filter((v) => v !== 'Default (F5-TTS)').map((v) => <option key={v} value={v}>{v}</option>)}
                </select></Field></div>
              </div>
              <Field label="Visual style" hint="Applied to every scene's image prompt.">
                <input className="input" value={st.visual_style || ''} onChange={(e) => setStyleField('visual_style', e.target.value)} />
              </Field>
              <Field label="Extra script instructions" hint="Appended to every topic.">
                <textarea className="textarea" rows={8} value={st.extra_instructions || ''} onChange={(e) => setStyleField('extra_instructions', e.target.value)} />
              </Field>
              <Field label="YouTube description suffix" hint="Appended to every generated YouTube description for videos in this style.">
                <textarea className="textarea" rows={3} value={st.description_suffix || ''} onChange={(e) => setStyleField('description_suffix', e.target.value)} />
              </Field>
              <Check checked={!!st.voice_robotic} onChange={(v) => setStyleField('voice_robotic', v)}
                label="Robotic voice — synthetic monotone so it isn't mistaken for a human" />
              <Field label={`Robotic level — ${Math.round((st.voice_robotic_amount ?? 0.35) * 100)}%`}
                hint="How strong the robotic effect is — 0% is natural, higher is more synthetic. The test below plays at this level; renders use it when “Robotic voice” is on.">
                <input className="slider" type="range" min={0} max={1} step={0.05}
                  value={st.voice_robotic_amount ?? 0.35}
                  onChange={(e) => setStyleField('voice_robotic_amount', +e.target.value)} />
              </Field>
              <VoiceTester key={st.name} voices={meta.voices} defaultVoice={st.voice}
                roboticAmount={st.voice_robotic_amount} onError={setError} />
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

        {tab === 'automation' && (
          /* ── YouTube automation ── */
          <Card span={12} className="reveal reveal-d1">
            <span className="label-sm">YouTube automation</span>
            <div className="stack gap-16 mt-16">
              <Check checked={fullyAutomated} onChange={setFullyAutomated} label="⚡ Fully automated mode — turns on every step below" />
              <Check checked={!!cfg.youtube_auto_fetch_evaluate} onChange={(v) => set('youtube_auto_fetch_evaluate', v)} label="Fetch & evaluate comments on a schedule" />
              <Check checked={!!cfg.youtube_auto_approve_comments} onChange={(v) => set('youtube_auto_approve_comments', v)} label="Auto-approve requests above the confidence threshold" />
              <Check checked={!!cfg.youtube_auto_start_job} onChange={(v) => set('youtube_auto_start_job', v)} label="Auto-prepare the highest-interest request (generate its script)" />
              <Check checked={!!cfg.youtube_auto_approve_script} onChange={(v) => set('youtube_auto_approve_script', v)} label="Auto-approve generated scripts — render without review" />
              <Check checked={!!cfg.youtube_auto_post} onChange={(v) => set('youtube_auto_post', v)} label="Auto-post to YouTube when a film finishes" />
              <Field label="Default privacy">
                <Segmented value={cfg.youtube_post_privacy || 'private'} onChange={(v) => set('youtube_post_privacy', v)} options={['private', 'unlisted', 'public']} />
              </Field>
            </div>
          </Card>
        )}

      </div>
    </div>
  )
}
