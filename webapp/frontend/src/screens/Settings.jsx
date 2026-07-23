import { useState, useEffect, useRef } from 'react'
import { Card, Field, Segmented, ResolutionPicker, Check, Button, Banner, Chip, Icon, VersionStrip, ImageLightbox, voiceMetaMap, voiceLabel } from '../components.jsx'
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

// Per-host container power: start/stop/restart each worker's ComfyUI + TTS
// docker stack over SSH (the same path as `make start/stop W=<host>`). The
// machine stays on — only the containers toggle. Hosts are the union of the
// configured comfy + tts workers; the chip mirrors the host's render state.
function WorkerControls({ workers, busyHost, onAction }) {
  const comfy = workers?.comfy || []
  const hosts = Array.from(new Set([...comfy, ...(workers?.tts || [])].map((w) => shortHost(w.endpoint))))
  if (!hosts.length) return null
  return (
    <Field label="Container power" hint="Start or stop each host's ComfyUI + TTS containers over SSH. The machine stays on; only the docker stack toggles. Stopping a busy worker interrupts its render.">
      <div className="stack gap-10 mt-6">
        {hosts.map((h) => {
          const c = comfy.find((w) => shortHost(w.endpoint) === h)
          const [tone, label] = !c ? ['neutral', '—'] : !c.up ? ['danger', 'off'] : c.busy ? ['warn', 'busy'] : ['ok', 'idle']
          const acting = busyHost === h
          return (
            <div key={h} className="row center between gap-10">
              <div className="row center gap-10" style={{ minWidth: 0 }}>
                <span style={{ fontSize: 13, fontWeight: 500 }}>{h}</span>
                <Chip tone={tone} dot>{label}</Chip>
              </div>
              <div className="row gap-6">
                <Button variant="ghost" disabled={acting} onClick={() => onAction(h, 'start')}>{acting ? '…' : 'Start'}</Button>
                <Button variant="ghost" disabled={acting} onClick={() => onAction(h, 'stop')}>Stop</Button>
                <Button variant="ghost" disabled={acting} onClick={() => onAction(h, 'restart')}>Restart</Button>
              </div>
            </div>
          )
        })}
      </div>
    </Field>
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

// Decode a recorded blob and re-encode it as 16-bit PCM mono WAV so the TTS
// workers never see a browser codec (webm/opus from MediaRecorder).
async function blobToWavFile(blob, name) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)()
  try {
    const buf = await ctx.decodeAudioData(await blob.arrayBuffer())
    const n = buf.length, rate = buf.sampleRate, ch = buf.numberOfChannels
    const mono = new Float32Array(n)
    for (let c = 0; c < ch; c++) {
      const d = buf.getChannelData(c)
      for (let i = 0; i < n; i++) mono[i] += d[i] / ch
    }
    const out = new DataView(new ArrayBuffer(44 + n * 2))
    const str = (o, s) => { for (let i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)) }
    str(0, 'RIFF'); out.setUint32(4, 36 + n * 2, true); str(8, 'WAVEfmt ')
    out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true)
    out.setUint32(24, rate, true); out.setUint32(28, rate * 2, true)
    out.setUint16(32, 2, true); out.setUint16(34, 16, true)
    str(36, 'data'); out.setUint32(40, n * 2, true)
    for (let i = 0; i < n; i++) {
      const s = Math.max(-1, Math.min(1, mono[i]))
      out.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
    }
    return new File([out.buffer], name, { type: 'audio/wav' })
  } finally { ctx.close() }
}

// Record a reference clip with the microphone (issue #192): pick a script
// language, read the passage shown, review the take, and hand it to the
// add-voice form as if it had been chosen from disk.
function RecordVoiceModal({ languages, onUse, onClose }) {
  const [lang, setLang] = useState('en')
  const [script, setScript] = useState('')
  const [scriptBusy, setScriptBusy] = useState(false)
  const [recording, setRecording] = useState(false)
  const [processing, setProcessing] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [take, setTake] = useState(null)          // { file, url }
  const [error, setError] = useState('')
  const recRef = useRef(null)                     // { mr, stream, timer }

  const langs = languages && Object.keys(languages).length ? languages : { en: 'English' }

  const fetchScript = async (l, fresh = false) => {
    setScriptBusy(true); setError('')
    try { const r = await api.voiceReadingScript(l, fresh); setScript(r.text || '') }
    catch (e) { setError(e.message) } finally { setScriptBusy(false) }
  }
  useEffect(() => { fetchScript(lang) }, [lang])

  // Whatever way the modal goes away, release the mic and the timer.
  useEffect(() => () => {
    const r = recRef.current
    if (r) { clearInterval(r.timer); r.stream.getTracks().forEach((t) => t.stop()) }
  }, [])

  const start = async () => {
    setError('')
    if (!navigator.mediaDevices?.getUserMedia) {
      setError('Recording needs a secure connection (HTTPS or localhost) — the browser exposes no microphone here.')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mr = new MediaRecorder(stream)
      const chunks = []
      mr.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data) }
      mr.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop())
        clearInterval(recRef.current?.timer)
        recRef.current = null
        setRecording(false); setProcessing(true)
        try {
          const raw = new Blob(chunks, { type: mr.mimeType || 'audio/webm' })
          const file = await blobToWavFile(raw, 'voice-recording.wav')
          setTake((old) => { if (old) URL.revokeObjectURL(old.url); return { file, url: URL.createObjectURL(file) } })
        } catch (e) { setError(`Could not process the recording: ${e.message}`) }
        setProcessing(false)
      }
      recRef.current = { mr, stream, timer: setInterval(() => setElapsed((s) => s + 1), 1000) }
      setElapsed(0)
      mr.start()
      setRecording(true)
    } catch (e) {
      setError(e.name === 'NotAllowedError'
        ? 'Microphone access was denied — allow it in the browser and try again.'
        : `Microphone unavailable: ${e.message}`)
    }
  }
  const stop = () => { const r = recRef.current; if (r && r.mr.state !== 'inactive') r.mr.stop() }
  const fmt = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

  return (
    <div onClick={() => !recording && onClose()}
      style={{ position: 'fixed', inset: 0, zIndex: 1100, background: 'rgba(0,0,0,.82)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
      <div onClick={(e) => e.stopPropagation()}
        style={{ background: 'var(--paper)', borderRadius: 'var(--r-lg)', padding: 20, width: 'min(640px, 96vw)', maxHeight: '92vh', overflowY: 'auto', boxShadow: '0 30px 80px rgba(0,0,0,.5)' }}>
        <div className="row center between" style={{ marginBottom: 12 }}>
          <span className="h-title">Record a voice</span>
          <button type="button" className="btn btn--quiet" onClick={onClose}><Icon name="xmark" /></button>
        </div>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 0, marginBottom: 12 }}>
          Read the script below in a quiet room — aim for 15–30 seconds of clear, natural speech.
          The take becomes the voice's reference clip.
        </p>

        <div className="row center gap-10 row--wrap" style={{ marginBottom: 12 }}>
          <select className="input" style={{ maxWidth: 180 }} value={lang} disabled={recording}
            onChange={(e) => setLang(e.target.value)}>
            {Object.entries(langs).map(([c, l]) => <option key={c} value={c}>{l}</option>)}
          </select>
          <Button variant="ghost" icon="rotate" disabled={scriptBusy || recording}
            onClick={() => fetchScript(lang, true)}>{scriptBusy ? 'Writing…' : 'New script'}</Button>
        </div>

        <div style={{ background: 'var(--paper-2)', borderRadius: 'var(--r-md)', padding: '14px 16px', fontSize: 15.5, lineHeight: 1.55, minHeight: 72, whiteSpace: 'pre-wrap' }}>
          {script || (scriptBusy ? 'Writing a script…' : '')}
        </div>

        <div className="row center gap-10 row--wrap mt-16">
          {!recording && (
            <Button variant="primary" icon="microphone" disabled={processing} onClick={start}>
              {take ? 'Re-record' : 'Start recording'}
            </Button>
          )}
          {recording && (
            <>
              <Button variant="danger" icon="stop" onClick={stop}>Stop</Button>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontWeight: 600 }}>
                <span style={{ width: 10, height: 10, borderRadius: '50%', background: 'var(--danger)', animation: 'recpulse 1.1s ease-in-out infinite' }} />
                {fmt(elapsed)}
              </span>
            </>
          )}
          {processing && <span className="muted" style={{ fontSize: 13 }}>Processing…</span>}
          {take && !recording && !processing && (
            <>
              <audio controls src={take.url} style={{ height: 34 }} />
              <div className="grow" />
              <Button variant="primary" icon="check" onClick={() => onUse(take.file)}>Use this recording</Button>
            </>
          )}
        </div>

        {error && <Banner tone="danger">{error}</Banner>}
      </div>
    </div>
  )
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
function VoiceTester({ voice, roboticAmount, speed, engine, language, onError }) {
  const [busy, setBusy] = useState(false)
  const audioRef = useRef(null)

  const play = async () => {
    onError(''); setBusy(true)
    try {
      const amount = roboticAmount ?? 0.35
      const r = await api.testVoice({ voice: voice || '', robotic: amount > 0, robotic_amount: amount, speed: speed ?? 1, engine: engine || '', language: language || '' })
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
const VOICE_GENDERS = ['', 'male', 'female']
const VOICE_AGES = ['', 'young', 'adult', 'mature', 'elderly']

// Casting-metadata inputs shared by the add form and the row editor. Gender +
// age drive the automatic voice pick for story characters; a voice without a
// gender is never auto-cast (manual pick only).
function VoiceMetaFields({ meta, onChange }) {
  const set = (k, v) => onChange({ ...meta, [k]: v })
  return (
    <>
      <select className="input" style={{ maxWidth: 110 }} value={meta.gender || ''} onChange={(e) => set('gender', e.target.value)}>
        {VOICE_GENDERS.map((g) => <option key={g} value={g}>{g || 'gender…'}</option>)}
      </select>
      <select className="input" style={{ maxWidth: 110 }} value={meta.age || ''} onChange={(e) => set('age', e.target.value)}>
        {VOICE_AGES.map((a) => <option key={a} value={a}>{a || 'age…'}</option>)}
      </select>
      <input className="input" placeholder="accent (e.g. British)" value={meta.accent || ''}
        onChange={(e) => set('accent', e.target.value)} style={{ maxWidth: 150 }} />
      <input className="input" placeholder="tone (e.g. deep, warm)" value={meta.tone || ''}
        onChange={(e) => set('tone', e.target.value)} style={{ maxWidth: 150 }} />
    </>
  )
}

function VoicesManager({ voices, busy, ttsLanguages, onAdd, onUpdate, onDelete }) {
  const [name, setName] = useState('')
  const [file, setFile] = useState(null)
  const [addMeta, setAddMeta] = useState({})
  const [editing, setEditing] = useState(null)   // name of the voice being edited
  const [editName, setEditName] = useState('')
  const [editFile, setEditFile] = useState(null)
  const [editMeta, setEditMeta] = useState({})
  const [recOpen, setRecOpen] = useState(false)
  const addRef = useRef(null)

  const add = async () => {
    try { await onAdd(name.trim(), file, addMeta) } catch { return }
    setName(''); setFile(null); setAddMeta({}); if (addRef.current) addRef.current.value = ''
  }
  const startEdit = (v) => {
    setEditing(v.name); setEditName(v.name); setEditFile(null)
    setEditMeta({ gender: v.gender || '', age: v.age || '', accent: v.accent || '', tone: v.tone || '' })
  }
  const cancelEdit = () => { setEditing(null); setEditName(''); setEditFile(null); setEditMeta({}) }
  const saveEdit = async (v) => {
    try { await onUpdate(v.name, editName.trim(), editFile, editMeta) } catch { return }
    cancelEdit()
  }
  const castChips = (v) => [v.gender, v.age, v.accent, v.tone].filter(Boolean).join(' · ')

  const rowStyle = { padding: '10px 12px', background: 'var(--paper-2)', borderRadius: 'var(--r-md)' }

  return (
    <Card span={12} className="reveal reveal-d2">
      <div className="row center between">
        <span className="label-sm">Voices</span>
        <span className="muted" style={{ fontSize: 11.5 }}>changes save immediately</span>
      </div>
      <div className="field__hint" style={{ marginTop: 6 }}>
        Reference clips (15–30s of clear speech) F5-TTS clones for narration and dialogue. Pick each
        style's narrator voice under <strong>Styles</strong>. <strong>Gender + age</strong> let story
        characters be auto-cast with a fitting voice — a voice without a gender is never auto-cast.
      </div>

      <div className="stack gap-10 mt-16">
        {(voices || []).length === 0 && (
          <div className="muted" style={{ fontSize: 13 }}>No voices yet — add one below.</div>
        )}
        {(voices || []).map((v) => (
          <div key={v.name} className="row center gap-10 row--wrap" style={rowStyle}>
            {editing === v.name ? (
              <>
                <input className="input" value={editName} onChange={(e) => setEditName(e.target.value)} style={{ maxWidth: 170 }} />
                <VoiceMetaFields meta={editMeta} onChange={setEditMeta} />
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
                {castChips(v) && <span className="muted" style={{ fontSize: 12 }}>{castChips(v)}</span>}
                {v.library && <span className="muted" style={{ fontSize: 11, border: '1px solid var(--line)', borderRadius: 6, padding: '1px 6px' }}>library</span>}
                <div className="grow" />
                <Button variant="ghost" icon="pen" disabled={busy} onClick={() => startEdit(v)}>Edit</Button>
                <Button variant="danger" icon="trash" disabled={busy} onClick={() => onDelete(v.name)}>Delete</Button>
              </>
            )}
          </div>
        ))}
      </div>

      <div className="row center gap-10 row--wrap mt-16" style={{ borderTop: '1px solid var(--line)', paddingTop: 16 }}>
        <input className="input" placeholder="New voice name" value={name} onChange={(e) => setName(e.target.value)} style={{ maxWidth: 170 }} />
        <VoiceMetaFields meta={addMeta} onChange={setAddMeta} />
        <label className="btn btn--ghost">
          <Icon name="upload" /> {file ? file.name : 'Choose audio…'}
          <input ref={addRef} type="file" accept="audio/*" hidden onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </label>
        <Button variant="ghost" icon="microphone" disabled={busy} onClick={() => setRecOpen(true)}>Record…</Button>
        <Button variant="primary" icon="plus" disabled={busy || !name.trim() || !file} onClick={add}>Add voice</Button>
      </div>
      {recOpen && (
        <RecordVoiceModal languages={ttsLanguages} onClose={() => setRecOpen(false)}
          onUse={(f) => { setFile(f); setRecOpen(false); if (addRef.current) addRef.current.value = '' }} />
      )}
    </Card>
  )
}

const TABS = [
  { id: 'infra', label: 'Infrastructure' },
  { id: 'styles', label: 'Styles' },
  { id: 'characters', label: 'Characters' },
  { id: 'voices', label: 'Voices' },
  { id: 'channels', label: 'Channels' },
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

export default function Settings({ meta, setMeta, leaveGuardRef, go }) {
  const [cfg, setCfg] = useState(meta.config || {})
  // True while `cfg` holds Save-required edits not yet persisted. Voice and
  // channel ops auto-save server-side, so they deliberately don't set it.
  // A ref, not state: only the sync effect and leave guards read it.
  const dirtyRef = useRef(false)
  // Reactive mirror of dirtyRef — character image ops persist server-side, so
  // they're only offered when there are no unsaved edits to clobber.
  const [dirty, setDirty] = useState(false)
  const [charBusy, setCharBusy] = useState('')  // character id with an image op in flight
  const [charBust, setCharBust] = useState(0)   // cache-bust token for character thumbnails
  const [charLightbox, setCharLightbox] = useState(null)  // character being viewed full-res
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const [vbusy, setVbusy] = useState(false)   // a voice operation is in flight
  const [restoring, setRestoring] = useState(false)  // a backup restore is in flight
  const restoreRef = useRef(null)
  const [workers, setWorkers] = useState(null)
  const [workerBusy, setWorkerBusy] = useState('')  // host with a start/stop action in flight
  const [clearingDeclined, setClearingDeclined] = useState(false)  // declined-ideas reset in flight
  const [engineInfo, setEngineInfo] = useState(null)  // {engines, availability, hf_token_set, default_engine}
  const [ttsEngineInfo, setTtsEngineInfo] = useState(null)  // {engines, availability, default_engine}
  const [engInstall, setEngInstall] = useState({})    // engine key -> install status payload
  const [tab, setTab] = useState('infra')
  const [styleIdx, setStyleIdx] = useState(0)  // selected style in the Styles tab
  const [playlists, setPlaylists] = useState({})  // channel key -> {loading, items:[{id,title}], error}
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

  // Image engines: registry + per-worker model availability (for the picker +
  // the download buttons). Refreshed after an install completes.
  const reloadEngines = () => api.listEngines().then(setEngineInfo).catch(() => {})
  useEffect(() => { reloadEngines() }, [])
  const installEngine = async (key) => {
    setEngInstall((m) => ({ ...m, [key]: { status: 'running' } }))
    try {
      const { task_id } = await api.installEngine(key)
      const poll = async () => {
        try {
          const s = await api.installEngineStatus(task_id)
          setEngInstall((m) => ({ ...m, [key]: s }))
          if (s.status === 'running') setTimeout(poll, 4000)
          else reloadEngines()
        } catch (e) { setEngInstall((m) => ({ ...m, [key]: { status: 'error', error: e.message } })) }
      }
      setTimeout(poll, 3000)
    } catch (e) {
      setEngInstall((m) => ({ ...m, [key]: { status: 'error', error: e.message } }))
    }
  }

  // TTS narration models: registry + per-worker availability + download buttons.
  // Mirrors the image-engine flow; reuses the shared engInstall status map (keys
  // don't collide with the flux* engines) and the install-status endpoint.
  const reloadTtsEngines = () => api.listTtsEngines().then(setTtsEngineInfo).catch(() => {})
  useEffect(() => { reloadTtsEngines() }, [])
  const installTtsEngine = async (key) => {
    setEngInstall((m) => ({ ...m, [key]: { status: 'running' } }))
    try {
      const { task_id } = await api.installTtsEngine(key)
      const poll = async () => {
        try {
          const s = await api.installEngineStatus(task_id)
          setEngInstall((m) => ({ ...m, [key]: s }))
          if (s.status === 'running') setTimeout(poll, 4000)
          else reloadTtsEngines()
        } catch (e) { setEngInstall((m) => ({ ...m, [key]: { status: 'error', error: e.message } })) }
      }
      setTimeout(poll, 3000)
    } catch (e) {
      setEngInstall((m) => ({ ...m, [key]: { status: 'error', error: e.message } }))
    }
  }

  // Stage a Save-required edit and flag it as unsaved (see dirtyRef).
  const editCfg = (updater) => { dirtyRef.current = true; setDirty(true); setCfg(updater) }

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

  // Poll live cluster status; the Container power controls below act on it.
  useEffect(() => {
    let alive = true
    const tick = () => api.workerStatus().then((w) => { if (alive) setWorkers(w) }).catch(() => {})
    tick()
    const id = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  // Start/stop/restart a host's worker containers over SSH. Stopping a worker
  // mid-render kills that render, so confirm first when it's busy.
  const controlWorker = async (host, action) => {
    const c = (workers?.comfy || []).find((w) => shortHost(w.endpoint) === host)
    if ((action === 'stop' || action === 'restart') && c?.busy &&
      !window.confirm(`${host} is rendering. ${action === 'stop' ? 'Stopping' : 'Restarting'} it will interrupt that render. Continue?`)) return
    setError(''); setStatus(''); setWorkerBusy(host)
    try {
      await api.controlWorker(host, action)
      setStatus(`${host}: containers ${action === 'start' ? 'started' : action === 'stop' ? 'stopped' : 'restarted'}.`)
      api.workerStatus().then(setWorkers).catch(() => {})
    } catch (e) {
      setError(`${host}: ${action} failed — ${e.message}`)
    } finally {
      setWorkerBusy('')
    }
  }

  // Empty the declined ("not accepted") ideas list. Forgets every declined idea
  // so the AI no longer treats those topics as rejected; ignored ideas stay
  // hidden. Operational data, so it doesn't touch the Save-required config.
  const resetDeclinedIdeas = async () => {
    if (!window.confirm('Clear the declined ideas list? The AI will stop treating those topics as rejected (they may resurface in future suggestions). Ignored ideas stay hidden.')) return
    setError(''); setStatus(''); setClearingDeclined(true)
    try {
      const r = await api.resetDeclinedSuggestions()
      setStatus(`Declined ideas cleared${typeof r.cleared === 'number' ? ` (${r.cleared}).` : '.'}`)
    } catch (e) {
      setError(`Could not clear declined ideas — ${e.message}`)
    } finally {
      setClearingDeclined(false)
    }
  }

  const set = (k, v) => editCfg((c) => {
    const next = { ...c, [k]: v }
    // Immediate auto-post and scheduled publishing are mutually exclusive —
    // turning one on clears the other so they can never both be active.
    if (v && k === 'publish_schedule_enabled') { next.youtube_auto_post = false; next.x_auto_post = false }
    else if (v && (k === 'youtube_auto_post' || k === 'x_auto_post')) { next.publish_schedule_enabled = false }
    return next
  })

  // ── Style profiles (issue #66) ──
  // Each style bundles the script/content, render-quality and audio-mix
  // settings; the backend mirrors the default style onto the legacy flat keys.
  const styles = cfg.styles || []
  const st = styles[Math.min(styleIdx, Math.max(0, styles.length - 1))] || {}
  useEffect(() => {
    if (styleIdx >= styles.length && styles.length) setStyleIdx(styles.length - 1)
  }, [styles.length, styleIdx])

  // Playlists for the active style's channel (per-style playlist picker below).
  // Effective channel mirrors the backend's channel_for_style fallback (own
  // channel, else first connected) so the list matches the real upload target.
  const stChannel = st.channel || (cfg.youtube_channels?.[0]?.id || '')
  useEffect(() => {
    if (tab !== 'styles' || stChannel in playlists) return
    setPlaylists((p) => ({ ...p, [stChannel]: { loading: true, items: [], error: '' } }))
    api.ytPlaylists(stChannel)
      .then((r) => setPlaylists((p) => ({ ...p, [stChannel]: { loading: false, items: r.playlists || [], error: r.error || '' } })))
      .catch((e) => setPlaylists((p) => ({ ...p, [stChannel]: { loading: false, items: [], error: String(e.message || e) } })))
  }, [tab, stChannel, playlists])
  const stPlaylists = playlists[stChannel] || { loading: true, items: [], error: '' }

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
  // Recurring characters are a GLOBAL library (their own Characters tab); each
  // style opts into the ones it uses by id. The backend normalizes/ids these on
  // save (_norm_characters), so the UI can add bare rows and drop blank aliases.
  const chars = cfg.characters || []
  const addChar = () => set('characters', [...chars, { name: '', aliases: [], description: '', enabled: true }])
  const updateChar = (i, patch) => set('characters', chars.map((c, j) => (j === i ? { ...c, ...patch } : c)))
  const removeChar = (i) => set('characters', chars.filter((_, j) => j !== i))
  // Which global characters the selected style opts into.
  const styleCharIds = st.character_ids || []
  const toggleStyleChar = (id, on) => setStyleField('character_ids',
    on ? [...new Set([...styleCharIds, id])] : styleCharIds.filter((x) => x !== id))
  // Character reference images persist server-side immediately (like voice ops),
  // so they're gated on a clean form. Merge the fresh global library back into
  // the working copy and bump the thumbnail cache-bust token.
  const characterOp = async (charId, run) => {
    setError(''); setStatus(''); setCharBusy(charId)
    try {
      const r = await run()
      setCfg((c) => ({ ...c, characters: r.config.characters }))
      setMeta((m) => ({ ...m, config: r.config }))
      setCharBust((n) => n + 1)
    } catch (e) { setError(e.message) } finally { setCharBusy('') }
  }
  const uploadCharImage = (char, file) => characterOp(char.id, async () =>
    api.setCharacterImage(char.id, file.name, await fileToDataUrl(file)))
  const clearCharImage = (char) => characterOp(char.id, () => api.clearCharacterImage(char.id))
  const genCharPortrait = (char) => characterOp(char.id, () => api.generateCharacterPortrait(char.id, ''))
  const selectCharVersion = (char, versionId) => characterOp(char.id, () => api.selectCharacterImage(char.id, versionId))
  const deleteCharVersion = (char, versionId) => characterOp(char.id, () => api.deleteCharacterImage(char.id, versionId))
  // Index of the selected look within a character's version list — where the
  // full-res lightbox opens.
  const charSelVerIdx = (c) => {
    const vs = c.history?.versions || []
    const i = vs.findIndex((v) => v.id === c.history?.selected)
    return i < 0 ? Math.max(0, vs.length - 1) : i
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
    // Fully automated uses immediate auto-post, which excludes the schedule.
    if (v) next.publish_schedule_enabled = false
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
      // The connected-channel / X-account lists are owned entirely by their own
      // endpoints (connect/disconnect + the per-channel/account Settings panel,
      // which saves publish_per_day & co. immediately). The main form only reads
      // them; sending the mount-time snapshot back here would clobber any cadence
      // edit made through those panels with a stale value.
      delete out.youtube_channels
      delete out.x_accounts
      out.youtube_fully_automated = AUTO_FLAGS.every((f) => cfg[f])
      out.comfy_workers = fromLines(toLines(cfg.comfy_workers))
      out.tts_workers = fromLines(toLines(cfg.tts_workers))
      out.echomimic_workers = fromLines(toLines(cfg.echomimic_workers))
      const r = await api.saveConfig(out)
      setStatus('Settings saved.')
      dirtyRef.current = false; setDirty(false)   // saved — let the sync effect adopt r.config
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
  const addVoice = (name, file, meta = {}) => voiceOp(async () =>
    api.addVoice(name, file.name, await fileToDataUrl(file), meta))
  const updateVoice = (name, newName, file, meta = {}) => voiceOp(async () => {
    const fields = { ...meta }
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
      dirtyRef.current = false; setDirty(false)
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

  const llmBackend = cfg.llm_backend || 'local'

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
              <span className="muted" style={{ fontSize: 11.5 }}>containers toggle below · full stack via <code>make start</code></span>
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
              <Field label="EchoMimic workers" hint="One URL per line (port 8190). Talking-head engine for dialogue/performance scenes.">
                <textarea className="textarea" rows={2} value={toLines(cfg.echomimic_workers)} onChange={(e) => set('echomimic_workers', e.target.value)} />
                <WorkerStatus items={workers?.echomimic} />
              </Field>
              <WorkerControls workers={workers} busyHost={workerBusy} onAction={controlWorker} />
              <Field label="UI worker idle timeout (min)" hint="While the UI is in use, one render worker is kept idle for cover/preview jobs; it rejoins the render pool after the UI has been idle this long.">
                <input className="input" type="number" min={1} step={1}
                  value={Math.max(1, Math.round((cfg.ui_idle_timeout_seconds ?? 300) / 60))}
                  onChange={(e) => set('ui_idle_timeout_seconds', Math.max(1, +e.target.value || 1) * 60)} />
                <UiWorkerStatus ui={workers?.ui} />
              </Field>
              <Field label="Temporal AI upscale timeout (min)" hint="Maximum time for the packaged ComfyUI LTX upscaler to process a finished film.">
                <input className="input" type="number" min={1} step={1}
                  value={Math.max(1, Math.round((cfg.temporal_video_upscaler_timeout ?? 7200) / 60))}
                  onChange={(e) => set('temporal_video_upscaler_timeout', Math.max(1, +e.target.value || 1) * 60)} />
              </Field>
            </div>
          </Card>

          {/* ── LLM backend ── */}
          <Card span={6} className="reveal reveal-d1">
            <span className="label-sm">LLM backend</span>
            <div className="stack gap-22 mt-16">
              <Field label="Backend" hint="Scripts, ideas, descriptions, and comment helpers all use this backend.">
                <Segmented value={llmBackend} onChange={(v) => set('llm_backend', v)}
                  options={[
                    { value: 'local', label: 'Local' },
                    { value: 'claude', label: 'Claude' },
                    { value: 'grok', label: 'Grok' },
                    { value: 'openai', label: 'OpenAI' },
                  ]} />
              </Field>
              {llmBackend === 'claude' && (
                <>
                  <Field label="Claude API key"><input className="input" type="password" placeholder={cfg.claude_api_key_set ? '•••••••• (saved — leave blank to keep)' : 'sk-ant-…'} value={cfg.claude_api_key || ''} onChange={(e) => set('claude_api_key', e.target.value)} /></Field>
                  <Field label="Claude model"><input className="input" value={cfg.claude_model || ''} onChange={(e) => set('claude_model', e.target.value)} /></Field>
                </>
              )}
              {llmBackend === 'grok' && (
                <>
                  <Field label="Grok API key" hint="xAI API key (console.x.ai). You can also set XAI_API_KEY in the environment.">
                    <input className="input" type="password" placeholder={cfg.grok_api_key_set ? '•••••••• (saved — leave blank to keep)' : 'xai-…'} value={cfg.grok_api_key || ''} onChange={(e) => set('grok_api_key', e.target.value)} />
                  </Field>
                  <Field label="Grok model" hint="e.g. grok-4.5, grok-3, grok-3-mini">
                    <input className="input" value={cfg.grok_model || 'grok-4.5'} onChange={(e) => set('grok_model', e.target.value)} />
                  </Field>
                </>
              )}
              {llmBackend === 'openai' && (
                <>
                  <Field label="OpenAI API key" hint="platform.openai.com API key. You can also set OPENAI_API_KEY in the environment.">
                    <input className="input" type="password" placeholder={cfg.openai_api_key_set ? '•••••••• (saved — leave blank to keep)' : 'sk-…'} value={cfg.openai_api_key || ''} onChange={(e) => set('openai_api_key', e.target.value)} />
                  </Field>
                  <Field label="OpenAI model" hint="e.g. gpt-4o, gpt-4.1, gpt-4o-mini">
                    <input className="input" value={cfg.openai_model || 'gpt-4o'} onChange={(e) => set('openai_model', e.target.value)} />
                  </Field>
                </>
              )}
              {llmBackend === 'local' && (
                <>
                  <Field label="Local LLM URL"><input className="input" value={cfg.local_llm_url || ''} onChange={(e) => set('local_llm_url', e.target.value)} /></Field>
                  <Field label="Local LLM model"><input className="input" value={cfg.local_llm_model || ''} onChange={(e) => set('local_llm_model', e.target.value)} /></Field>
                </>
              )}
            </div>
          </Card>

          {/* ── Image models (engines) ── */}
          <Card span={12} className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm">Image models</span>
              <span className="muted" style={{ fontSize: 11.5 }}>pick per style under <strong>Styles</strong> · download here</span>
            </div>
            <div className="stack gap-22 mt-16">
              <Field label="Hugging Face token"
                hint="Needed to auto-download gated model weights. Create a read token at huggingface.co/settings/tokens and accept each model's license on its HF page first.">
                <input className="input" type="password"
                  placeholder={engineInfo?.hf_token_set ? '•••••••• (saved — leave blank to keep)' : 'hf_…'}
                  value={cfg.hf_token || ''} onChange={(e) => set('hf_token', e.target.value)} />
              </Field>
              <div className="stack gap-10">
                {!engineInfo && <div className="muted" style={{ fontSize: 12 }}>Loading engines…</div>}
                {(engineInfo?.engines || []).map((e) => {
                  const avail = engineInfo?.availability?.[e.key]
                  const ins = engInstall[e.key]
                  const running = ins?.status === 'running'
                  return (
                    <div key={e.key} className="row center between" style={{ borderTop: '1px solid var(--line)', paddingTop: 10, gap: 12 }}>
                      <div className="grow">
                        <div className="row center gap-8" style={{ flexWrap: 'wrap' }}>
                          <span style={{ fontWeight: 600 }}>{e.label}</span>
                          {avail === true && <Chip tone="ok" dot>installed</Chip>}
                          {avail === false && <Chip tone="warn">not installed</Chip>}
                          {!e.commercial_ok && <Chip tone="info">non-commercial</Chip>}
                        </div>
                        <div className="muted" style={{ fontSize: 12 }}>{e.sub} · {e.license}</div>
                        {ins?.status === 'error' && <div style={{ color: 'var(--danger)', fontSize: 12 }}>Download failed{ins.error ? `: ${ins.error}` : ' — see workers'}</div>}
                        {ins?.status === 'done' && <div style={{ color: 'var(--ok)', fontSize: 12 }}>Download complete</div>}
                      </div>
                      <Button variant="ghost" size="sm" icon={running ? 'spinner' : 'download'}
                        disabled={running || avail === true}
                        onClick={() => installEngine(e.key)}>
                        {running ? 'Downloading…' : avail === true ? 'Installed' : 'Download'}
                      </Button>
                    </div>
                  )
                })}
              </div>
              <div className="field__hint">Downloads run on every ComfyUI worker over SSH and can take a while (weights are several GB). FLUX.2 is authored from public templates and may need a workflow tweak on first use.</div>
            </div>
          </Card>

          {/* ── Voice models (TTS engines) ── */}
          <Card span={12} className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm">Voice models</span>
              <span className="muted" style={{ fontSize: 11.5 }}>pick per style under <strong>Styles</strong> · download here</span>
            </div>
            <div className="stack gap-10 mt-16">
              {!ttsEngineInfo && <div className="muted" style={{ fontSize: 12 }}>Loading voice models…</div>}
              {(ttsEngineInfo?.engines || []).map((e) => {
                const hosts = ttsEngineInfo?.availability || {}
                const reachable = Object.values(hosts).filter((v) => v && typeof v === 'object')
                const avail = reachable.length ? reachable.every((h) => h[e.key]) : null
                const ins = engInstall[e.key]
                const running = ins?.status === 'running'
                return (
                  <div key={e.key} className="row center between" style={{ borderTop: '1px solid var(--line)', paddingTop: 10, gap: 12 }}>
                    <div className="grow">
                      <div className="row center gap-8" style={{ flexWrap: 'wrap' }}>
                        <span style={{ fontWeight: 600 }}>{e.label}</span>
                        {avail === true && <Chip tone="ok" dot>installed</Chip>}
                        {avail === false && <Chip tone="warn">not installed</Chip>}
                        {!e.commercial_ok && <Chip tone="info">non-commercial</Chip>}
                      </div>
                      <div className="muted" style={{ fontSize: 12 }}>{e.sub} · {e.license}</div>
                      {ins?.status === 'error' && <div style={{ color: 'var(--danger)', fontSize: 12 }}>Download failed{ins.error ? `: ${ins.error}` : ' — see workers'}</div>}
                      {ins?.status === 'done' && <div style={{ color: 'var(--ok)', fontSize: 12 }}>Download complete</div>}
                    </div>
                    <Button variant="ghost" size="sm" icon={running ? 'spinner' : 'download'}
                      disabled={running || avail === true}
                      onClick={() => installTtsEngine(e.key)}>
                      {running ? 'Downloading…' : avail === true ? 'Installed' : 'Download'}
                    </Button>
                  </div>
                )
              })}
            </div>
            <div className="field__hint">Downloads run on every TTS worker (into its model cache) and can take a while (weights are several GB). Models also download automatically on first use.</div>
          </Card>

          {/* ── Predictive model (issue #50) ── */}
          <Card span={12} className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm">Predictive model</span>
              <span className="muted" style={{ fontSize: 11.5 }}>rebuild on the <strong>Predictive Model</strong> tab to apply</span>
            </div>
            <div className="row gap-22 mt-16 row--wrap">
              <Field label="Prediction horizon (days)"
                hint="What the model predicts: total views over a video's first N calendar days. Changing this needs a rebuild.">
                <input className="input" type="number" min={1} max={30} step={1} style={{ width: 120 }}
                  value={cfg.engagement_prediction_days ?? 3}
                  onChange={(e) => set('engagement_prediction_days', Math.max(1, Math.min(30, +e.target.value || 1)))} />
              </Field>
              <Field label="Minimum training samples"
                hint="Below this many usable videos the model is flagged low-confidence (“insufficient”).">
                <input className="input" type="number" min={1} step={1} style={{ width: 120 }}
                  value={cfg.engagement_min_samples ?? 15}
                  onChange={(e) => set('engagement_min_samples', Math.max(1, +e.target.value || 1))} />
              </Field>
              <Field label="Data lag / exclusion (days)"
                hint="Videos newer than this are excluded from training — the Analytics API finalises a day's views a few days late.">
                <input className="input" type="number" min={0} step={1} style={{ width: 120 }}
                  value={cfg.engagement_data_lag_days ?? 3}
                  onChange={(e) => set('engagement_data_lag_days', Math.max(0, +e.target.value || 0))} />
              </Field>
            </div>
          </Card>

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

          {/* ── Prompt editor ── Advanced: the raw model instructions behind every
              generation. Lives on its own screen, and stays read-only there
              until unlocked. */}
          <Card span={12} className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm">Prompts</span>
              <span className="muted" style={{ fontSize: 11.5 }}>advanced</span>
            </div>
            <div className="field__hint" style={{ marginTop: 6 }}>
              The raw instructions sent to the language and image models for every film — scripts, narration, image
              prompts, descriptions, tags and replies. Editing them changes all future generations; the originals are
              always one click away.
            </div>
            <div className="row center gap-10 mt-16">
              <Button icon="sliders" onClick={() => go('prompts')}>Open prompt editor</Button>
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
              <Field label="YouTube channel" hint="Where videos in this style are published — connect channels in the Channels tab.">
                <select className="select" value={st.channel || ''} onChange={(e) => setStyleField('channel', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="">(first connected channel)</option>
                  {(cfg.youtube_channels || []).map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
                </select>
              </Field>
              <Field label="YouTube playlist" hint="Add every upload in this style to a playlist on its channel. “Auto-create” makes (or reuses) one named after the style.">
                <select className="select" value={st.youtube_playlist_id || ''} onChange={(e) => setStyleField('youtube_playlist_id', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="">(none — don’t add to a playlist)</option>
                  <option value="__auto__">Auto-create playlist named after the style</option>
                  {stPlaylists.items.map((p) => <option key={p.id} value={p.id}>{p.title || p.id}</option>)}
                  {st.youtube_playlist_id && st.youtube_playlist_id !== '__auto__'
                    && !stPlaylists.items.some((p) => p.id === st.youtube_playlist_id)
                    && <option value={st.youtube_playlist_id}>{stPlaylists.loading ? 'Loading…' : `${st.youtube_playlist_id} (not on this channel)`}</option>}
                </select>
                {stPlaylists.error
                  ? <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Couldn’t load playlists: {stPlaylists.error}</div>
                  : stPlaylists.loading
                    ? <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Loading playlists…</div>
                    : null}
              </Field>
              <Field label="X account" hint="Which X account this style posts to (or none) — connect accounts in the Channels tab.">
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
              <Field label="Script mode" hint="Story-first writes and judges the whole story as prose before dividing it into scenes — keeps long videos coherent (in Create you can review and edit the story before scene division). Classic generates scenes directly. Dialogue/Mixed formats always use Classic.">
                <select className="select" value={st.script_mode || 'classic'} onChange={(e) => setStyleField('script_mode', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="classic">Classic — scenes directly</option>
                  <option value="story">Story-first — draft, judge, then divide</option>
                </select>
              </Field>
              <Field label="Visual style" hint="Applied to every scene's image prompt.">
                <input className="input" value={st.visual_style || ''} onChange={(e) => setStyleField('visual_style', e.target.value)} />
              </Field>
              <Field label="Video / motion style" hint="Steers how each scene moves — camera and subject motion in every scene's video prompt. e.g. “Favour dynamic action and visible movement over static shots and slow pans.”">
                <textarea className="textarea" rows={2} value={st.video_style || ''} onChange={(e) => setStyleField('video_style', e.target.value)} />
              </Field>
              <Field label="Video negative prompt" hint="Things to keep OUT of every video render in this style (artifacts, unwanted objects, styles). Leave blank to use the built-in quality default (blur, watermark, distortion, …).">
                <textarea className="textarea" rows={3} value={st.video_negative_prompt || ''} onChange={(e) => setStyleField('video_negative_prompt', e.target.value)} />
              </Field>
              <Field label="Title style" hint="How AI-suggested video titles are worded — e.g. “short and punchy” or “pose an intriguing question”.">
                <textarea className="textarea" rows={2} value={st.title_style || ''} onChange={(e) => setStyleField('title_style', e.target.value)} />
              </Field>
              <Field label="Extra script instructions" hint="Appended to every topic.">
                <textarea className="textarea" rows={8} value={st.extra_instructions || ''} onChange={(e) => setStyleField('extra_instructions', e.target.value)} />
              </Field>
              <Field label="Script — avoid" hint="Tell the writer what to keep OUT of the script for this style (topics, words, tropes, tone). e.g. “no politics, avoid the word ‘journey’, don't be preachy.”">
                <textarea className="textarea" rows={4} value={st.script_avoid || ''} onChange={(e) => setStyleField('script_avoid', e.target.value)} />
              </Field>
              <Field label="YouTube description suffix" hint="Appended to every generated YouTube description for videos in this style.">
                <textarea className="textarea" rows={3} value={st.description_suffix || ''} onChange={(e) => setStyleField('description_suffix', e.target.value)} />
              </Field>
              <Field label="Attribution footer" hint="Credit line appended to every YouTube description for this style. Cleared = no credit line.">
                <textarea className="textarea" rows={2} value={st.attribution_description || ''} onChange={(e) => setStyleField('attribution_description', e.target.value)} />
              </Field>
              <Field label="Attribution X hashtags" hint="Extra hashtags appended to X posts for this style — space or comma separated, the “#” is optional. e.g. “stephenspielbot”.">
                <input className="input" value={st.attribution_hashtags || ''} onChange={(e) => setStyleField('attribution_hashtags', e.target.value)} />
              </Field>
              <Field label="Attribution YouTube tags" hint="Extra keyword tags appended to YouTube uploads for this style — comma separated. e.g. “stephenspielbot”.">
                <input className="input" value={st.attribution_youtube_tags || ''} onChange={(e) => setStyleField('attribution_youtube_tags', e.target.value)} />
              </Field>
              {/* Voice model (TTS engine) — which narration model synthesises this style */}
              <Field label="Voice model" hint="Which TTS model synthesises this style's narration. OpenF5 is Apache-2.0 (commercial-safe); non-commercial models are flagged. Download models under Infrastructure.">
                <select className="select" value={st.tts_engine || 'openf5'} onChange={(e) => setStyleField('tts_engine', e.target.value)}>
                  {(ttsEngineInfo?.engines?.length ? ttsEngineInfo.engines : [{ key: 'openf5', label: 'OpenF5-TTS-Base', commercial_ok: true }]).map((e) => (
                    <option key={e.key} value={e.key}>{e.label}{e.commercial_ok ? '' : ' · non-commercial'}</option>
                  ))}
                </select>
              </Field>
              {/* Narration language — only multilingual voice models (Chatterbox) speak
                  non-English; the F5 models ignore it, so the picker hides for them. */}
              {(() => {
                const langs = (ttsEngineInfo?.engines || []).find((e) => e.key === (st.tts_engine || 'openf5'))?.languages || {}
                if (!Object.keys(langs).length) return null
                return (
                  <Field label="Narration language" hint="Which language this style's narration is spoken in — the script text itself should be written in the same language.">
                    <select className="select" value={st.tts_language || 'en'} onChange={(e) => setStyleField('tts_language', e.target.value)} style={{ maxWidth: 320 }}>
                      {Object.entries(langs).sort((a, b) => a[1].localeCompare(b[1])).map(([code, name]) => (
                        <option key={code} value={code}>{name}</option>
                      ))}
                    </select>
                  </Field>
                )
              })()}
              {/* Narration — narrator beside the robotic toggle, the dial-in sliders and test right below */}
              <div className="row gap-22 row--wrap" style={{ alignItems: 'flex-end' }}>
                <div className="grow"><Field label="Narrator voice"><select className="select" value={st.voice || ''} onChange={(e) => setStyleField('voice', e.target.value)}>
                  <option value="">(F5-TTS default)</option>
                  {(meta.voices || []).filter((v) => v !== 'Default (F5-TTS)').map((v) => <option key={v} value={v}>{voiceLabel(v, voiceMetaMap(cfg.voices))}</option>)}
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
              <VoiceTester voice={st.voice} roboticAmount={st.voice_robotic_amount} speed={st.voice_speed} engine={st.tts_engine} language={st.tts_language} onError={setError} />
            </div>
          </Card>

          {/* ── Characters (opt-in from the global library) ── */}
          <Card span={12} className="reveal reveal-d3">
            <div className="row center between">
              <span className="label-sm">Characters</span>
              <span className="muted" style={{ fontSize: 11.5 }}>define them under <strong>Characters</strong></span>
            </div>
            <div className="field__hint" style={{ marginTop: 6 }}>
              Pick which recurring characters can appear in this style. When a scene mentions one by name (or an alias), its appearance is written into the image prompt so it stays consistent across scenes and videos.
            </div>
            <div className="stack gap-10 mt-16">
              <Check checked={!!st.auto_accept_characters} onChange={(v) => setStyleField('auto_accept_characters', v)}
                label="Accept all characters automatically — every character in the library, including ones added later, can appear in this style." />
              {st.auto_accept_characters ? (
                <div className="muted" style={{ fontSize: 12 }}>All {chars.filter((c) => c.id && c.enabled !== false).length} character(s) in the library are accepted. Turn this off to pick specific ones.</div>
              ) : (<>
                {chars.length === 0 && <div className="muted" style={{ fontSize: 12 }}>No characters yet — add some under the <strong>Characters</strong> tab, then enable them here.</div>}
                {chars.filter((c) => c.id).map((c) => (
                  <Check key={c.id} checked={styleCharIds.includes(c.id)} onChange={(v) => toggleStyleChar(c.id, v)}
                    label={`${c.name || '(unnamed)'}${c.enabled === false ? ' — disabled in the library' : ''}${c.description ? ` · ${c.description.slice(0, 70)}${c.description.length > 70 ? '…' : ''}` : ''}`} />
                ))}
                {chars.some((c) => !c.id) && <div className="muted" style={{ fontSize: 12 }}>Save settings to enable newly added characters here.</div>}
              </>)}
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

          {/* ── Image model (engine) ── */}
          <Card span={6} className="reveal reveal-d3">
            <span className="label-sm">Image model</span>
            <div className="field__hint" style={{ marginTop: 6 }}>Which engine generates this style's scenes and powers “Edit image”. Download models under <strong>Infrastructure</strong>.</div>
            <div className="stack gap-22 mt-16">
              {!engineInfo && <div className="muted" style={{ fontSize: 12 }}>Loading engines…</div>}
              {engineInfo && (<>
                <Field label="Generation">
                  <select className="select" value={st.image_engine || 'flux1-schnell'} onChange={(e) => setStyleField('image_engine', e.target.value)}>
                    {(engineInfo.engines || []).filter((e) => e.can_generate).map((e) => (
                      <option key={e.key} value={e.key}>{e.label}{e.commercial_ok ? '' : ' · non-commercial'}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Edit (mask + prompt)" hint="Model used for masked 'Edit image' inpaints.">
                  <select className="select" value={st.edit_engine || 'flux1-schnell'} onChange={(e) => setStyleField('edit_engine', e.target.value)}>
                    {(engineInfo.engines || []).filter((e) => e.can_edit).map((e) => (
                      <option key={e.key} value={e.key}>{e.label}{e.commercial_ok ? '' : ' · non-commercial'}</option>
                    ))}
                  </select>
                </Field>
              </>)}
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

        {tab === 'characters' && (<>
          {/* ── Characters (global library) ── */}
          <Card span={12} className="reveal reveal-d1">
            <div className="row center between">
              <span className="label-sm">Characters</span>
              <span className="muted" style={{ fontSize: 11.5 }}>shared across styles · enable per style under <strong>Styles</strong></span>
            </div>
            <div className="field__hint" style={{ marginTop: 6 }}>
              Recurring people or things that should look the same across every scene and video. Define a character once here, then enable it on any style under <strong>Styles</strong>. When a scene mentions it by name (or an alias), its appearance is written into the image prompt so it stays consistent. A generated portrait uses the default style's image model.
            </div>
            <div className="stack gap-16 mt-16">
              {chars.length === 0 && <div className="muted" style={{ fontSize: 12 }}>No characters yet — add one to keep a subject looking the same across scenes.</div>}
              {chars.map((c, i) => (
                <div key={c.id || i} className="stack gap-12" style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 12 }}>
                  <div className="row gap-12 row--wrap" style={{ alignItems: 'flex-end' }}>
                    <div className="grow"><Field label="Name">
                      <input className="input" value={c.name || ''} placeholder="e.g. Robot XYZ"
                        onChange={(e) => updateChar(i, { name: e.target.value })} />
                    </Field></div>
                    <div className="grow"><Field label="Also known as" hint="Comma-separated aliases that also refer to this character.">
                      <input className="input" defaultValue={(c.aliases || []).join(', ')} placeholder="XYZ, the machine"
                        key={`alias-${c.id || i}`}
                        onBlur={(e) => updateChar(i, { aliases: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} />
                    </Field></div>
                  </div>
                  <Field label="Appearance" hint="Written verbatim into the image prompt — describe the look only, no name. e.g. “matte-black humanoid chassis, single cyan optical sensor, exposed brass joints”.">
                    <textarea className="textarea" rows={3} value={c.description || ''}
                      onChange={(e) => updateChar(i, { description: e.target.value })} />
                  </Field>
                  <Field label="Voice" hint="Cloned voice this character speaks with in dialogue scenes. Blank = the style's narrator voice.">
                    <select className="input" style={{ maxWidth: 260 }} value={c.voice || ''}
                      onChange={(e) => updateChar(i, { voice: e.target.value })}>
                      <option value="">Style narrator (default)</option>
                      {(cfg.voices || []).map((v) => (
                        <option key={v.name} value={v.name}>
                          {v.name}{[v.gender, v.age, v.accent].filter(Boolean).length ? ` — ${[v.gender, v.age, v.accent].filter(Boolean).join(', ')}` : ''}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <div className="row gap-12" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                    <Check checked={c.enabled !== false} onChange={(v) => updateChar(i, { enabled: v })}
                      label="Enabled — available to use in scripts and renders" />
                    <Button variant="ghost" icon="trash" onClick={() => removeChar(i)}>Remove</Button>
                  </div>
                  {/* Reference image — anchors the look to a photo/portrait (FLUX.2 only) */}
                  {c.id && !dirty ? (
                    <div className="stack gap-12">
                      <div className="row gap-12 row--wrap" style={{ alignItems: 'center' }}>
                        {c.ref_image
                          ? <div onClick={() => setCharLightbox(c.id)}
                              style={{ position: 'relative', width: 120, height: 120, flex: '0 0 auto', borderRadius: 8, overflow: 'hidden', border: '1px solid var(--border)', cursor: 'zoom-in' }}>
                              <img src={`${fileUrl(`${meta.characters_dir}/${c.id}.png`)}&v=${charBust}`} alt=""
                                style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                              <span style={{ position: 'absolute', right: 6, bottom: 6, background: 'rgba(45,51,53,.72)', color: '#fff', fontSize: 10.5, fontWeight: 600, padding: '2px 7px', borderRadius: 6, display: 'inline-flex', alignItems: 'center', gap: 4, backdropFilter: 'blur(4px)' }}>
                                <Icon name="up-right-and-down-left-from-center" /> Full size
                              </span>
                            </div>
                          : <span className="muted" style={{ fontSize: 12 }}>No reference image — text only.</span>}
                        <label className={`btn btn--ghost${charBusy === c.id ? ' btn--disabled' : ''}`}>
                          <Icon name="upload" /> Upload image
                          <input type="file" accept="image/*" style={{ display: 'none' }} disabled={charBusy === c.id}
                            onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadCharImage(c, f); e.target.value = '' }} />
                        </label>
                        <Button variant="ghost" icon="wand-magic-sparkles" disabled={charBusy === c.id || !c.description}
                          onClick={() => genCharPortrait(c)}>
                          {charBusy === c.id ? 'Working…' : (c.ref_image ? 'Re-roll portrait' : 'Generate portrait')}
                        </Button>
                        {c.ref_image && <Button variant="ghost" icon="trash" disabled={charBusy === c.id} onClick={() => clearCharImage(c)}>Remove image</Button>}
                      </div>
                      <VersionStrip versions={c.history?.versions} selected={c.history?.selected}
                        onSelect={(vid) => selectCharVersion(c, vid)} onDelete={(vid) => deleteCharVersion(c, vid)}
                        aspect="1 / 1" busy={charBusy === c.id} />
                    </div>
                  ) : (
                    <span className="muted" style={{ fontSize: 12 }}>Save settings to add a reference image that pins this character's look (FLUX.2 only).</span>
                  )}
                </div>
              ))}
              <div><Button variant="ghost" icon="plus" onClick={addChar}>Add character</Button></div>
            </div>
          </Card>

          {charLightbox && (() => {
            const c = chars.find((x) => x.id === charLightbox)
            if (!c) return null
            return (
              <ImageLightbox versions={c.history?.versions || []} start={charSelVerIdx(c)}
                fallback={c.ref_image ? `${fileUrl(`${meta.characters_dir}/${c.id}.png`)}&v=${charBust}` : ''}
                title={c.name || 'Character'} onClose={() => setCharLightbox(null)} />
            )
          })()}
        </>)}

        {tab === 'voices' && (<>
          {/* ── Voices (narrator reference clips) ── */}
          <VoicesManager voices={cfg.voices} busy={vbusy}
            ttsLanguages={(ttsEngineInfo?.engines || []).find((e) => Object.keys(e.languages || {}).length)?.languages}
            onAdd={addVoice} onUpdate={updateVoice} onDelete={deleteVoice} />
        </>)}

        {tab === 'channels' && (<>
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
          {/* ── X (Twitter) accounts (issue #107) ── */}
          <XAccountsCard onConfigChanged={reloadXAccounts} onError={setError} />
          <Card span={12} className="reveal reveal-d2">
            <span className="label-sm">X API</span>
            <div className="stack gap-22 mt-16">
              <Field label="Client ID" hint="OAuth 2.0 Client ID from the X developer portal. Register the redirect URI http://127.0.0.1:8723/callback on the app.">
                <input className="input" value={cfg.x_client_id || ''} onChange={(e) => set('x_client_id', e.target.value)} />
              </Field>
              <Field label="Client secret" hint="Only for confidential X apps. Leave blank for a public (PKCE-only) app.">
                <input className="input" type="password" placeholder={cfg.x_client_secret_set ? '•••••••• (saved — leave blank to keep)' : ''} value={cfg.x_client_secret || ''} onChange={(e) => set('x_client_secret', e.target.value)} />
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
              <Check checked={!!cfg.youtube_auto_write_scripts} onChange={(v) => set('youtube_auto_write_scripts', v)} label="Auto-write scripts for queued items but don't render — they wait unapproved for you to review, edit and approve" />
              <Check checked={!!cfg.youtube_auto_approve_script} onChange={(v) => set('youtube_auto_approve_script', v)} label="Auto-approve scripts — also write missing scripts and render them without review" />
              <Check checked={!!cfg.youtube_auto_ai_ideas} onChange={(v) => set('youtube_auto_ai_ideas', v)} label="Top up the queue with an AI idea when it runs empty (needs auto-approved scripts)" />
              <div className="row center between row--wrap gap-10" style={{ paddingLeft: 26 }}>
                <span className="muted" style={{ fontSize: 12.5 }}>Declined ideas are kept out of new AI suggestions. Clear the list to let those topics resurface — ignored ideas stay hidden.</span>
                <Button variant="ghost" icon="trash-can" disabled={clearingDeclined} onClick={resetDeclinedIdeas}>{clearingDeclined ? 'Clearing…' : 'Clear declined ideas'}</Button>
              </div>
              <Check checked={!!cfg.youtube_auto_post} disabled={!!cfg.publish_schedule_enabled} onChange={(v) => set('youtube_auto_post', v)} label="Auto-post to YouTube the moment a film finishes (off = it waits in the publish queue)" />
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
              <Check checked={!!cfg.x_auto_post} disabled={!!cfg.publish_schedule_enabled} onChange={(v) => set('x_auto_post', v)} label="Auto-post to X the moment a film finishes — uses the film's style X account; long videos fall back to the YouTube link on non-Premium" />
            </div>
          </Card>
          {/* ── Publishing schedule — release the queue on a cadence ── */}
          <Card span={12} className="reveal reveal-d3">
            <span className="label-sm">Publishing schedule</span>
            <div className="field__hint" style={{ marginTop: 6 }}>
              Finished videos always collect in the publish queue (<strong>Publishing → Schedule</strong>) — publishing one manually removes it. Turn this on to release them automatically on each channel/account's own cadence instead of the moment they finish; set the per-channel and per-account <strong>Videos per day</strong> in the YouTube and X tabs. Mutually exclusive with the “auto-post the moment a film finishes” toggles above.
            </div>
            <div className="stack gap-16 mt-16">
              <Check checked={!!cfg.publish_schedule_enabled} disabled={!!cfg.youtube_auto_post || !!cfg.x_auto_post}
                onChange={(v) => set('publish_schedule_enabled', v)}
                label="Publish on a schedule instead of the moment a film finishes" />
              <Check checked={cfg.publish_schedule_skip_comment_requests !== false} disabled={!cfg.publish_schedule_enabled}
                onChange={(v) => set('publish_schedule_skip_comment_requests', v)}
                label="Let comment-requested videos skip the schedule and post immediately" />
              <Check checked={!!cfg.publish_require_approval}
                onChange={(v) => set('publish_require_approval', v)}
                label="Require approval before publishing — finished videos are held until you approve them in the Films tab (comment-requested videos still post automatically)" />
              <Check checked={!!cfg.publish_auto_publish_unapproved} disabled={!cfg.publish_require_approval}
                onChange={(v) => set('publish_auto_publish_unapproved', v)}
                label="…but let automation publish them without waiting for approval — videos still show as unapproved, yet the scheduler/auto-post releases them on cadence (turn this off again to re-hold any not-yet-published videos)" />
            </div>
          </Card>
          {/* ── Content Credentials (C2PA) — signed AI-provenance on published videos ── */}
          <Card span={12} className="reveal reveal-d4">
            <span className="label-sm">Content Credentials (C2PA)</span>
            <div className="field__hint" style={{ marginTop: 6 }}>
              Signs every published video with tamper-evident provenance that declares it AI-generated by Stephen Spielbot. Needs <strong>c2patool</strong> installed (<strong>brew install c2patool</strong>) — it's skipped silently otherwise. With no certificate set, a local self-signed one is generated automatically (readable everywhere, but validators show “issued by an unknown source”); point to a trust-listed certificate below for a verified issuer.
            </div>
            <div className="stack gap-16 mt-16">
              <Check checked={cfg.c2pa_enabled !== false} onChange={(v) => set('c2pa_enabled', v)}
                label="Embed Content Credentials in published videos" />
              <Field label="Signing certificate path" hint="Optional — PEM certificate (chain) for a trusted issuer. Leave blank to auto-generate a local self-signed cert.">
                <input className="input" value={cfg.c2pa_cert_path || ''} disabled={cfg.c2pa_enabled === false}
                  onChange={(e) => set('c2pa_cert_path', e.target.value)} placeholder="~/.config/video-generator/c2pa/cert.pem (auto)" />
              </Field>
              <Field label="Signing key path" hint="Optional — matching ES256 private key (PKCS#8 PEM).">
                <input className="input" value={cfg.c2pa_key_path || ''} disabled={cfg.c2pa_enabled === false}
                  onChange={(e) => set('c2pa_key_path', e.target.value)} placeholder="~/.config/video-generator/c2pa/key.pem (auto)" />
              </Field>
            </div>
          </Card>
        </>)}

      </div>
    </div>
  )
}
