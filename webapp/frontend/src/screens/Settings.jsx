import { useState, useEffect, useMemo, useRef } from 'react'
import { Card, Field, Segmented, ResolutionPicker, resolutionTier, Check, Button, Banner, Chip, Icon, VersionStrip, ImageLightbox, voiceMetaMap, voiceLabel, voiceWpm, effectiveWpm, styleMinutes, lengthEstimateLabel, sceneBounds, sceneSecsFor, fmtDuration, DurationInput, LEGACY_SCENE_SECS } from '../components.jsx'
import { api, fileUrl } from '../api.js'
import SettingsAssets from './SettingsAssets.jsx'
import { resolveStyle, styleLineage, styleTreeOrder, STYLE_TEXT_FIELDS, AUTOMATION_FIELDS,
  globalAutomation, resolveAutomation, automationSource } from '../styleUtils.js'

// ── Character turnaround sheet ───────────────────────────────────────────────
// Several views of one character in one strip. The engine is picked HERE, per
// generation, because the two trade off against each other: the image model
// paints all four panels in seconds with a clean layout but only a likeness of
// the face, while the camera orbit takes minutes and holds the real face (and
// the real back of the head) through the turn. An orbit keeps its clip, so its
// panels are frames the user can move — nobody has to accept the automatic pick.
const SHEET_ENGINES = [
  { key: 'image', label: 'Image model', hint: 'Seconds. Clean four-panel layout; the face is a likeness, and the back view is invented.' },
  { key: 'orbit', label: 'Camera orbit', hint: 'Minutes on a worker. Films the character turning, so the face and the back of the head are really theirs — pick the frames afterwards.' },
]

function SheetPanelPicker({ clipUrl, duration, panels, busy, onApply, onCancel }) {
  // One <video> per panel, each seeked to its own timestamp: the browser draws
  // the frame, so scrubbing costs nothing and the preview is exactly the frame
  // the backend will cut. Only Apply goes to the server.
  const [times, setTimes] = useState(() => (panels?.length ? panels : [0]).map(Number))
  const refs = useRef([])
  useEffect(() => {
    times.forEach((t, i) => {
      const v = refs.current[i]
      if (v && Number.isFinite(t) && Math.abs(v.currentTime - t) > 0.01) v.currentTime = t
    })
  }, [times])
  const setAt = (i, t) => setTimes(times.map((old, j) => (j === i ? t : old)))
  const addPanel = () => setTimes([...times, Math.min(duration || 0, (times[times.length - 1] || 0) + 0.5)])
  const dropPanel = (i) => setTimes(times.filter((_, j) => j !== i))
  const max = Math.max(0.1, (duration || 0) - 0.05)
  return (
    <div className="stack gap-12">
      <span className="muted" style={{ fontSize: 12 }}>
        Drag each panel to the frame you want — the sheet is stitched left to right in this order. No re-render: these are frames of the orbit already filmed.
      </span>
      <div className="row gap-8 row--wrap">
        {times.map((t, i) => (
          <div key={i} style={{ width: 168 }}>
            <video ref={(el) => { refs.current[i] = el }} src={clipUrl} preload="auto" muted playsInline
              style={{ width: '100%', borderRadius: 8, border: '1px solid var(--border)', background: '#000' }}
              onLoadedMetadata={(e) => { e.currentTarget.currentTime = t }} />
            <input type="range" min={0} max={max} step={0.02} value={Math.min(t, max)} disabled={busy}
              style={{ width: '100%' }} onChange={(e) => setAt(i, Number(e.target.value))} />
            <div className="row gap-8" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="muted" style={{ fontSize: 11 }}>Panel {i + 1} · {t.toFixed(2)}s</span>
              {times.length > 1 && <a role="button" tabIndex={0} className="muted" style={{ fontSize: 11 }}
                onClick={() => dropPanel(i)}>Remove</a>}
            </div>
          </div>
        ))}
      </div>
      <div className="row gap-8">
        <Button variant="primary" icon="check" disabled={busy} onClick={() => onApply(times)}>
          {busy ? 'Stitching…' : 'Use these frames'}
        </Button>
        {times.length < 8 && <Button variant="ghost" icon="plus" disabled={busy} onClick={addPanel}>Add panel</Button>}
        <Button variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  )
}

function CharacterSheet({ char, initial, disabled, disabledNote, onLightbox }) {
  const [sheet, setSheet] = useState(initial || { status: 'none' })
  const [engine, setEngine] = useState('image')
  const [busy, setBusy] = useState(false)
  const [picking, setPicking] = useState(false)
  const [err, setErr] = useState('')
  const rendering = sheet.status === 'rendering'
  // A render outlives the request that started it, so poll until it lands.
  useEffect(() => {
    if (!rendering) return undefined
    const id = setInterval(async () => {
      try { setSheet((await api.characterSheet(char.id)).sheet) } catch { /* transient */ }
    }, 4000)
    return () => clearInterval(id)
  }, [rendering, char.id])

  const run = async (fn) => {
    setBusy(true); setErr('')
    try { setSheet((await fn()).sheet) } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  const build = () => run(() => api.buildCharacterSheet(char.id, engine, ''))
  const apply = (times) => run(async () => {
    const r = await api.pickCharacterSheetPanels(char.id, times)
    setPicking(false)
    return r
  })

  return (
    <div className="stack gap-8" style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
      <div className="row gap-8" style={{ alignItems: 'center', justifyContent: 'space-between' }}>
        <span className="label-sm">Turnaround sheet</span>
        {sheet.status === 'ready' && <span className="muted" style={{ fontSize: 11 }}>
          {sheet.engine === 'orbit' ? 'From a camera orbit' : 'From the image model'}
        </span>}
      </div>
      {disabled
        ? <span className="muted" style={{ fontSize: 12 }}><Icon name="circle-info" /> {disabledNote}</span>
        : (<>
          {sheet.sheet_url && !rendering && (
            <img src={sheet.sheet_url} alt="" onClick={() => onLightbox(sheet.sheet_url)}
              style={{ width: '100%', borderRadius: 8, border: '1px solid var(--border)', cursor: 'zoom-in' }} />
          )}
          {rendering && <span className="muted" style={{ fontSize: 12 }}>
            <Icon name="spinner" /> Building the sheet{sheet.engine === 'orbit' ? ' — a camera orbit takes a few minutes on a worker' : '…'}
          </span>}
          {sheet.status === 'error' && sheet.error && <Banner kind="error">{sheet.error}</Banner>}
          {err && <Banner kind="error">{err}</Banner>}
          {picking
            ? <SheetPanelPicker clipUrl={sheet.clip_url} duration={sheet.duration} panels={sheet.panels}
                busy={busy} onApply={apply} onCancel={() => setPicking(false)} />
            : (
              <div className="row gap-8 row--wrap" style={{ alignItems: 'center' }}>
                <select className="input" style={{ width: 150 }} value={engine} disabled={busy || rendering}
                  onChange={(e) => setEngine(e.target.value)}>
                  {SHEET_ENGINES.map((e) => <option key={e.key} value={e.key}>{e.label}</option>)}
                </select>
                <Button variant="ghost" icon="wand-magic-sparkles" disabled={busy || rendering || !char.ref_image}
                  onClick={build}>{sheet.status === 'ready' ? 'Build again' : 'Build sheet'}</Button>
                {sheet.has_clip && sheet.status === 'ready' && !rendering &&
                  <Button variant="ghost" icon="film" disabled={busy} onClick={() => setPicking(true)}>Adjust frames</Button>}
                {sheet.status === 'ready' && !rendering &&
                  <Button variant="ghost" icon="trash" disabled={busy}
                    onClick={() => run(() => api.clearCharacterSheet(char.id))}>Remove sheet</Button>}
              </div>
            )}
          <span className="muted" style={{ fontSize: 11.5 }}>
            {char.ref_image
              ? SHEET_ENGINES.find((e) => e.key === engine)?.hint
              : 'Add a reference image first — the sheet is built from it.'}
          </span>
        </>)}
    </div>
  )
}

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
  'youtube_auto_critic',
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

// Publishing-clock row for one channel/account settings panel. The cadence has
// no fixed time of day — it spaces releases from whenever the last one went out,
// so YouTube and X drift onto different timings. Resetting re-anchors the clock:
// releases before the reset stop counting, the next one is allowed at the picked
// time (empty = right away) and later ones space from it. Setting the same time
// on a YouTube channel and an X account brings the two platforms in sync.
function PublishClockControl({ platform, id, onError }) {
  const [info, setInfo] = useState(null)   // this key's /publish/clock summary
  const [at, setAt] = useState('')         // datetime-local value; '' = right away
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const refresh = () => api.publishClock()
    .then((r) => setInfo(((platform === 'youtube' ? r.channels : r.accounts) || {})[id] || {}))
    .catch(() => setInfo({}))
  useEffect(() => { refresh() }, [id])
  const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) : '—'
  const reset = async () => {
    setBusy(true); setNote(''); onError('')
    try {
      const ts = at ? Math.round(new Date(at).getTime() / 1000) : 0
      if (at && !(ts > 0)) throw new Error('Pick a valid date and time, or leave it empty for “right away”.')
      await api.publishClockReset(platform, id, ts)
      await refresh()
      setAt('')
      setNote(ts ? 'Clock re-anchored — the next release is allowed at the chosen time.' : 'Clock reset — the next release can go out right away.')
    } catch (e) { onError(e.message) } finally { setBusy(false) }
  }
  const other = platform === 'youtube' ? 'an X account' : 'a YouTube channel'
  return (
    <Field label="Publishing clock"
      hint={`The cadence spaces releases from the last one, so release times drift. Resetting re-anchors the clock: the next release is allowed at the time you pick (leave empty for right away) and later ones space from it. Set the same time here and on ${other} to sync the two platforms.`}>
      <div className="stack gap-8">
        <span className="muted" style={{ fontSize: 12.5 }}>
          {info === null ? 'Checking…' : <>
            Last release {fmt(info.last_released)}{info.reset_pending ? ' (clock reset)' : ''} · next allowed{' '}
            <strong>{info.next_eligible && info.next_eligible > Date.now() / 1000 + 30 ? fmt(info.next_eligible) : 'now'}</strong>
          </>}
        </span>
        <div className="row gap-10 row--wrap" style={{ alignItems: 'center' }}>
          <input className="input" type="datetime-local" style={{ width: 210 }} value={at}
            onChange={(e) => setAt(e.target.value)} />
          <Button variant="ghost" icon="clock-rotate-left" disabled={busy} onClick={reset}>
            {busy ? 'Resetting…' : 'Reset clock'}
          </Button>
        </div>
        {note && <span className="muted" style={{ fontSize: 12 }}>{note}</span>}
      </div>
    </Field>
  )
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

// Audition the style's narrator voice at the current robotic level and cadence.
// F5-TTS runs on the backend (a few seconds for one sentence), so show a
// "Generating…" state while waiting. Reads the live, unsaved narrator-voice,
// robotic-level and cadence fields so you can dial them in by ear before
// saving. Deliberately no voice picker of its own: a second dropdown here
// looked like the style's voice setting but silently saved nothing.
function VoiceTester({ voice, roboticAmount, cadenceWpm, engine, language, sentencePause, onError }) {
  const [busy, setBusy] = useState(false)
  const [text, setText] = useState('')
  const audioRef = useRef(null)

  const play = async () => {
    onError(''); setBusy(true)
    try {
      const r = await api.testVoice({ voice: voice || '', robotic_amount: roboticAmount ?? 0, cadence_wpm: cadenceWpm ?? 0, engine: engine || '', language: language || '', text: text.trim(), sentence_pause: sentencePause ?? null })
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
      hint={`Plays the narrator voice chosen above — “This is the voice of ${spoken}. What do you think?” at the robotic level, cadence and sentence pause (cached after the first time). Type a custom line to audition a respelling or [pause:1.5] markers.`}>
      <div className="row center gap-10 row--wrap">
        <Button variant="primary" icon="play" disabled={busy} onClick={play}>{busy ? 'Generating…' : 'Play'}</Button>
        <input className="input grow" placeholder="Custom line to speak (optional) — e.g. The lead pipes burst. [pause] Something still lives."
          value={text} onChange={(e) => setText(e.target.value)} />
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

function VoicesManager({ voices, busy, ttsLanguages, cadences: cadencesProp, onAdd, onUpdate, onDelete, onError }) {
  const [name, setName] = useState('')
  const [file, setFile] = useState(null)
  const [addMeta, setAddMeta] = useState({})
  const [editing, setEditing] = useState(null)   // name of the voice being edited
  const [editName, setEditName] = useState('')
  const [editFile, setEditFile] = useState(null)
  const [editMeta, setEditMeta] = useState({})
  const [recOpen, setRecOpen] = useState(false)
  const addRef = useRef(null)
  // Measured cadence store ("<voice>|<engine>" → {wpm, samples}); calibration
  // responses refresh it without waiting for a full config refetch.
  const [cadences, setCadences] = useState(cadencesProp || {})
  useEffect(() => { setCadences(cadencesProp || {}) }, [cadencesProp])
  const [calibrating, setCalibrating] = useState('')

  // Best measurement for a voice across engines (most samples wins).
  const wpmOf = (voiceName) => {
    let best = null
    for (const [k, e] of Object.entries(cadences || {})) {
      if (k.split('|')[0] === (voiceName || '__default__') && Number(e?.wpm) > 0) {
        if (!best || (e.samples || 0) > (best.samples || 0)) best = e
      }
    }
    return best
  }
  const calibrate = async (voiceName) => {
    setCalibrating(voiceName || '__default__')
    try {
      const r = await api.calibrateVoice(voiceName)
      if (r.voice_cadences) setCadences(r.voice_cadences)
    } catch (e) { onError && onError(e.message) } finally { setCalibrating('') }
  }
  const calibrateAll = async () => {
    setCalibrating('*')
    try {
      for (const v of voices || []) {
        const r = await api.calibrateVoice(v.name)
        if (r.voice_cadences) setCadences(r.voice_cadences)
      }
    } catch (e) { onError && onError(e.message) } finally { setCalibrating('') }
  }

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
        <div className="row center gap-10">
          <Button variant="ghost" icon="gauge-high" disabled={busy || !!calibrating}
            onClick={calibrateAll}>{calibrating === '*' ? 'Calibrating…' : 'Calibrate all cadences'}</Button>
          <span className="muted" style={{ fontSize: 11.5 }}>changes save immediately</span>
        </div>
      </div>
      <div className="field__hint" style={{ marginTop: 6 }}>
        Reference clips (15–30s of clear speech) F5-TTS clones for narration and dialogue. Pick each
        style's narrator voice under <strong>Styles</strong>. <strong>Gender + age</strong> let story
        characters be auto-cast with a fitting voice — a voice without a gender is never auto-cast.
        Each voice's <strong>cadence</strong> (words/minute) sizes scripts to a video length; it is
        measured by Calibrate (a one-off synthesis) and keeps refining from every real narration.
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
                {(() => { const c = wpmOf(v.name); return c
                  ? <span className="muted" style={{ fontSize: 12 }}>{Math.round(c.wpm)} wpm</span>
                  : <span className="muted" style={{ fontSize: 12, opacity: 0.6 }}>cadence unmeasured</span> })()}
                <div className="grow" />
                <Button variant="ghost" icon="gauge-high" disabled={busy || !!calibrating}
                  onClick={() => calibrate(v.name)}>{calibrating === v.name ? 'Measuring…' : 'Calibrate'}</Button>
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

// Per-style cover typography — defaults mirrored from pipeline/cover_typography.py
// so the editor shows effective values before the style has its own dict.
const CT_DEFAULTS = {
  font: 'Anton', position: 'bottom', align: 'center', case: 'upper',
  width_pct: 82, scale: 1.0, color: '#FFFFFF', accent: 'last_word',
  accent_color: '#FFD400', accent_scale: 1.15, outline: true,
  card: false, card_color: '#000000', card_opacity: 0.55,
}

// Editor for a style's cover_typography: controls beside a live preview
// rendered server-side by the exact code that composites real covers, so
// what you see here is what gets burned onto thumbnails.
function CoverTypographyEditor({ value, onChange, systemFonts, bundledFonts }) {
  const ct = { ...CT_DEFAULTS, ...(value || {}) }
  const set = (k, v) => onChange({ ...ct, [k]: v })
  const [sample, setSample] = useState('The Secret Life of Deep Sea Giants')
  const [portrait, setPortrait] = useState(false)
  const [preview, setPreview] = useState('')
  const [previewErr, setPreviewErr] = useState('')
  const ctKey = JSON.stringify(ct)
  useEffect(() => {
    let gone = false
    const t = setTimeout(() => {
      fetch('/api/cover-typography/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cover_typography: ct, text: sample, orientation: portrait ? 'portrait' : 'landscape' }),
      }).then((res) => {
        if (!res.ok) throw new Error(`Preview failed (${res.status})`)
        return res.blob()
      }).then((blob) => {
        if (gone) return
        setPreview((old) => { if (old) URL.revokeObjectURL(old); return URL.createObjectURL(blob) })
        setPreviewErr('')
      }).catch((e) => { if (!gone) setPreviewErr(e.message) })
    }, 300)
    return () => { gone = true; clearTimeout(t) }
  }, [ctKey, sample, portrait])  // eslint-disable-line react-hooks/exhaustive-deps
  const fontKnown = (bundledFonts || []).some((f) => f.name === ct.font)
    || (systemFonts || []).some((f) => f.path === ct.font)
  return (
    <div className="stack gap-16" style={{ border: '1px solid var(--line)', borderRadius: 'var(--r-md)', padding: 16 }}>
      <div className="row gap-22 row--wrap" style={{ alignItems: 'flex-start' }}>
          <div className="stack gap-14" style={{ flex: '1 1 340px', minWidth: 300 }}>
            <Field label="Font" hint="Bundled fonts ship with Spielbot (thumbnail-grade display faces); system fonts come from this machine.">
              <select className="select" value={ct.font || ''} onChange={(e) => set('font', e.target.value)} style={{ maxWidth: 320 }}>
                {(bundledFonts || []).length > 0 && (
                  <optgroup label="Bundled">
                    {(bundledFonts || []).map((f) => <option key={f.path} value={f.name}>{f.name}</option>)}
                  </optgroup>
                )}
                <optgroup label="System">
                  {(systemFonts || []).map((f) => <option key={f.path} value={f.path}>{f.name}</option>)}
                </optgroup>
                {ct.font && !fontKnown && <option value={ct.font}>{ct.font} (not found)</option>}
              </select>
            </Field>
            <div className="row gap-14 row--wrap">
              <Field label="Position">
                <select className="select" value={ct.position} onChange={(e) => set('position', e.target.value)}>
                  <option value="top">Top</option>
                  <option value="middle">Middle</option>
                  <option value="bottom">Bottom</option>
                </select>
              </Field>
              <Field label="Alignment">
                <select className="select" value={ct.align} onChange={(e) => set('align', e.target.value)}>
                  <option value="left">Left</option>
                  <option value="center">Centre</option>
                  <option value="right">Right</option>
                </select>
              </Field>
              <Field label="Case">
                <select className="select" value={ct.case} onChange={(e) => set('case', e.target.value)}>
                  <option value="upper">UPPERCASE</option>
                  <option value="title">Title Case</option>
                  <option value="keep">As typed</option>
                </select>
              </Field>
            </div>
            <div className="row gap-14 row--wrap">
              <Field label="Text size" hint="Multiplier on the auto-fitted size.">
                <input className="input" type="number" min={0.5} max={1.6} step={0.05} value={ct.scale}
                  onChange={(e) => set('scale', +e.target.value)} style={{ maxWidth: 100 }} />
              </Field>
              <Field label="Block width" hint="% of the image the text may fill.">
                <input className="input" type="number" min={40} max={96} value={ct.width_pct}
                  onChange={(e) => set('width_pct', +e.target.value)} style={{ maxWidth: 100 }} />
              </Field>
              <Field label="Text colour">
                <input className="input" type="color" value={ct.color}
                  onChange={(e) => set('color', e.target.value)} style={{ maxWidth: 90, height: 38, padding: 4, cursor: 'pointer' }} />
              </Field>
            </div>
            <div className="row gap-14 row--wrap">
              <Field label="Accent words" hint="Which word of a new film's title-derived cover phrase is wrapped in *asterisks* (the accent colour and size). Only marked words are accented — edit the film's phrase to move or remove them.">
                <select className="select" value={ct.accent} onChange={(e) => set('accent', e.target.value)}>
                  <option value="none">None</option>
                  <option value="first_word">First word</option>
                  <option value="last_word">Last word</option>
                  <option value="longest_word">Longest word</option>
                </select>
              </Field>
              <Field label="Accent colour">
                <input className="input" type="color" value={ct.accent_color}
                  onChange={(e) => set('accent_color', e.target.value)} style={{ maxWidth: 90, height: 38, padding: 4, cursor: 'pointer' }} />
              </Field>
              <Field label="Accent size" hint="Accented words relative to the rest.">
                <input className="input" type="number" min={1} max={1.8} step={0.05} value={ct.accent_scale}
                  onChange={(e) => set('accent_scale', +e.target.value)} style={{ maxWidth: 100 }} />
              </Field>
            </div>
            <Check checked={!!ct.outline} onChange={(v) => set('outline', v)}
              label="Outline + soft shadow — keeps the title readable on any artwork" />
            <Check checked={!!ct.card} onChange={(v) => set('card', v)}
              label="Backdrop card — rounded panel behind the text" />
            {ct.card && (
              <div className="row gap-14 row--wrap">
                <Field label="Card colour">
                  <input className="input" type="color" value={ct.card_color}
                    onChange={(e) => set('card_color', e.target.value)} style={{ maxWidth: 90, height: 38, padding: 4, cursor: 'pointer' }} />
                </Field>
                <Field label="Card opacity">
                  <input className="input" type="number" min={0.05} max={0.95} step={0.05} value={ct.card_opacity}
                    onChange={(e) => set('card_opacity', +e.target.value)} style={{ maxWidth: 100 }} />
                </Field>
              </div>
            )}
          </div>
          <div className="stack gap-8" style={{ flex: '0 1 380px', minWidth: 260 }}>
            <Field label="Preview text" hint="Sample only — real covers use each film's phrase. Without *asterisks* the accent rule marks a word; Enter forces a line break.">
              <textarea className="textarea" rows={2} value={sample} maxLength={80} onChange={(e) => setSample(e.target.value)} />
            </Field>
            <Check checked={portrait} onChange={setPortrait} label="Portrait (Shorts)" />
            <div style={{ borderRadius: 'var(--r-md)', overflow: 'hidden', background: '#15171a', alignSelf: portrait ? 'center' : 'stretch', width: portrait ? 236 : undefined, aspectRatio: portrait ? '9 / 16' : '16 / 9' }}>
              {preview && <img src={preview} alt="Cover typography preview"
                style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }} />}
            </div>
            {previewErr && <div style={{ fontSize: 12, color: 'var(--danger)' }}>{previewErr}</div>}
          </div>
      </div>
    </div>
  )
}

// Per-style look of burned-in subtitles — defaults mirrored from
// pipeline/subtitle_style.py so the editor shows effective values before the
// style has its own dict.
const SS_DEFAULTS = {
  font: '', scale: 1.0, bold: false, color: '#FFFFFF', position: 'bottom',
  align: 'center', outline: 1.0, outline_color: '#000000', shadow: false,
  card: false, card_color: '#000000', card_opacity: 0.55, margin: 4,
  min_seconds: 2.5, delay: 0,
}

// Editor for a style's subtitle_style: controls beside a live preview frame
// drawn by the very ffmpeg filter that burns real films.
function SubtitleStyleEditor({ value, onChange, systemFonts, bundledFonts }) {
  const st = { ...SS_DEFAULTS, ...(value || {}) }
  const set = (k, v) => onChange({ ...st, [k]: v })
  const [sample, setSample] = useState('The deep sea hides giants\nnobody has ever filmed.')
  const [portrait, setPortrait] = useState(false)
  const [preview, setPreview] = useState('')
  const [previewErr, setPreviewErr] = useState('')
  const stKey = JSON.stringify(st)
  useEffect(() => {
    let gone = false
    const t = setTimeout(() => {
      fetch('/api/subtitle-style/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subtitle_style: st, text: sample, orientation: portrait ? 'portrait' : 'landscape' }),
      }).then((res) => {
        if (!res.ok) throw new Error(`Preview failed (${res.status})`)
        return res.blob()
      }).then((blob) => {
        if (gone) return
        setPreview((old) => { if (old) URL.revokeObjectURL(old); return URL.createObjectURL(blob) })
        setPreviewErr('')
      }).catch((e) => { if (!gone) setPreviewErr(e.message) })
    }, 300)
    return () => { gone = true; clearTimeout(t) }
  }, [stKey, sample, portrait])  // eslint-disable-line react-hooks/exhaustive-deps
  const fontKnown = !st.font || (bundledFonts || []).some((f) => f.name === st.font)
    || (systemFonts || []).some((f) => f.path === st.font)
  return (
    <div className="stack gap-16" style={{ border: '1px solid var(--line)', borderRadius: 'var(--r-md)', padding: 16 }}>
      <div className="row gap-22 row--wrap" style={{ alignItems: 'flex-start' }}>
          <div className="stack gap-14" style={{ flex: '1 1 340px', minWidth: 300 }}>
            <Field label="Font" hint="Bundled fonts ship with Spielbot; system fonts come from this machine. The default is the player-style sans that ffmpeg uses on its own.">
              <select className="select" value={st.font || ''} onChange={(e) => set('font', e.target.value)} style={{ maxWidth: 320 }}>
                <option value="">Default (Arial / sans)</option>
                {(bundledFonts || []).length > 0 && (
                  <optgroup label="Bundled">
                    {(bundledFonts || []).map((f) => <option key={f.path} value={f.name}>{f.name}</option>)}
                  </optgroup>
                )}
                <optgroup label="System">
                  {(systemFonts || []).map((f) => <option key={f.path} value={f.path}>{f.name}</option>)}
                </optgroup>
                {st.font && !fontKnown && <option value={st.font}>{st.font} (not found)</option>}
              </select>
            </Field>
            <div className="row gap-14 row--wrap">
              <Field label="Position">
                <select className="select" value={st.position} onChange={(e) => set('position', e.target.value)}>
                  <option value="top">Top</option>
                  <option value="middle">Middle</option>
                  <option value="bottom">Bottom</option>
                </select>
              </Field>
              <Field label="Alignment">
                <select className="select" value={st.align} onChange={(e) => set('align', e.target.value)}>
                  <option value="left">Left</option>
                  <option value="center">Centre</option>
                  <option value="right">Right</option>
                </select>
              </Field>
              <Field label="Edge margin" hint="% of the picture height.">
                <input className="input" type="number" min={0} max={40} value={st.margin}
                  onChange={(e) => set('margin', +e.target.value)} style={{ maxWidth: 100 }} />
              </Field>
            </div>
            <div className="row gap-14 row--wrap">
              <Field label="Text size" hint="Multiplier on the standard subtitle size.">
                <input className="input" type="number" min={0.5} max={2.5} step={0.05} value={st.scale}
                  onChange={(e) => set('scale', +e.target.value)} style={{ maxWidth: 100 }} />
              </Field>
              <Field label="Text colour">
                <input className="input" type="color" value={st.color}
                  onChange={(e) => set('color', e.target.value)} style={{ maxWidth: 90, height: 38, padding: 4, cursor: 'pointer' }} />
              </Field>
              <Field label="Outline width" hint="0 = no stroke.">
                <input className="input" type="number" min={0} max={4} step={0.5} value={st.outline}
                  onChange={(e) => set('outline', +e.target.value)} style={{ maxWidth: 100 }} />
              </Field>
              <Field label="Outline colour">
                <input className="input" type="color" value={st.outline_color}
                  onChange={(e) => set('outline_color', e.target.value)} style={{ maxWidth: 90, height: 38, padding: 4, cursor: 'pointer' }} />
              </Field>
            </div>
            <div className="row gap-14 row--wrap">
              <Field label="Minimum on screen" hint="Seconds. A shorter line joins the next one as a two-line caption, or is held longer. 0 = exactly as spoken or sung.">
                <input className="input" type="number" min={0} max={10} step={0.5} value={st.min_seconds}
                  onChange={(e) => set('min_seconds', +e.target.value)} style={{ maxWidth: 100 }} />
              </Field>
              <Field label="Delay" hint="Seconds to shift every caption later (negative = earlier) when the track reads early or late.">
                <input className="input" type="number" min={-5} max={5} step={0.1} value={st.delay}
                  onChange={(e) => set('delay', +e.target.value)} style={{ maxWidth: 100 }} />
              </Field>
            </div>
            <Check checked={!!st.bold} onChange={(v) => set('bold', v)} label="Bold" />
            <Check checked={!!st.shadow} onChange={(v) => set('shadow', v)} label="Drop shadow under the text" />
            <Check checked={!!st.card} onChange={(v) => set('card', v)}
              label="Backdrop box — solid panel behind each line" />
            {st.card && (
              <div className="row gap-14 row--wrap">
                <Field label="Box colour">
                  <input className="input" type="color" value={st.card_color}
                    onChange={(e) => set('card_color', e.target.value)} style={{ maxWidth: 90, height: 38, padding: 4, cursor: 'pointer' }} />
                </Field>
                <Field label="Box opacity">
                  <input className="input" type="number" min={0.05} max={1} step={0.05} value={st.card_opacity}
                    onChange={(e) => set('card_opacity', +e.target.value)} style={{ maxWidth: 100 }} />
                </Field>
              </div>
            )}
          </div>
          <div className="stack gap-8" style={{ flex: '0 1 380px', minWidth: 260 }}>
            <Field label="Preview text" hint="Sample only — real films show their own captions.">
              <textarea className="input" rows={2} value={sample} maxLength={200} onChange={(e) => setSample(e.target.value)} />
            </Field>
            <Check checked={portrait} onChange={setPortrait} label="Portrait (Shorts)" />
            <div style={{ borderRadius: 'var(--r-md)', overflow: 'hidden', background: '#15171a', alignSelf: portrait ? 'center' : 'stretch', width: portrait ? 236 : undefined, aspectRatio: portrait ? '9 / 16' : '16 / 9' }}>
              {preview && <img src={preview} alt="Subtitle style preview"
                style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }} />}
            </div>
            {previewErr && <div style={{ fontSize: 12, color: 'var(--danger)' }}>{previewErr}</div>}
          </div>
      </div>
    </div>
  )
}

const TABS = [
  { id: 'infra', label: 'Infrastructure' },
  { id: 'styles', label: 'Styles' },
  { id: 'characters', label: 'Characters' },
  { id: 'assets', label: 'Assets' },
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
                <PublishClockControl platform="youtube" id={ch.id} onError={onError} />
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
                <PublishClockControl platform="x" id={acc.id} onError={onError} />
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
  const [sheetLightbox, setSheetLightbox] = useState(null)  // turnaround sheet being viewed full-res
  const [charScope, setCharScope] = useState('')          // Characters tab: selected home ('' = Global)
  const [autoScope, setAutoScope] = useState('')          // Automation tab: selected scope ('' = Global)
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
  const [fontInfo, setFontInfo] = useState(null)  // [{path, name}] — host fonts for cover text
  const [fontBundled, setFontBundled] = useState([])  // [{path, name}] — fonts shipped in assets/fonts
  const [engInstall, setEngInstall] = useState({})    // engine key -> install status payload
  const [tab, setTab] = useState('infra')
  const [styleIdx, setStyleIdx] = useState(0)  // selected style in the Styles tab
  const [playlists, setPlaylists] = useState({})  // channel key -> {loading, items:[{id,title}], error}
  const [newOpen, setNewOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [newChild, setNewChild] = useState(false)

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

  // Fonts installed on this machine (+ bundled), for the cover-text pickers.
  useEffect(() => {
    api.listFonts().then((r) => { setFontInfo(r.fonts || []); setFontBundled(r.bundled || []) }).catch(() => {})
  }, [])

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
  // Style hierarchy: a style with a `parent` stores only its overrides and
  // inherits the rest. All VALUE reads below go through `eff` (the resolved
  // settings); writes still go to the style itself, becoming overrides.
  const eff = useMemo(() => resolveStyle(styles, st.name) || {}, [styles, st.name])
  const parentMissing = !!st.parent && !styles.some((s) => s.name === st.parent)
  // Transitive descendants of the selected style — excluded from the parent
  // picker so the hierarchy can never loop.
  const stDescendants = useMemo(() => {
    const kids = new Set()
    let grew = true
    while (grew) {
      grew = false
      for (const s of styles) {
        if (!s.name || !s.parent || kids.has(s.name)) continue
        if (s.parent === st.name || kids.has(s.parent)) { kids.add(s.name); grew = true }
      }
    }
    return kids
  }, [styles, st.name])
  // The parent's own resolved settings — what "{parent}" expands to.
  const parentEff = useMemo(() => (st.parent ? resolveStyle(styles, st.parent) : null),
    [styles, st.parent])
  // TEXT controls on a child show the literal stored text; an inherited field
  // (nothing stored) displays as the bare "{parent}" placeholder. Editing
  // around the placeholder extends the parent's text, replacing it overrides,
  // typing "{parent}" again re-inherits (setStyleField drops the key).
  const fieldVal = (k) => {
    if (st.parent && !parentMissing) return st[k] !== undefined ? (st[k] ?? '') : '{parent}'
    return st.parent && st[k] !== undefined ? (st[k] ?? '') : (eff[k] ?? '')
  }
  // Under each text field of a child style: what the box's text resolves to.
  const ParentPreview = ({ k }) => {
    if (!st.parent || parentMissing) return null
    const raw = String(fieldVal(k))
    const pv = String(parentEff?.[k] ?? '')
    const short = (t) => (t.length > 160 ? `${t.slice(0, 160)}…` : t)
    if (raw === '{parent}') {
      return (
        <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          Uses “{st.parent}”’s text{pv ? <>: <em>{short(pv)}</em></> : ' (blank)'} — add around the placeholder to extend it, or replace it to override.
        </div>
      )
    }
    if (raw.includes('{parent}')) {
      return (
        <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          Final — with “{st.parent}”’s text filled in: <em>{String(eff[k] ?? '') || '(empty)'}</em>
        </div>
      )
    }
    return (
      <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
        Replaces “{st.parent}”’s text{pv ? <> (<em>{short(pv)}</em>)</> : ''} — write <code>{'{parent}'}</code> where its text should stay.
      </div>
    )
  }
  // Human-readable rendering of a parent's value for the per-field hints below.
  const fmtParentVal = (k, v) => {
    switch (k) {
      case 'channel': {
        const c = (cfg.youtube_channels || []).find((x) => x.id === v)
        return c ? (c.name || c.id) : (v || '(first connected channel)')
      }
      case 'x_account': {
        const a = (cfg.x_accounts || []).find((x) => x.id === v)
        return a ? (a.name ? `@${a.name}` : a.id) : (v || '(don’t post to X)')
      }
      case 'youtube_playlist_id':
        return v === '__auto__' ? 'auto-create playlist' : (v || '(no playlist)')
      case 'image_engine':
      case 'edit_engine':
        return (engineInfo?.engines || []).find((e) => e.key === v)?.label || String(v || '')
      case 'video_engine':
        return (engineInfo?.video_engines || []).find((e) => e.key === v)?.label || String(v || '')
      case 'music_engine':
        return (engineInfo?.music_engines || []).find((e) => e.key === v)?.label || String(v || '')
      case 'tts_engine':
        return (ttsEngineInfo?.engines || []).find((e) => e.key === v)?.label || String(v || '')
      case 'reference_engine':
        return (engineInfo?.reference_engines || []).find((e) => e.key === v)?.label || String(v || '')
      case 'first_frame_cover':
        return v === 'image' || v === 'text' ? 'Cover image' : 'off'
      case 'first_frame_cover_seconds':
        return `${v ?? 1}s`
      case 'cover_typography': {
        const t = { ...CT_DEFAULTS, ...(v || {}) }
        return `${t.font} · ${t.position} · accent ${String(t.accent).replace('_', ' ')}`
      }
      case 'subtitle_style': {
        const t = { ...SS_DEFAULTS, ...(v || {}) }
        return `${t.font || 'default font'} · ${t.position} · ×${t.scale}`
      }
      case 'voice':
        return v || '(F5-TTS default)'
      case 'voice_cadence_wpm':
        return Number(v) > 0 ? `${v} words/min` : 'natural pace'
      case 'video_minutes':
        return Number(v) > 0 ? fmtDuration(v) : '(from legacy scene count)'
      case 'video_scenes':
        return Number(v) > 0 ? `${v} scenes` : 'automatic'
      case 'size_presets':
        return ['small', 'medium', 'large'].map((b) => {
          const p = (v || {})[b] || {}
          const mins = p.minutes || (p.scenes ? p.scenes * LEGACY_SCENE_SECS / 60 : 0)
          return `${b}: ${mins ? fmtDuration(mins) : '?'} · ${p.resolution || '?'}`
        }).join('  ·  ')
      default:
        if (typeof v === 'boolean') return v ? 'on' : 'off'
        return v === '' || v == null ? '(blank)' : String(v)
    }
  }
  // Per-field inheritance hint for non-text settings on a child style: shows
  // the parent's value. While in sync nothing is recorded (the field follows
  // the parent live); once diverged, the recorded value wins — clicking the
  // parent's value drops it and follows the parent again.
  const ParentVal = ({ k }) => {
    if (!st.parent || parentMissing) return null
    if (st[k] === undefined) {
      return <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Follows “{st.parent}”.</div>
    }
    return (
      <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
        “{st.parent}”: <a role="button" tabIndex={0} title={`Use “${st.parent}”’s value again`}
          style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
          onClick={() => clearStyleField(k)}><em>{fmtParentVal(k, parentEff?.[k])}</em></a> — click to use it.
      </div>
    )
  }

  // Playlists for the active style's channel (per-style playlist picker below).
  // Effective channel mirrors the backend's channel_for_style fallback (own
  // channel, else first connected) so the list matches the real upload target.
  const stChannel = eff.channel || (cfg.youtube_channels?.[0]?.id || '')
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
    // Inheritance is equality: a child text field that is exactly "{parent}",
    // or any other field set to the parent's current effective value, stores
    // nothing — the field keeps following the parent live. Only real
    // differences are recorded, and they win until changed back.
    const drop = cur.parent && k !== 'name' && (
      STYLE_TEXT_FIELDS.has(k)
        ? v === '{parent}'
        : JSON.stringify(v ?? null) === JSON.stringify((resolveStyle(list, cur.parent) || {})[k] ?? null))
    const next = {
      ...c,
      styles: list.map((s, i) => {
        if (i !== styleIdx) return s
        if (drop) { const { [k]: _x, ...rest } = s; return rest }
        return { ...s, [k]: v }
      }),
    }
    if (k === 'name') {
      // Renaming the default style keeps it the default, child styles follow
      // their renamed parent, and characters owned by the style stay owned.
      if (c.default_style === cur.name) next.default_style = v
      next.styles = next.styles.map((s) => (s.parent === cur.name ? { ...s, parent: v } : s))
      next.characters = (c.characters || []).map((ch) => (ch.style === cur.name ? { ...ch, style: v } : ch))
    }
    return next
  })
  // The style's DEFAULT film format — the same per-style automation override
  // as Settings → Automation → Default format, surfaced here because it shapes
  // the style's films (the Create picker starts on it, AI ideas are pitched
  // for it, unattended runs film in it). Inheritance is equality, as with
  // setAuto: picking what the style would inherit stores nothing, so the
  // style keeps following its parent chain — and Global — live.
  const stFormatInherited = resolveAutomation(styles, st.name, cfg, -1).auto_format || 'narration'
  const stFormat = resolveAutomation(styles, st.name, cfg).auto_format || 'narration'
  const setStyleFormat = (v) => editCfg((c) => ({
    ...c,
    styles: (c.styles || []).map((s, i) => {
      if (i !== styleIdx) return s
      const next = { ...(s.automation || {}) }
      if (v === stFormatInherited) delete next.auto_format
      else next.auto_format = v
      const { automation: _drop, ...rest } = s
      return Object.keys(next).length ? { ...rest, automation: next } : rest
    }),
  }))

  // Drop an override so the field falls back to the parent's value.
  const clearStyleField = (k) => editCfg((c) => {
    const list = c.styles || []
    const cur = list[styleIdx]
    if (!cur) return c
    const { [k]: _dropped, ...rest } = cur
    return { ...c, styles: list.map((s, i) => (i === styleIdx ? rest : s)) }
  })
  // Re-parent the selected style. Choosing a parent keeps only the fields that
  // actually differ from it (everything equal collapses to "inherited");
  // clearing the parent freezes the current effective values into a standalone
  // style so nothing visibly changes.
  const setParent = (pname) => editCfg((c) => {
    const list = c.styles || []
    const cur = list[styleIdx]
    if (!cur) return c
    let next
    if (pname) {
      const pe = resolveStyle(list, pname) || {}
      next = { name: cur.name, parent: pname }
      for (const [k, v] of Object.entries(cur)) {
        if (k === 'name' || k === 'parent') continue
        if (k === 'automation') {
          // Automation overrides collapse per FLAG, not as a whole object, so
          // adopting a parent keeps only the flags that really differ from it.
          const kept = Object.fromEntries(Object.entries(v || {})
            .filter(([f, fv]) => JSON.stringify(fv) !== JSON.stringify((pe.automation || {})[f])))
          if (Object.keys(kept).length) next.automation = kept
          continue
        }
        if (JSON.stringify(v) !== JSON.stringify(pe[k])) next[k] = v
      }
    } else {
      next = { ...(resolveStyle(list, cur.name) || cur) }
      delete next.parent
    }
    return { ...c, styles: list.map((s, i) => (i === styleIdx ? next : s)) }
  })
  // Update one field of one size bucket (small/medium/large) on the current style.
  const setSizePreset = (bucket, key, value) => {
    const presets = { ...(eff.size_presets || {}) }
    presets[bucket] = { ...(presets[bucket] || {}), [key]: value }
    setStyleField('size_presets', presets)
  }
  // One character library; each entry's `style` field scopes it — '' = the
  // global pool every style inherits automatically, else the owning style
  // (visible to it and every style under it). The backend normalizes/ids these
  // on save (_norm_characters), so the UI can add bare rows and drop blank
  // aliases.
  const chars = cfg.characters || []
  const addChar = (scope = '') => set('characters', [...chars, { name: '', aliases: [], description: '', enabled: true, style: scope }])
  const updateChar = (i, patch) => set('characters', chars.map((c, j) => (j === i ? { ...c, ...patch } : c)))
  const removeChar = (i) => set('characters', chars.filter((_, j) => j !== i))
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
    // A new style starts from the currently selected one — either as an
    // independent copy, or as a CHILD that inherits it live and stores only
    // what it later overrides (style hierarchy).
    const desc = newDesc.trim()
    const row = newChild
      ? { name, parent: st.name, ...(desc ? { description: desc } : {}) }
      : { ...st, name, description: desc }
    editCfg((c) => ({ ...c, styles: [...(c.styles || []), row] }))
    setStyleIdx(styles.length)
    setNewOpen(false); setNewName(''); setNewDesc(''); setNewChild(false)
  }
  const deleteStyle = () => {
    if (styles.length <= 1) return
    const kids = styles.filter((s) => s.parent === st.name).map((s) => s.name)
    if (kids.length) {
      setError(`“${st.name}” is the parent of ${kids.join(', ')} — change their parent first.`)
      return
    }
    // Characters owned by the style are re-homed, not orphaned: to its parent
    // when it has one, else to the global pool.
    const owned = (cfg.characters || []).filter((ch) => ch.style === st.name).length
    const home = st.parent ? `“${st.parent}”` : 'the global pool'
    const note = owned ? ` Its ${owned} character(s) move to ${home}.` : ''
    if (!window.confirm(`Delete style “${st.name}”? Videos already rendered keep their settings.${note}`)) return
    editCfg((c) => {
      const list = (c.styles || []).filter((_, i) => i !== styleIdx)
      const next = {
        ...c,
        styles: list,
        characters: (c.characters || []).map((ch) => (ch.style === st.name ? { ...ch, style: st.parent || '' } : ch)),
      }
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
              <Field label="Temporal AI upscale chunk (sec)" hint="Recovery only: clips are always upscaled whole. If a worker runs out of memory and returns black frames, the clip is retried in pieces this long, halving until it fits. 0 uses the default (12s). Splitting is never the default — joining separately upscaled pieces breaks continuity at the seams.">
                <input className="input" type="number" min={0} step={1}
                  value={cfg.temporal_video_upscale_chunk_seconds ?? 0}
                  onChange={(e) => set('temporal_video_upscale_chunk_seconds', Math.max(0, +e.target.value || 0))} />
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

          {/* ── Video models (engines) ── */}
          <Card span={12} className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm">Video models</span>
              <span className="muted" style={{ fontSize: 11.5 }}>pick per style under <strong>Styles</strong> · download here</span>
            </div>
            <div className="stack gap-10 mt-16">
              {!engineInfo && <div className="muted" style={{ fontSize: 12 }}>Loading engines…</div>}
              {(engineInfo?.video_engines || []).map((e) => {
                const avail = engineInfo?.video_availability?.[e.key]
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
                      {e.license_note && <div className="muted" style={{ fontSize: 12 }}>{e.license_note}</div>}
                      {ins?.status === 'error' && <div style={{ color: 'var(--danger)', fontSize: 12 }}>Download failed{ins.error ? `: ${ins.error}` : ' — see workers'}</div>}
                      {ins?.status === 'done' && <div style={{ color: 'var(--ok)', fontSize: 12 }}>Download complete</div>}
                    </div>
                    {e.downloadable && (
                      <Button variant="ghost" size="sm" icon={running ? 'spinner' : 'download'}
                        disabled={running || avail === true}
                        onClick={() => installEngine(e.key)}>
                        {running ? 'Downloading…' : avail === true ? 'Installed' : 'Download'}
                      </Button>
                    )}
                  </div>
                )
              })}
              <div className="field__hint">MiniMax H3's nodes ship with ComfyUI itself (≥ v0.30.0) and LTX 2.5 support with ≥ v0.32.0 — “not installed” with weights already downloaded usually means the worker container needs a rebuild. LTX 2.5 downloads need a Hugging Face token with the Lightricks/LTX-2.5 license accepted. Expect much slower renders from MiniMax than LTX.</div>
            </div>
          </Card>

          {/* ── Acted-scene models (Ref2VA engines) ── */}
          <Card span={12} className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm">Acted-scene models</span>
              <span className="muted" style={{ fontSize: 11.5 }}>pick per style under <strong>Styles</strong> · download here</span>
            </div>
            <div className="stack gap-10 mt-16">
              {!engineInfo && <div className="muted" style={{ fontSize: 12 }}>Loading engines…</div>}
              {(engineInfo?.reference_engines || []).map((e) => {
                const avail = engineInfo?.video_availability?.[e.key]
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
                      {e.license_note && <div className="muted" style={{ fontSize: 12 }}>{e.license_note}</div>}
                      {ins?.status === 'error' && <div style={{ color: 'var(--danger)', fontSize: 12 }}>Download failed{ins.error ? `: ${ins.error}` : ' — see workers'}</div>}
                      {ins?.status === 'done' && <div style={{ color: 'var(--ok)', fontSize: 12 }}>Download complete</div>}
                    </div>
                    {e.downloadable && (
                      <Button variant="ghost" size="sm" icon={running ? 'spinner' : 'download'}
                        disabled={running || avail === true}
                        onClick={() => installEngine(e.key)}>
                        {running ? 'Downloading…' : avail === true ? 'Installed' : 'Download'}
                      </Button>
                    )}
                  </div>
                )
              })}
              <div className="field__hint">Ref2VA engines render acted (dialogue) scenes from character portraits and cast voices — see the docs' performance films page. None are part of the bulk install: MiniMax H3's license restricts where it may be used, so downloading is an explicit choice. The LightX2V turbo download also converts the LoRA's key names on install — without that step the LoRA would silently not apply.</div>
            </div>
          </Card>

          {/* ── Music models (engines) ── */}
          <Card span={12} className="reveal reveal-d2">
            <div className="row center between">
              <span className="label-sm">Music models</span>
              <span className="muted" style={{ fontSize: 11.5 }}>pick per style under <strong>Styles</strong> · download here</span>
            </div>
            <div className="stack gap-10 mt-16">
              {!engineInfo && <div className="muted" style={{ fontSize: 12 }}>Loading engines…</div>}
              {(engineInfo?.music_engines || []).map((e) => {
                const avail = engineInfo?.music_availability?.[e.key]
                const ins = engInstall[e.key]
                const running = ins?.status === 'running'
                return (
                  <div key={e.key} className="row center between" style={{ borderTop: '1px solid var(--line)', paddingTop: 10, gap: 12 }}>
                    <div className="grow">
                      <div className="row center gap-8" style={{ flexWrap: 'wrap' }}>
                        <span style={{ fontWeight: 600 }}>{e.label}</span>
                        {avail === true && <Chip tone="ok" dot>installed</Chip>}
                        {avail === false && <Chip tone="warn">not installed</Chip>}
                      </div>
                      <div className="muted" style={{ fontSize: 12 }}>{e.sub} · {e.license}</div>
                      {e.license_note && <div className="muted" style={{ fontSize: 12 }}>{e.license_note}</div>}
                      {ins?.status === 'error' && <div style={{ color: 'var(--danger)', fontSize: 12 }}>Download failed{ins.error ? `: ${ins.error}` : ' — see workers'}</div>}
                      {ins?.status === 'done' && <div style={{ color: 'var(--ok)', fontSize: 12 }}>Download complete</div>}
                    </div>
                    {e.downloadable && (
                      <Button variant="ghost" size="sm" icon={running ? 'spinner' : 'download'}
                        disabled={running || avail === true}
                        onClick={() => installEngine(e.key)}>
                        {running ? 'Downloading…' : avail === true ? 'Installed' : 'Download'}
                      </Button>
                    )}
                  </div>
                )
              })}
              <div className="field__hint">MiniMax Music 3 is ~14 GB per worker and needs ComfyUI ≥ v0.33.0 for its nodes — “not installed” with the weights already downloaded means the worker container needs a rebuild. It writes song-shaped tracks and takes minutes where ACE-Step takes seconds (measured on a GB10: 83 s for a 30 s bed); it also stops at 6 minutes, and a longer film loops the bed.</div>
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
            {styles.some((s) => s.parent) ? (
              /* Hierarchy present: compact tree — an only-child chain stays on
                 its parent's line (BHOB ↳ BHOB Español); a new line starts only
                 at a second+ sibling. In the depth-first order that's exactly
                 the nodes NOT one level deeper than their predecessor. Cells go
                 into a grid (one column per depth, max-content wide) so
                 siblings on different lines align under the same column. */
              (() => {
                const cells = []
                let row = -1
                styleTreeOrder(styles).forEach((o, j, arr) => {
                  if (j === 0 || o.depth !== arr[j - 1].depth + 1) row++
                  cells.push({ ...o, row })
                })
                const cols = cells.reduce((m, c) => Math.max(m, c.depth), 0) + 1
                return (<>
                  <div className="mt-16" style={{ overflowX: 'auto' }}>
                    <div style={{ display: 'inline-grid', gridTemplateColumns: `repeat(${cols}, max-content)`, gap: 6, alignItems: 'center' }}>
                      {cells.map(({ style: s, depth, row: r }) => {
                        const i = styles.indexOf(s)
                        return (
                          <div key={s.name || i} style={{ gridRow: r + 1, gridColumn: depth + 1 }}>
                            <Button variant={i === styleIdx ? 'primary' : 'ghost'}
                              icon={cfg.default_style === s.name ? 'star' : undefined}
                              onClick={() => setStyleIdx(i)}>{`${depth ? '↳ ' : ''}${s.name || '(unnamed)'}`}</Button>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                  <div style={{ marginTop: 10 }}><Button variant="ghost" icon="plus" onClick={() => setNewOpen((v) => !v)}>New style</Button></div>
                </>)
              })()
            ) : (
              <div className="row gap-6 row--wrap mt-16">
                {styles.map((s, i) => (
                  <Button key={s.name || i} variant={i === styleIdx ? 'primary' : 'ghost'}
                    icon={cfg.default_style === s.name ? 'star' : undefined}
                    onClick={() => setStyleIdx(i)}>{s.name || '(unnamed)'}</Button>
                ))}
                <Button variant="ghost" icon="plus" onClick={() => setNewOpen((v) => !v)}>New style</Button>
              </div>
            )}
            {newOpen && (
              <div className="stack gap-10 mt-16" style={{ borderTop: '1px solid var(--line)', paddingTop: 16 }}>
                <div className="row center gap-10 row--wrap">
                  <input className="input" placeholder="Style name" value={newName}
                    onChange={(e) => setNewName(e.target.value)} style={{ maxWidth: 220 }} />
                  <div className="grow">
                    <input className="input" placeholder="Short description — what it looks and sounds like"
                      value={newDesc} onChange={(e) => setNewDesc(e.target.value)} />
                  </div>
                  <Button variant="primary" icon="plus" disabled={!newName.trim() || nameTaken(newName.trim())}
                    onClick={addStyle}>Create</Button>
                </div>
                <Check checked={newChild} onChange={setNewChild}
                  label={`Create as a child of “${st.name}” — inherits all its settings live and stores only what you override (edits to “${st.name}” keep flowing through)`} />
              </div>
            )}
            {newOpen && nameTaken(newName.trim()) && (
              <div className="muted" style={{ fontSize: 12, marginTop: 6, color: 'var(--warn)' }}>A style with that name already exists.</div>
            )}
            <div className="field__hint" style={{ marginTop: 10 }}>
              A new style starts as an independent copy of the selected one — or as a child that inherits it. Remember to <strong>Save settings</strong> after editing.
            </div>
          </Card>

          {/* ── Identity ── */}
          <Card span={12} className="reveal reveal-d1">
            <div className="row center between">
              <span className="label-sm">
                Style —{' '}
                {styleLineage(styles, st.name).slice(0, -1).map((a) => (
                  <span key={a.name}>
                    <a role="button" tabIndex={0} title={`Open “${a.name}”`}
                      style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
                      onClick={() => setStyleIdx(styles.indexOf(a))}>{a.name}</a>
                    {' ▸ '}
                  </span>
                ))}
                {st.name}
              </span>
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
              <Field label="Parent style" hint="Inherit every setting from another style and override only what differs — later edits to the parent flow through automatically. Text fields literally contain {parent} where the parent’s text goes: a box that is just {parent} follows it entirely; add text around the placeholder to extend it, or replace it to override. Picking a parent keeps just what differs; clearing it freezes the current values.">
                <select className="select" value={st.parent || ''} onChange={(e) => setParent(e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="">(none — standalone style)</option>
                  {styles.filter((s) => s.name && s.name !== st.name && !stDescendants.has(s.name))
                    .map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
                  {parentMissing && <option value={st.parent}>{st.parent} (missing)</option>}
                </select>
              </Field>
              {parentMissing && (
                <Banner tone="warn">Parent style “{st.parent}” doesn’t exist — this style behaves like a standalone one until the parent is restored or re-picked.</Banner>
              )}
              <Field label="Description" hint="What this style is for — shown when choosing a style for a video.">
                <textarea className="textarea" rows={2} value={fieldVal('description')} onChange={(e) => setStyleField('description', e.target.value)} />
                <ParentPreview k="description" />
              </Field>
              <Field label="YouTube channel" hint="Where videos in this style are published — connect channels in the Channels tab.">
                <select className="select" value={eff.channel || ''} onChange={(e) => setStyleField('channel', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="">(first connected channel)</option>
                  {(cfg.youtube_channels || []).map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
                </select>
                <ParentVal k="channel" />
              </Field>
              <Field label="YouTube playlist" hint="Add every upload in this style to a playlist on its channel. “Auto-create” makes (or reuses) one named after the style.">
                <select className="select" value={eff.youtube_playlist_id || ''} onChange={(e) => setStyleField('youtube_playlist_id', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="">(none — don’t add to a playlist)</option>
                  <option value="__auto__">Auto-create playlist named after the style</option>
                  {stPlaylists.items.map((p) => <option key={p.id} value={p.id}>{p.title || p.id}</option>)}
                  {eff.youtube_playlist_id && eff.youtube_playlist_id !== '__auto__'
                    && !stPlaylists.items.some((p) => p.id === eff.youtube_playlist_id)
                    && <option value={eff.youtube_playlist_id}>{stPlaylists.loading ? 'Loading…' : `${eff.youtube_playlist_id} (not on this channel)`}</option>}
                </select>
                <ParentVal k="youtube_playlist_id" />
                {stPlaylists.error
                  ? <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Couldn’t load playlists: {stPlaylists.error}</div>
                  : stPlaylists.loading
                    ? <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Loading playlists…</div>
                    : null}
              </Field>
              <Field label="X account" hint="Which X account this style posts to (or none) — connect accounts in the Channels tab.">
                <select className="select" value={eff.x_account || ''} onChange={(e) => setStyleField('x_account', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="">(none — don’t post to X)</option>
                  {(cfg.x_accounts || []).map((a) => <option key={a.id} value={a.id}>{a.name ? `@${a.name}` : a.id}</option>)}
                </select>
                <ParentVal k="x_account" />
              </Field>
              <Check checked={!!eff.made_for_kids} onChange={(v) => setStyleField('made_for_kids', v)}
                label="Made for Kids — self-declare this style's uploads as directed at children (disables personalized ads, comments and other features per YouTube's policy)" />
              <ParentVal k="made_for_kids" />
              <Field label="Opening cover" hint="After each render, burn the cover image into the opening of the video — YouTube Shorts ignore uploaded thumbnails and pick their own frame. Nothing is prepended, so timing and captions are unchanged. Finished films can also be stamped from their edit screen.">
                <select className="select" value={eff.first_frame_cover === 'text' ? 'image' : (eff.first_frame_cover || 'none')} onChange={(e) => setStyleField('first_frame_cover', e.target.value)} style={{ maxWidth: 320 }}>
                  <option value="none">Off — leave the opening as rendered</option>
                  <option value="image">Cover image</option>
                </select>
                <ParentVal k="first_frame_cover" />
              </Field>
              <Field label="Cover hold" hint="How long the cover is held at the start, in seconds. A single frame (0.04s) is a flash YouTube’s frame picker throws away — 1s reads as its own shot. “Cover image” freezes the picture for that long (audio keeps running); “Cover text” lays the title over the moving video, so holding it costs no motion.">
                <input className="input" type="number" min={0.04} max={3} step={0.1} value={eff.first_frame_cover_seconds ?? 1}
                  onChange={(e) => setStyleField('first_frame_cover_seconds', +e.target.value)} style={{ maxWidth: 120 }} />
                <ParentVal k="first_frame_cover_seconds" />
              </Field>
              <Field label="Burned-in subtitles" hint="Burn the script's captions into the video picture itself (open captions) when a render finishes — and again on every rebuild (remix, reassemble, localized cut, which burns its own language). Narrated scenes only; viewers can't switch them off, so channels that also upload the SRT track may want the channel's “Upload captions” off to avoid doubled text.">
                <label className="check">
                  <input type="checkbox" checked={!!eff.burn_subtitles}
                    onChange={(e) => setStyleField('burn_subtitles', e.target.checked)} />
                  <span>Burn subtitles into the final video</span>
                </label>
                <ParentVal k="burn_subtitles" />
              </Field>
              <Field label="Subtitle style" hint="How burned-in subtitles look — font, size, colours, outline or backdrop box, and where they sit. Applies to every burn from now on (renders and rebuilds alike); already-burned films pick it up on their next rebuild.">
                <SubtitleStyleEditor value={eff.subtitle_style}
                  onChange={(v) => setStyleField('subtitle_style', v)}
                  systemFonts={fontInfo || []} bundledFonts={fontBundled} />
                <ParentVal k="subtitle_style" />
              </Field>
              <Field label="Cover typography" hint="How cover titles look. The artwork is always generated TEXT-FREE (with this style's own image engine) and the title is drawn on top with real fonts — it can never be misspelled, regenerating rerolls only the artwork, and phrase edits re-apply instantly.">
                <CoverTypographyEditor value={eff.cover_typography}
                  onChange={(v) => setStyleField('cover_typography', v)}
                  systemFonts={fontInfo || []} bundledFonts={fontBundled} />
                <ParentVal k="cover_typography" />
              </Field>
            </div>
          </Card>

          {/* ── Script & content ── */}
          <Card span={12} className="reveal reveal-d2">
            <span className="label-sm">Script & content</span>
            <div className="stack gap-22 mt-16">
              <Field label="Default format"
                hint="What this style films by default: the Create screen starts on it, AI ideas are pitched to suit it, and unattended films are written in it. Every film can still switch formats on the Create screen. Music video unfolds its song steps under Settings → Automation.">
                <Segmented value={stFormat}
                  onChange={setStyleFormat}
                  options={[{ value: 'narration', label: 'Narration' }, { value: 'dialogue', label: 'Dialogue' },
                            { value: 'mixed', label: 'Mixed' }, { value: 'silent', label: 'Silent' },
                            { value: 'song', label: 'Music video' }]} />
                {(() => {
                  const from = (() => { const s = automationSource(styles, st.name, 'auto_format'); return s ? `“${s}”` : 'Global' })()
                  const lbl = { narration: 'Narration', dialogue: 'Dialogue', mixed: 'Mixed', silent: 'Silent', song: 'Music video' }[stFormatInherited] || String(stFormatInherited)
                  if (!('auto_format' in (st.automation || {}))) return <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Follows {from}.</div>
                  return (
                    <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                      {from}: <a role="button" tabIndex={0} title={`Use ${from}’s value again`}
                        style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
                        onClick={() => setStyleFormat(stFormatInherited)}><em>{lbl}</em></a> — click to use it.
                    </div>
                  )
                })()}
              </Field>
              <Field label={`Video length — ${fmtDuration(styleMinutes(eff))}`}
                hint={`How long this style's videos run. The script's word budget comes from the narrator's cadence, divided into scenes — ${lengthEstimateLabel(styleMinutes(eff), effectiveWpm(meta, eff).wpm, eff.tts_sentence_pause, eff.video_scenes, sceneBounds(eff))}.`}>
                <DurationInput value={eff.video_minutes || styleMinutes(eff)}
                  onChange={(v) => setStyleField('video_minutes', v)} />
                <ParentVal k="video_minutes" />
              </Field>
              <Field label="Scenes"
                hint={Number(eff.video_scenes) > 0
                  ? `That length split ${eff.video_scenes} ways — about ${Math.round(sceneSecsFor(styleMinutes(eff), eff.video_scenes, sceneBounds(eff)))} s a scene. Fewer scenes are longer ones; a scene never runs past what the video engine holds in one take. The Create screen can override it per film.`
                  : 'Automatic — as many scenes as the length needs (about 12 s of narration each, 10 s a take in an acted or silent film). Set a count to make this style’s scenes longer or shorter.'}>
                <input className="input" type="number" min={0} max={200} step={1}
                  value={eff.video_scenes || ''} placeholder="Auto" style={{ width: 110 }}
                  onChange={(e) => setStyleField('video_scenes', Math.max(0, Math.min(200, parseInt(e.target.value, 10) || 0)))} />
                <ParentVal k="video_scenes" />
              </Field>
              <Field label="Visual style" hint="Applied to every scene's image prompt.">
                <input className="input" value={fieldVal('visual_style')} onChange={(e) => setStyleField('visual_style', e.target.value)} />
                <ParentPreview k="visual_style" />
              </Field>
              <Field label="Video / motion style" hint="Steers how each scene moves — camera and subject motion in every scene's video prompt. e.g. “Favour dynamic action and visible movement over static shots and slow pans.”">
                <textarea className="textarea" rows={2} value={fieldVal('video_style')} onChange={(e) => setStyleField('video_style', e.target.value)} />
                <ParentPreview k="video_style" />
              </Field>
              <Field label="Video negative prompt" hint="Things to keep OUT of every video render in this style (artifacts, unwanted objects, styles). Leave blank to use the built-in quality default (blur, watermark, distortion, …).">
                <textarea className="textarea" rows={3} value={fieldVal('video_negative_prompt')} onChange={(e) => setStyleField('video_negative_prompt', e.target.value)} />
                <ParentPreview k="video_negative_prompt" />
              </Field>
              <Field label="Title style" hint="How AI-suggested video titles are worded — e.g. “short and punchy” or “pose an intriguing question”.">
                <textarea className="textarea" rows={2} value={fieldVal('title_style')} onChange={(e) => setStyleField('title_style', e.target.value)} />
                <ParentPreview k="title_style" />
              </Field>
              <Field label="Extra script instructions" hint="Appended to every topic.">
                <textarea className="textarea" rows={8} value={fieldVal('extra_instructions')} onChange={(e) => setStyleField('extra_instructions', e.target.value)} />
                <ParentPreview k="extra_instructions" />
              </Field>
              <Field label="Script — avoid" hint="Tell the writer what to keep OUT of the script for this style (topics, words, tropes, tone). e.g. “no politics, avoid the word ‘journey’, don't be preachy.”">
                <textarea className="textarea" rows={4} value={fieldVal('script_avoid')} onChange={(e) => setStyleField('script_avoid', e.target.value)} />
                <ParentPreview k="script_avoid" />
              </Field>
              <Field label="YouTube description suffix" hint="Appended to every generated YouTube description for videos in this style.">
                <textarea className="textarea" rows={3} value={fieldVal('description_suffix')} onChange={(e) => setStyleField('description_suffix', e.target.value)} />
                <ParentPreview k="description_suffix" />
              </Field>
              <Field label="Attribution footer" hint="Credit line appended to every YouTube description for this style. Cleared = no credit line.">
                <textarea className="textarea" rows={2} value={fieldVal('attribution_description')} onChange={(e) => setStyleField('attribution_description', e.target.value)} />
                <ParentPreview k="attribution_description" />
              </Field>
              <Field label="Attribution X hashtags" hint="Extra hashtags appended to X posts for this style — space or comma separated, the “#” is optional. e.g. “stephenspielbot”.">
                <input className="input" value={fieldVal('attribution_hashtags')} onChange={(e) => setStyleField('attribution_hashtags', e.target.value)} />
                <ParentPreview k="attribution_hashtags" />
              </Field>
              <Field label="Attribution YouTube tags" hint="Extra keyword tags appended to YouTube uploads for this style — comma separated. e.g. “stephenspielbot”.">
                <input className="input" value={fieldVal('attribution_youtube_tags')} onChange={(e) => setStyleField('attribution_youtube_tags', e.target.value)} />
                <ParentPreview k="attribution_youtube_tags" />
              </Field>
              {/* Voice model (TTS engine) — which narration model synthesises this style */}
              <Field label="Voice model" hint="Which TTS model synthesises this style's narration. OpenF5 is Apache-2.0 (commercial-safe); non-commercial models are flagged. Download models under Infrastructure.">
                <select className="select" value={eff.tts_engine || 'openf5'} onChange={(e) => setStyleField('tts_engine', e.target.value)}>
                  {(ttsEngineInfo?.engines?.length ? ttsEngineInfo.engines : [{ key: 'openf5', label: 'OpenF5-TTS-Base', commercial_ok: true }]).map((e) => (
                    <option key={e.key} value={e.key}>{e.label}{e.commercial_ok ? '' : ' · non-commercial'}</option>
                  ))}
                </select>
                <ParentVal k="tts_engine" />
              </Field>
              {/* Narration language — only multilingual voice models (Chatterbox) speak
                  non-English; the F5 models ignore it, so the picker hides for them. */}
              {(() => {
                const langs = (ttsEngineInfo?.engines || []).find((e) => e.key === (eff.tts_engine || 'openf5'))?.languages || {}
                if (!Object.keys(langs).length) return null
                return (
                  <Field label="Narration language" hint="Which language this style's narration is spoken in — the script text itself should be written in the same language.">
                    <select className="select" value={eff.tts_language || 'en'} onChange={(e) => setStyleField('tts_language', e.target.value)} style={{ maxWidth: 320 }}>
                      {Object.entries(langs).sort((a, b) => a[1].localeCompare(b[1])).map(([code, name]) => (
                        <option key={code} value={code}>{name}</option>
                      ))}
                    </select>
                    <ParentVal k="tts_language" />
                  </Field>
                )
              })()}
              {/* Narration — narrator picker, the dial-in sliders and test right below */}
              <div className="row gap-22 row--wrap" style={{ alignItems: 'flex-end' }}>
                <div className="grow"><Field label="Narrator voice"><select className="select" value={eff.voice || ''} onChange={(e) => setStyleField('voice', e.target.value)}>
                  <option value="">(F5-TTS default)</option>
                  {(meta.voices || []).filter((v) => v !== 'Default (F5-TTS)').map((v) => <option key={v} value={v}>{voiceLabel(v, voiceMetaMap(cfg.voices))}</option>)}
                </select><ParentVal k="voice" /></Field></div>
              </div>
              <div className="row gap-22 row--wrap">
                <div className="grow">{(() => {
                  const nat = voiceWpm(meta, eff.voice, eff.tts_engine)
                  const target = Number(eff.voice_cadence_wpm || 0)
                  // No target → show the pace narration actually plays at
                  // (natural × any legacy voice_speed multiplier).
                  const value = target > 0 ? target : Math.round(effectiveWpm(meta, eff).wpm)
                  const mult = Math.max(0.3, Math.min(2, value / nat.wpm))
                  return (
                    <Field label={`Cadence — ${value} words/min${target > 0 ? ` (×${mult.toFixed(2)})` : ' · natural'}`}
                      hint={`How fast the narrator speaks. This voice's natural pace is ~${Math.round(nat.wpm)} words/min${nat.measured ? '' : ' (estimated — calibrate it under the Voices tab)'}; the cadence also sets the script's word budget for the video length.`}>
                      <input className="slider" type="range" min={90} max={220} step={5}
                        value={Math.max(90, Math.min(220, value))}
                        onChange={(e) => setStyleField('voice_cadence_wpm', +e.target.value)} />
                      {target > 0 && (
                        <div className="field__hint">
                          <a onClick={() => setStyleField('voice_cadence_wpm', 0)} style={{ cursor: 'pointer' }}>Reset to natural pace</a>
                        </div>
                      )}
                      <ParentVal k="voice_cadence_wpm" />
                    </Field>
                  )
                })()}</div>
                <div className="grow"><Field label={`Robotic level — ${Math.round((eff.voice_robotic_amount ?? 0) * 100)}%`}
                  hint="0% is natural (off); higher is a more synthetic monotone so the voice isn't mistaken for a human.">
                  <input className="slider" type="range" min={0} max={1} step={0.05}
                    value={eff.voice_robotic_amount ?? 0}
                    onChange={(e) => setStyleField('voice_robotic_amount', +e.target.value)} />
                  <ParentVal k="voice_robotic_amount" />
                </Field></div>
                <div className="grow"><Field label={`Sentence pause — ${(eff.tts_sentence_pause ?? 0).toFixed(2)}s`}
                  hint="Extra silence enforced between narration sentences for a calmer cadence — 0 keeps the model's own pacing. Try 0.3–0.6s.">
                  <input className="slider" type="range" min={0} max={2} step={0.05}
                    value={eff.tts_sentence_pause ?? 0}
                    onChange={(e) => setStyleField('tts_sentence_pause', +e.target.value)} />
                  <ParentVal k="tts_sentence_pause" />
                </Field></div>
              </div>
              <VoiceTester voice={eff.voice} roboticAmount={eff.voice_robotic_amount} cadenceWpm={eff.voice_cadence_wpm} engine={eff.tts_engine} language={eff.tts_language} sentencePause={eff.tts_sentence_pause} onError={setError} />
            </div>
          </Card>

          {/* ── Characters (inherited cast summary — managed on the Characters tab) ── */}
          <Card span={12} className="reveal reveal-d3">
            <div className="row center between">
              <span className="label-sm">Characters</span>
              <Button variant="ghost" icon="user-group" onClick={() => setTab('characters')}>Manage characters</Button>
            </div>
            <div className="field__hint" style={{ marginTop: 6 }}>
              The cast “{st.name}” inherits automatically: every <strong>global</strong> character, plus the ones that belong to this style or a style above it. When a scene mentions one by name (or an alias), its appearance is written into the image prompt so it stays consistent across scenes and videos. Scripts only cast these characters when you <strong>ask for one by name</strong> in the topic, the title, or this style’s extra instructions — otherwise every story invents its own people.
            </div>
            {(() => {
              const lineageNames = styleLineage(styles, st.name).map((s) => s.name)
              const cast = chars.filter((c) => !c.style || lineageNames.includes(c.style))
              if (!cast.length) {
                return <div className="muted" style={{ fontSize: 12, marginTop: 14 }}>No characters reach this style yet — create some under <strong>Characters</strong>.</div>
              }
              return (
                <div className="row row--wrap" style={{ gap: 8, marginTop: 14 }}>
                  {cast.map((c, idx) => (
                    <span key={c.id || `new-${idx}`} className="chip chip--neutral"
                      title={c.description || ''}
                      style={{ padding: '4px 12px 4px 5px', cursor: 'pointer', opacity: c.enabled === false ? 0.55 : 1 }}
                      onClick={() => setTab('characters')}>
                      {c.id && c.ref_image
                        ? <img src={`${fileUrl(`${meta.characters_dir}/${c.id}.png`)}&v=${charBust}`} alt=""
                            style={{ width: 22, height: 22, borderRadius: '50%', objectFit: 'cover' }} />
                        : <span style={{ width: 22, height: 22, borderRadius: '50%', background: 'var(--paper-2)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 10.5 }}>
                            {(c.name || '?').trim().charAt(0).toUpperCase() || '?'}
                          </span>}
                      {c.name || '(unnamed)'}
                      <span className="muted" style={{ fontWeight: 400 }}>
                        {!c.style ? '· global' : (c.style === st.name ? '' : `· from ${c.style}`)}{c.enabled === false ? ' · disabled' : ''}
                      </span>
                    </span>
                  ))}
                </div>
              )
            })()}
          </Card>

          {/* ── Render quality ── */}
          <Card span={6} className="reveal reveal-d3">
            <span className="label-sm">Render quality</span>
            <div className="stack gap-22 mt-16">
              <Field label="Resolution" hint="Orientation, then quality (higher = slower).">
                <ResolutionPicker value={eff.resolution || ''} onChange={(r) => setStyleField('resolution', r)} meta={meta} />
                <ParentVal k="resolution" />
              </Field>
              {(() => {
                // The finishing upscaler only matters when something in this
                // style targets an upscale-only size (QHD/4K) — the render then
                // finishes with this upscale to reach the target.
                const upscaleOnly = (key) =>
                  (meta.pixel_tiers || []).find((t) => t.key === key)?.upscale_only
                const presetsInPlay = Object.values(eff.size_presets || {})
                  .map((p) => p?.resolution).filter(Boolean)
                const anyFinishing = [eff.resolution, ...presetsInPlay]
                  .some((r) => r && upscaleOnly(resolutionTier(meta, r)))
                if (!anyFinishing) return null
                return (
                  <Field label="Finishing upscaler" hint="How a QHD/4K target is reached from the rendered film. AI modes shoot each scene through a ComfyUI upscaler; Fast is a plain resample.">
                    <select className="select" value={eff.finish_upscale_mode || 'flashvsr'}
                      onChange={(e) => setStyleField('finish_upscale_mode', e.target.value)}>
                      <option value="flashvsr">FlashVSR (video super-resolution)</option>
                      <option value="fast">Fast (ffmpeg)</option>
                      <option value="ltx_latent">LTX latent (simple model)</option>
                      <option value="ic_lora">LTX IC-LoRA (generative)</option>
                      <option value="h3_latent">H3 latent (MiniMax H3)</option>
                    </select>
                    <ParentVal k="finish_upscale_mode" />
                  </Field>
                )
              })()}
              <div className="row gap-22 row--wrap">
                <div className="grow"><Field label="First-pass steps" hint="8 distilled · 20–30 dev model.">
                  <input className="input" type="number" value={eff.first_pass_steps ?? ''} onChange={(e) => setStyleField('first_pass_steps', +e.target.value)} /><ParentVal k="first_pass_steps" /></Field></div>
                <div className="grow"><Field label="Second-pass steps">
                  <input className="input" type="number" value={eff.second_pass_steps ?? ''} onChange={(e) => setStyleField('second_pass_steps', +e.target.value)} /><ParentVal k="second_pass_steps" /></Field></div>
              </div>
              <Field label={`LoRA strength — ${eff.lora_strength ?? 0}`}>
                <input className="slider" type="range" min={0} max={1} step={0.05} value={eff.lora_strength ?? 0} onChange={(e) => setStyleField('lora_strength', +e.target.value)} />
                <ParentVal k="lora_strength" />
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
                  <select className="select" value={eff.image_engine || 'flux1-schnell'} onChange={(e) => setStyleField('image_engine', e.target.value)}>
                    {(engineInfo.engines || []).filter((e) => e.can_generate).map((e) => (
                      <option key={e.key} value={e.key}>{e.label}{e.commercial_ok ? '' : ' · non-commercial'}</option>
                    ))}
                  </select>
                  <ParentVal k="image_engine" />
                </Field>
                <Field label="Edit (mask + prompt)" hint="Model used for masked 'Edit image' inpaints.">
                  <select className="select" value={eff.edit_engine || 'flux1-schnell'} onChange={(e) => setStyleField('edit_engine', e.target.value)}>
                    {(engineInfo.engines || []).filter((e) => e.can_edit).map((e) => (
                      <option key={e.key} value={e.key}>{e.label}{e.commercial_ok ? '' : ' · non-commercial'}</option>
                    ))}
                  </select>
                  <ParentVal k="edit_engine" />
                </Field>
              </>)}
            </div>
          </Card>

          {/* ── Video models: one per kind of scene, one shared steps knob ── */}
          <Card span={6} className="reveal reveal-d3">
            <span className="label-sm">Video models</span>
            <div className="field__hint" style={{ marginTop: 6 }}>
              A film can hold two kinds of scene, and each kind has its own model.
              Download models under <strong>Infrastructure</strong>.
            </div>
            <div className="stack gap-22 mt-16">
              {!engineInfo && <div className="muted" style={{ fontSize: 12 }}>Loading engines…</div>}
              {engineInfo && (
                <Field label="Narrated & silent scenes"
                  hint="Animates each scene from its first-frame still, with the narrator's voice-over on top. In a MIXED film these scenes render on MiniMax H3 automatically, so the whole film matches the acted takes.">
                  <select className="select" value={eff.video_engine || engineInfo.default_video_engine || 'ltx25'} onChange={(e) => setStyleField('video_engine', e.target.value)}>
                    {(engineInfo.video_engines || []).map((e) => (
                      <option key={e.key} value={e.key}>{e.label}{e.commercial_ok ? '' : ' · non-commercial'}</option>
                    ))}
                  </select>
                  <ParentVal k="video_engine" />
                  {(() => {
                    const sel = (engineInfo.video_engines || []).find((x) => x.key === (eff.video_engine || engineInfo.default_video_engine))
                    return sel?.license_note ? <div className="field__hint" style={{ marginTop: 6 }}>{sel.license_note}</div> : null
                  })()}
                </Field>
              )}
              {engineInfo && (
                <Field label="Acted (dialogue) scenes"
                  hint="Generates each acted scene — picture and spoken dialogue in one pass — from the characters' portraits and cast voices. No first frame. Unused by a film with no dialogue scenes.">
                  <select className="select" value={eff.reference_engine || ''} onChange={(e) => setStyleField('reference_engine', e.target.value)}>
                    {(engineInfo.reference_engines || []).map((e) => (
                      <option key={e.key} value={e.key}>{e.label} — {e.sub}</option>
                    ))}
                  </select>
                  {(engineInfo.reference_engines || []).find((e) => e.key === eff.reference_engine)?.license_note && (
                    <div className="field__hint" style={{ marginTop: 6 }}>{(engineInfo.reference_engines).find((e) => e.key === eff.reference_engine).license_note}</div>
                  )}
                  <ParentVal k="reference_engine" />
                </Field>
              )}
              {engineInfo && (String(eff.video_engine || engineInfo.default_video_engine || '').startsWith('minimax')
                || String(eff.reference_engine || engineInfo.default_reference_engine || '').startsWith('minimax')) && (
                <Field label="Sampling steps — every MiniMax render"
                  hint="ONE knob for both pickers above: it overrides the step count of every MiniMax H3 render in this style — narrated-scene I2V and acted-scene Ref2VA alike. 0 = each engine's own default (Turbo: 4, the others: 15). More steps = sharper but slower (~2.5 min per step per scene on a GB10). LTX ignores it.">
                  <input className="input" type="number" min={0} max={50} style={{ width: 120 }}
                    value={eff.video_steps ?? 0}
                    onChange={(e) => setStyleField('video_steps', Math.max(0, Math.min(50, Math.round(+e.target.value || 0))))} />
                  <ParentVal k="video_steps" />
                </Field>
              )}
              {engineInfo && (
                <Field label="Chained scenes — longer than H3 can render in one pass"
                  hint="H3 tops out near 15 s a clip. With this on, a scene that needs more is rendered as TWO clips joined by H3 Motion Context — the second continues the first's motion and audio instead of cutting — so scenes can run to ~29 s. It always covers ACTED scenes (dialogue renders through MiniMax Ref2VA in every style), silent scenes wherever the toggle below performs them, and narrated scenes too when the video engine above is MiniMax; LTX narrated scenes ignore it (LTX continues clips natively). Scripts are planned to match: fewer, longer scenes — narration carries about twice the words, and dialogue that used to split into two scenes stays one take. Costs ~22% more render time per delivered second on the scenes that chain, and the workers must have the Motion Context nodes baked in.">
                  <label className="check">
                    <input type="checkbox" checked={!!eff.h3_chain_scenes}
                      onChange={(e) => setStyleField('h3_chain_scenes', e.target.checked)} />
                    <span>Render long scenes as two chained clips</span>
                  </label>
                  <ParentVal k="h3_chain_scenes" />
                </Field>
              )}
              {engineInfo && (
                <Field label="Silent scenes — act them on H3 too"
                  hint="A silent scene (a visual beat with no voice-over) is normally animated from a first-frame still by the I2V engine above. With this on EVERY silent scene is PERFORMED instead: one MiniMax H3 Ref2VA take carrying its own ambience — the same path the acted scenes take, so a silent beat cuts against them as one production. The take still opens on the scene's own first frame, and the portraits of anyone the writer put on screen join it as references, which is what keeps a face the same between a silent beat and the acted scene beside it. A performed silent beat runs 5–12 s (or up to ~23 s with Chained scenes above, shot as two joined clips). Costs an acted scene's render time (~6 min per 10 s) instead of an I2V clip's.">
                  <label className="check">
                    <input type="checkbox" checked={!!eff.h3_silent_scenes}
                      onChange={(e) => setStyleField('h3_silent_scenes', e.target.checked)} />
                    <span>Perform silent scenes on H3</span>
                  </label>
                  <ParentVal k="h3_silent_scenes" />
                </Field>
              )}
              {engineInfo && (
                <Field label="First frames — open every acted scene on a painted image"
                  hint="An acted scene normally renders from its cast's portraits alone — no opening image. With this on, every acted scene FIRST gets a first-frame image (from its image prompt, or composed from its setting when there is none) and the take is told to open on that picture. Use it in styles where the opening composition matters more than letting the model choose its own. A hand-picked location reference still outranks painting one, and any frame can be regenerated, replaced or removed per scene in the editors. Adds one FLUX render per acted scene.">
                  <label className="check">
                    <input type="checkbox" checked={!!eff.h3_first_frames}
                      onChange={(e) => setStyleField('h3_first_frames', e.target.checked)} />
                    <span>Always give acted scenes a first frame</span>
                  </label>
                  <ParentVal k="h3_first_frames" />
                </Field>
              )}
            </div>
          </Card>

          {/* ── Narrator & audio ── */}
          <Card span={6} className="reveal reveal-d3">
            <span className="label-sm">Narrator & audio</span>
            <div className="stack gap-22 mt-16">
              <Field label="Music"
                hint="Score every film in this style. Music is mixed in at the very end, never baked into a scene — off leaves the film with only its voices and room tone. Acted films never get a score.">
                <Check checked={eff.music_enabled !== false}
                  onChange={(v) => setStyleField('music_enabled', v)}
                  label="Add background music" />
                <ParentVal k="music_enabled" />
              </Field>
              {engineInfo && eff.music_enabled !== false && (
                <Field label="Music model"
                  hint="Writes the background bed. ACE-Step is quick and stays instrumental; MiniMax Music 3 writes song-shaped tracks at much higher quality but takes minutes per film and caps at 6 minutes. Download models under Infrastructure.">
                  <select className="select" value={eff.music_engine || engineInfo.default_music_engine || 'ace-step'} onChange={(e) => setStyleField('music_engine', e.target.value)}>
                    {(engineInfo.music_engines || []).map((e) => (
                      <option key={e.key} value={e.key}>{e.label}</option>
                    ))}
                  </select>
                  <ParentVal k="music_engine" />
                  {(() => {
                    const sel = (engineInfo.music_engines || []).find((x) => x.key === (eff.music_engine || engineInfo.default_music_engine))
                    return sel?.license_note ? <div className="field__hint" style={{ marginTop: 6 }}>{sel.license_note}</div> : null
                  })()}
                </Field>
              )}
              <Field label="Lyric timing"
                hint="Music videos only. Whisper-aligns the lyric sheet to the song's separated vocal stem at divide time, so every scene names and cuts on the words actually sung under it. Needs the re-voicing install (scripts/install_svc.sh); without it — or when the alignment can't be trusted — the energy measurement is used instead, so this is safe to leave on.">
                <Check checked={eff.song_align_lyrics !== false}
                  onChange={(v) => setStyleField('song_align_lyrics', v)}
                  label="Align lyrics to the sung track" />
                <ParentVal k="song_align_lyrics" />
              </Field>
              {[['voice_vol', 'Voice volume', 150], ['music_vol', 'Music volume', 100], ['ambient_vol', 'Ambient volume', 100]].map(([k, label, max]) => (
                <Field key={k} label={`${label} — ${eff[k] ?? 0}%`}>
                  <input className="slider" type="range" min={0} max={max} value={eff[k] ?? 0} onChange={(e) => setStyleField(k, +e.target.value)} />
                  <ParentVal k={k} />
                </Field>
              ))}
            </div>
          </Card>

          {/* ── Size presets (Small/Medium/Large) ── */}
          <Card span={12} className="reveal reveal-d3">
            <span className="label-sm">Size presets</span>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
              The Small / Medium / Large one-tap sizes on the AI ideas screen — each sets a video length (minutes) and a resolution for this style.
            </div>
            <div className="stack gap-22 mt-16">
              {(meta.size_buckets || ['small', 'medium', 'large']).map((bucket) => {
                const preset = (eff.size_presets || {})[bucket] || (meta.default_size_presets || {})[bucket] || {}
                const mins = preset.minutes || Math.round(((preset.scenes || 6) * LEGACY_SCENE_SECS / 60) * 100) / 100
                return (
                  <div key={bucket} className="row gap-22 row--wrap" style={{ alignItems: 'flex-end' }}>
                    <div style={{ minWidth: 78 }}>
                      <span className="label-sm" style={{ textTransform: 'capitalize' }}>{bucket}</span>
                    </div>
                    <Field label={`Length — ${fmtDuration(mins)}`} hint={lengthEstimateLabel(mins, effectiveWpm(meta, eff).wpm, eff.tts_sentence_pause)}>
                      <DurationInput value={mins} onChange={(v) => setSizePreset(bucket, 'minutes', v)} />
                    </Field>
                    <div className="grow"><Field label="Resolution">
                      <ResolutionPicker value={preset.resolution || ''} onChange={(r) => setSizePreset(bucket, 'resolution', r)} meta={meta} />
                    </Field></div>
                  </div>
                )
              })}
              <ParentVal k="size_presets" />
            </div>
          </Card>
        </>)}

        {tab === 'assets' && <SettingsAssets styles={styles} />}

        {tab === 'characters' && (() => {
          // One library, browsed by home: a scope picker mirroring the Styles
          // tab's tree — a Global pill first (the pool every style inherits),
          // then the style hierarchy — and below it the characters that
          // belong to the picked scope. Scopes naming a deleted style surface
          // as warning pills (dormant until re-homed).
          const knownStyles = new Set(styles.map((s) => s.name))
          const missingScopes = [...new Set(chars.map((c) => c.style).filter((sc) => sc && !knownStyles.has(sc)))]
          const countFor = (key) => chars.filter((c) => (c.style || '') === key).length
          // The picked scope can vanish mid-edit (style renamed, last dangling
          // character re-homed) — fall back to Global rather than a dead view.
          const scope = charScope === '' || knownStyles.has(charScope) || missingScopes.includes(charScope) ? charScope : ''
          const missing = missingScopes.includes(scope)
          const scopeEntries = chars.map((c, i) => ({ c, i })).filter(({ c }) => (c.style || '') === scope)
          const scopeOptionsFor = (cur) => (<>
            <option value="">Global — every style</option>
            {styleTreeOrder(styles).map(({ style: s, depth }) => (
              <option key={s.name} value={s.name}>{'\u00A0\u00A0'.repeat(depth)}{depth ? '↳ ' : ''}{s.name}</option>
            ))}
            {cur && !knownStyles.has(cur) && <option value={cur}>{cur} — missing style</option>}
          </>)
          const pill = (key, label, { icon, depth = 0 } = {}) => {
            const n = countFor(key)
            return (
              <Button variant={scope === key ? 'primary' : 'ghost'} icon={icon}
                title={key === '' ? 'Characters every style inherits' : undefined}
                onClick={() => setCharScope(key)}>
                {`${depth ? '↳ ' : ''}${label}`}
                {n > 0 && <span style={{ opacity: 0.55, fontWeight: 500 }}>{n}</span>}
              </Button>
            )
          }
          const charCard = ({ c, i }) => (
            <div key={c.id || `row-${i}`} className="stack gap-12" style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 12 }}>
              {/* Every multi-field row is TOP-aligned: Field renders its hint
                  BELOW the control, so bottom-aligning a row whose hints differ
                  in height shoves the labels and inputs out of line. */}
              <div className="row gap-12" style={{ alignItems: 'flex-start' }}>
                {/* Portrait rides beside the name — at the card's foot it read as
                    belonging to the NEXT character. Upload/re-roll/version ops
                    stay below with the version strip. Sized to the name row +
                    appearance box beside it. */}
                {c.id && c.ref_image && (
                  <div onClick={() => setCharLightbox(c.id)} title="Full size"
                    style={{ position: 'relative', width: 132, height: 132, flex: '0 0 auto', borderRadius: 8, overflow: 'hidden', border: '1px solid var(--border)', cursor: 'zoom-in' }}>
                    <img src={`${fileUrl(`${meta.characters_dir}/${c.id}.png`)}&v=${charBust}`} alt=""
                      style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                  </div>
                )}
                <div className="grow stack gap-12" style={{ minWidth: 0 }}>
                  <div className="row gap-12 row--wrap" style={{ alignItems: 'flex-start' }}>
                    <div className="grow" style={{ minWidth: 180 }}><Field label="Name" hint="How scripts refer to this character.">
                      <input className="input" value={c.name || ''} placeholder="e.g. Robot XYZ"
                        onChange={(e) => updateChar(i, { name: e.target.value })} />
                    </Field></div>
                    <div className="grow" style={{ minWidth: 200 }}><Field label="Also known as" hint="Comma-separated aliases that also refer to this character.">
                      <input className="input" defaultValue={(c.aliases || []).join(', ')} placeholder="XYZ, the machine"
                        key={`alias-${c.id || i}`}
                        onBlur={(e) => updateChar(i, { aliases: e.target.value.split(',').map((s) => s.trim()).filter(Boolean) })} />
                    </Field></div>
                    <div style={{ minWidth: 210 }}><Field label="Belongs to" hint="Global, or one style (plus the styles under it).">
                      <select className="select" value={c.style || ''} onChange={(e) => updateChar(i, { style: e.target.value })}>
                        {scopeOptionsFor(c.style || '')}
                      </select>
                    </Field></div>
                  </div>
                  <Field label="Appearance" hint="Written verbatim into the image prompt — describe the look only, no name. e.g. “matte-black humanoid chassis, single cyan optical sensor, exposed brass joints”.">
                    <textarea className="textarea" rows={3} value={c.description || ''}
                      onChange={(e) => updateChar(i, { description: e.target.value })} />
                  </Field>
                </div>
              </div>
              {/* flex-start: only Background carries a hint, so bottom-aligning
                  the row pushed the hint-less selects out of line. */}
              <div className="row gap-12 row--wrap" style={{ alignItems: 'flex-start' }}>
                <div style={{ width: 130 }}><Field label="Sex">
                  <select className="input" value={c.gender || ''}
                    onChange={(e) => updateChar(i, { gender: e.target.value })}>
                    {['', 'male', 'female'].map((g) => <option key={g} value={g}>{g || 'unset…'}</option>)}
                  </select>
                </Field></div>
                <div style={{ width: 130 }}><Field label="Age">
                  <select className="input" value={c.age || ''}
                    onChange={(e) => updateChar(i, { age: e.target.value })}>
                    {['', 'child', 'young', 'adult', 'mature', 'elderly'].map((a) => <option key={a} value={a}>{a || 'unset…'}</option>)}
                  </select>
                </Field></div>
                <div className="grow"><Field label="Background" hint="Nationality, language, accent — e.g. “Brazilian, sings in Portuguese-accented English”. Sex and age drive voice auto-casting; all three shape the sung voice when they front a music video.">
                  <input className="input" value={c.background || ''} placeholder="e.g. Brazilian, light Portuguese accent"
                    onChange={(e) => updateChar(i, { background: e.target.value })} />
                </Field></div>
              </div>
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
              {/* Reference image — anchors the look to a photo/portrait (FLUX.2 only).
                  The image and kept versions are ALWAYS visible; only the ops that
                  persist server-side immediately (upload / portrait / version picks)
                  wait for a clean form, so staged edits can't be clobbered. */}
              {c.id ? (
                <div className="stack gap-12">
                  <div className="row gap-12 row--wrap" style={{ alignItems: 'center' }}>
                    {!c.ref_image && <span className="muted" style={{ fontSize: 12 }}>No reference image — text only.</span>}
                    {dirty
                      ? <span className="muted" style={{ fontSize: 12 }}><Icon name="circle-info" /> Unsaved edits — <strong>Save settings</strong> to upload or re-roll the look.</span>
                      : (<>
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
                      </>)}
                  </div>
                  <VersionStrip versions={c.history?.versions} selected={c.history?.selected}
                    onSelect={(vid) => selectCharVersion(c, vid)} onDelete={(vid) => deleteCharVersion(c, vid)}
                    aspect="1 / 1" busy={charBusy === c.id || dirty} />
                  <CharacterSheet char={c} initial={c.sheet} disabled={dirty}
                    disabledNote="Unsaved edits — Save settings to build a sheet."
                    onLightbox={(url) => setSheetLightbox(url)} />
                </div>
              ) : (
                <span className="muted" style={{ fontSize: 12 }}>Save settings, then upload or generate a reference image that pins this character's look (FLUX.2 only).</span>
              )}
            </div>
          )
          return (<>
            <Card span={12} className="reveal reveal-d1">
              <div className="row center between">
                <span className="label-sm">Characters</span>
                <span className="muted" style={{ fontSize: 11.5 }}>Global characters reach every style; a style's characters reach it and the styles under it.</span>
              </div>
              {/* Scope picker — same compact tree as the Styles tab, with a
                  leading Global pill (and warning pills for deleted homes). */}
              {styles.some((s) => s.parent) ? (() => {
                const cells = []
                let row = 0   // row 0 = the Global pill
                styleTreeOrder(styles).forEach((o, j, arr) => {
                  if (j === 0 || o.depth !== arr[j - 1].depth + 1) row++
                  cells.push({ ...o, row })
                })
                missingScopes.forEach((name) => { row++; cells.push({ missing: name, depth: 0, row }) })
                const cols = cells.reduce((m, c) => Math.max(m, c.depth), 0) + 1
                return (
                  <div className="mt-16" style={{ overflowX: 'auto' }}>
                    <div style={{ display: 'inline-grid', gridTemplateColumns: `repeat(${cols}, max-content)`, gap: 6, alignItems: 'center' }}>
                      <div style={{ gridRow: 1, gridColumn: 1 }}>{pill('', 'Global', { icon: 'globe' })}</div>
                      {cells.map((cell) => (
                        <div key={cell.missing ? `miss-${cell.missing}` : cell.style.name}
                          style={{ gridRow: cell.row + 1, gridColumn: cell.depth + 1 }}>
                          {cell.missing
                            ? pill(cell.missing, cell.missing, { icon: 'triangle-exclamation' })
                            : pill(cell.style.name, cell.style.name, { depth: cell.depth })}
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })() : (
                <div className="row gap-6 row--wrap mt-16">
                  <div>{pill('', 'Global', { icon: 'globe' })}</div>
                  {styles.map((s) => <div key={s.name}>{pill(s.name, s.name)}</div>)}
                  {missingScopes.map((name) => <div key={`miss-${name}`}>{pill(name, name, { icon: 'triangle-exclamation' })}</div>)}
                </div>
              )}

              {/* The picked scope's cast */}
              <div className="row center between" style={{ marginTop: 22 }}>
                <span className="label-sm">
                  {scope === '' ? 'Global characters' : (<>
                    {!missing && styleLineage(styles, scope).slice(0, -1).map((a) => (
                      <span key={a.name}>
                        <a role="button" tabIndex={0} title={`Show “${a.name}” characters`}
                          style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
                          onClick={() => setCharScope(a.name)}>{a.name}</a>
                        {' ▸ '}
                      </span>
                    ))}
                    {scope}
                  </>)}
                  <span className="muted" style={{ fontWeight: 400 }}> · {scopeEntries.length}</span>
                </span>
                {!missing && <Button variant="ghost" icon="plus" onClick={() => addChar(scope)}>Add character</Button>}
              </div>
              <div className="field__hint" style={{ marginTop: 6 }}>
                {scope === ''
                  ? <>Recurring people or things that keep the same look across scenes and videos — these are inherited by <strong>every style</strong>. When a scene mentions one by name (or an alias), its appearance is written into the image prompt. Generated portraits use the owning style's image model and visual look (global characters use the default style's).</>
                  : missing
                    ? <>The style “{scope}” no longer exists, so these characters are dormant. Pick a new home with each card's <strong>Belongs to</strong>.</>
                    : <>Used by “{scope}” and the styles under it, on top of the global pool every style inherits.</>}
              </div>
              <div className="stack gap-16 mt-16">
                {scopeEntries.length === 0 && (
                  <div className="muted" style={{ fontSize: 12 }}>
                    {scope === ''
                      ? 'No global characters yet — these are the ones every style would share (e.g. a channel mascot).'
                      : `No characters belong to “${scope}” yet — Add character creates one here, or move one in with its Belongs to picker.`}
                  </div>
                )}
                {scopeEntries.map((e) => charCard(e))}
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
            {sheetLightbox && (
              <ImageLightbox fallback={sheetLightbox} title="Turnaround sheet"
                onClose={() => setSheetLightbox(null)} />
            )}
          </>)
        })()}

        {tab === 'voices' && (<>
          {/* ── Voices (narrator reference clips) ── */}
          <VoicesManager voices={cfg.voices} busy={vbusy}
            ttsLanguages={(ttsEngineInfo?.engines || []).find((e) => Object.keys(e.languages || {}).length)?.languages}
            cadences={meta.voice_cadences} onError={setError}
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

        {tab === 'automation' && (() => {
          // Automation is browsed by scope, the same way characters are: a
          // Global pill (the baseline every style inherits) then the style
          // hierarchy. Per-FILM flags — what gets written, critiqued, sung and
          // started — resolve per style; comment polling, AI-idea top-ups and
          // publishing are queue- or channel-wide, so they only show on Global.
          const knownStyles = new Set(styles.map((s) => s.name))
          const scope = autoScope && knownStyles.has(autoScope) ? autoScope : ''
          const overridesOf = (name) => (styles.find((s) => s.name === name)?.automation) || {}
          const own = scope ? overridesOf(scope) : {}
          // What this scope resolves to, and what it would fall back to if it
          // dropped its own overrides — the pair every hint below is built from.
          const av = scope ? resolveAutomation(styles, scope, cfg) : globalAutomation(cfg)
          const inherited = scope ? resolveAutomation(styles, scope, cfg, -1) : {}
          const setAuto = (k, v) => {
            if (!scope) return set(AUTOMATION_FIELDS[k], v)
            // Inheritance is equality (as on the Styles tab): setting a flag
            // back to what it would inherit drops the override, so the style
            // keeps following its parent — and the global — live.
            editCfg((c) => ({
              ...c,
              styles: (c.styles || []).map((s) => {
                if (s.name !== scope) return s
                const next = { ...(s.automation || {}) }
                if (JSON.stringify(v ?? null) === JSON.stringify(inherited[k] ?? null)) delete next[k]
                else next[k] = v
                const { automation: _drop, ...rest } = s
                return Object.keys(next).length ? { ...rest, automation: next } : rest
              }),
            }))
          }
          const clearAuto = (k) => setAuto(k, inherited[k])
          // Auto-pick rotation membership is a STYLE-row field (auto_pick_exclude,
          // children inherit it through the style chain), surfaced here — positively,
          // as "include" — because it gates the same top-up loop as the flags below.
          const scopeRow = scope ? styles.find((s) => s.name === scope) : null
          const pickInheritedExclude = !!((scopeRow?.parent
            ? (resolveStyle(styles, scopeRow.parent) || {}).auto_pick_exclude
            : undefined) ?? cfg.default_auto_pick_exclude)
          const pickExclude = scopeRow && 'auto_pick_exclude' in scopeRow
            ? !!scopeRow.auto_pick_exclude : pickInheritedExclude
          const setAutoPick = (include) => editCfg((c) => ({
            ...c,
            styles: (c.styles || []).map((s) => {
              if (s.name !== scope) return s
              // Inheritance is equality (as on the Styles tab): a child set back
              // to its parent's effective value stores nothing and follows live.
              const drop = s.parent && JSON.stringify(!include) ===
                JSON.stringify((resolveStyle(c.styles || [], s.parent) || {}).auto_pick_exclude ?? null)
              if (drop) { const { auto_pick_exclude: _x, ...rest } = s; return rest }
              return { ...s, auto_pick_exclude: !include }
            }),
          }))
          const clearAutoPick = () => editCfg((c) => ({
            ...c,
            styles: (c.styles || []).map((s) => {
              if (s.name !== scope) return s
              const { auto_pick_exclude: _x, ...rest } = s
              return rest
            }),
          }))
          const PickVal = () => {
            if (!scope) return null
            const from = scopeRow?.parent ? `“${scopeRow.parent}”` : 'Global'
            if (!(scopeRow && 'auto_pick_exclude' in scopeRow)) {
              return <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Follows {from}.</div>
            }
            return (
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {from}: <a role="button" tabIndex={0} title={`Use ${from}’s value again`}
                  style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
                  onClick={clearAutoPick}><em>{pickInheritedExclude ? 'not included' : 'included'}</em></a> — click to use it.
              </div>
            )
          }
          const fmtAutoVal = (k, v) => {
            if (k === 'auto_format') return { narration: 'Narration', dialogue: 'Dialogue', mixed: 'Mixed', silent: 'Silent', song: 'Music video' }[v] || String(v)
            if (k === 'auto_song_voice') return v ? String(v) : 'the model’s own vocalist'
            if (k === 'auto_critic_passes') return Number(v) ? `${v} passes` : 'until stable'
            if (k === 'auto_song_critic_passes') return Number(v) ? `${v} passes` : 'off'
            return v ? 'on' : 'off'
          }
          // Per-flag inheritance hint, mirroring the Styles tab's ParentVal:
          // says where the value comes from, and offers the inherited one back.
          const AutoVal = ({ k }) => {
            if (!scope) return null
            const src = automationSource(styles, scope, k)
            const from = src || 'Global'
            if (!(k in own)) return <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Follows {src ? `“${src}”` : 'Global'}.</div>
            return (
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {src ? `“${src}”` : 'Global'}: <a role="button" tabIndex={0} title={`Use ${from}’s value again`}
                  style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
                  onClick={() => clearAuto(k)}><em>{fmtAutoVal(k, inherited[k])}</em></a> — click to use it.
              </div>
            )
          }
          const pill = (key, label, { icon, depth = 0 } = {}) => {
            const n = Object.keys(key ? overridesOf(key) : {}).length
            return (
              <Button variant={scope === key ? 'primary' : 'ghost'} icon={icon}
                title={key === '' ? 'The baseline every style inherits' : `${n} override${n === 1 ? '' : 's'}`}
                onClick={() => setAutoScope(key)}>
                {`${depth ? '↳ ' : ''}${label}`}
                {n > 0 && <span style={{ opacity: 0.55, fontWeight: 500 }}>{n}</span>}
              </Button>
            )
          }
          return (<>
          {/* ── Scope picker ── */}
          <Card span={12} className="reveal reveal-d1">
            <div className="row center between">
              <span className="label-sm">Automation</span>
              <span className="muted" style={{ fontSize: 11.5 }}>Global is the baseline; a style overrides only what it changes, and styles under it inherit that.</span>
            </div>
            {styles.some((s) => s.parent) ? (() => {
              const cells = []
              let row = 0   // row 0 = the Global pill
              styleTreeOrder(styles).forEach((o, j, arr) => {
                if (j === 0 || o.depth !== arr[j - 1].depth + 1) row++
                cells.push({ ...o, row })
              })
              const cols = cells.reduce((m, c) => Math.max(m, c.depth), 0) + 1
              return (
                <div className="mt-16" style={{ overflowX: 'auto' }}>
                  <div style={{ display: 'inline-grid', gridTemplateColumns: `repeat(${cols}, max-content)`, gap: 6, alignItems: 'center' }}>
                    <div style={{ gridRow: 1, gridColumn: 1 }}>{pill('', 'Global', { icon: 'globe' })}</div>
                    {cells.map((cell) => (
                      <div key={cell.style.name} style={{ gridRow: cell.row + 1, gridColumn: cell.depth + 1 }}>
                        {pill(cell.style.name, cell.style.name, { depth: cell.depth })}
                      </div>
                    ))}
                  </div>
                </div>
              )
            })() : (
              <div className="row gap-6 row--wrap mt-16">
                <div>{pill('', 'Global', { icon: 'globe' })}</div>
                {styles.map((s) => <div key={s.name}>{pill(s.name, s.name)}</div>)}
              </div>
            )}
            <div className="field__hint" style={{ marginTop: 12 }}>
              {scope === ''
                ? <>These are the settings every style automates by, unless it says otherwise. Comment fetching, AI-idea top-ups and publishing are queue- and channel-wide, so they live here only.</>
                : (<>
                  {styleLineage(styles, scope).slice(0, -1).map((a) => (
                    <span key={a.name}>
                      <a role="button" tabIndex={0} title={`Show “${a.name}”`}
                        style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
                        onClick={() => setAutoScope(a.name)}>{a.name}</a>{' ▸ '}
                    </span>
                  ))}
                  <strong>{scope}</strong> — how automation treats this style's films. Everything follows Global until you change it here.
                </>)}
            </div>
          </Card>

          {/* ── What automation makes: per-film flags, resolved per style.
               The style's default format (which also seeds the Create screen's
               picker), with the music-video steps unfolding under it. ── */}
          <Card span={12} className="reveal reveal-d1">
            <span className="label-sm">What automation makes{scope ? ` · ${scope}` : ''}</span>
            <div className="stack gap-16 mt-16">
              <div>
                <Check checked={!!av.auto_write_scripts} onChange={(v) => setAuto('auto_write_scripts', v)} label="Auto-write scripts for queued items but don't render — they wait unapproved for you to review, edit and approve" />
                <AutoVal k="auto_write_scripts" />
              </div>
              <div>
                <Check checked={!!av.auto_approve_script} onChange={(v) => setAuto('auto_approve_script', v)} label="Auto-approve scripts — also write missing scripts and render them without review" />
                <AutoVal k="auto_approve_script" />
              </div>
              <div>
                <Check checked={!!av.auto_start_job} onChange={(v) => setAuto('auto_start_job', v)} label="Auto-start the next queue item with a ready script — loops until the queue is empty" />
                <AutoVal k="auto_start_job" />
              </div>
              <div>
                <Check checked={!!av.auto_ai_ideas} onChange={(v) => setAuto('auto_ai_ideas', v)} label={`Top up the empty queue with AI ideas for this style — invented films render without review, so the style also needs auto-approve and auto-start on (and to be included in auto-pick${scope ? ', below' : ' — a per-style switch on each style’s scope'})`} />
                <AutoVal k="auto_ai_ideas" />
              </div>
              {scope && (
                <div>
                  <Check checked={!pickExclude} onChange={setAutoPick}
                    label="Include in auto-picked ideas — let queue top-ups invent films in this style (unticked, the style is manual-only; the AI ideas screen still offers it)" />
                  <PickVal />
                </div>
              )}
              <div>
                <Check checked={!!av.auto_critic} onChange={(v) => setAuto('auto_critic', v)} label="Run the script critic on every automation-written script — QC for consistency, repetition and engagement (may rewrite, delete, add or reorder scenes) before it can render" />
                <AutoVal k="auto_critic" />
              </div>
              {!!av.auto_critic && (
                <div style={{ paddingLeft: 26 }}>
                  <div className="row center gap-10">
                    <span className="muted" style={{ fontSize: 12.5 }}>Critic passes</span>
                    <select className="select" value={String(av.auto_critic_passes ?? 0)}
                      onChange={(e) => setAuto('auto_critic_passes', Number(e.target.value))} style={{ maxWidth: 180 }}>
                      <option value="0">Until stable (≤5)</option>
                      <option value="1">1 pass</option>
                      <option value="2">2 passes</option>
                      <option value="3">3 passes</option>
                      <option value="5">5 passes</option>
                    </select>
                  </div>
                  <AutoVal k="auto_critic_passes" />
                </div>
              )}
              <div>
                {/* The default format itself lives on the Styles tab (it shapes
                    the style's films, not just automation); the Global baseline
                    has no style row to live on, so it stays editable here. */}
                {scope ? (
                  <Field label="Default format">
                    <div className="muted" style={{ fontSize: 13 }}>
                      This style films <strong>{fmtAutoVal('auto_format', av.auto_format)}</strong> by default — set it in
                      the <a role="button" tabIndex={0}
                        style={{ cursor: 'pointer', textDecoration: 'underline', textUnderlineOffset: 2 }}
                        onClick={() => { const i = styles.findIndex((s) => s.name === scope); if (i >= 0) setStyleIdx(i); setTab('styles') }}>Styles tab</a>,
                      alongside its length and visual style.{av.auto_format === 'song' ? ' Music video unfolds the song steps below.' : ''}
                    </div>
                  </Field>
                ) : (
                  <>
                    <Field label="Default format"
                      hint="The baseline: what a style films unless it sets its own default format in the Styles tab. The Create screen starts on the style's resolved default, AI ideas are pitched to suit it, and unattended films are written in it.">
                      <Segmented value={av.auto_format || 'narration'}
                        onChange={(v) => setAuto('auto_format', v)}
                        options={[{ value: 'narration', label: 'Narration' }, { value: 'dialogue', label: 'Dialogue' },
                                  { value: 'mixed', label: 'Mixed' }, { value: 'silent', label: 'Silent' },
                                  { value: 'song', label: 'Music video' }]} />
                    </Field>
                    <AutoVal k="auto_format" />
                  </>
                )}
              </div>
              {av.auto_format === 'song' && (<>
                <div>
                  <Check checked={!!av.auto_song} onChange={(v) => setAuto('auto_song', v)}
                    label="Write and generate the song before the story — the scenes are then timed against the real track and each take sings its own stretch of it (off = the song is only made at render time, and the takes have nothing to sing to)" />
                  <AutoVal k="auto_song" />
                </div>
                {!!av.auto_song && (<>
                  <div style={{ paddingLeft: 26 }}>
                    <div className="row center gap-10">
                      <span className="muted" style={{ fontSize: 12.5 }}>Song critic</span>
                      <select className="select" value={String(av.auto_song_critic_passes ?? 0)}
                        onChange={(e) => setAuto('auto_song_critic_passes', Number(e.target.value))} style={{ maxWidth: 220 }}>
                        <option value="0">Off — sing the first draft</option>
                        <option value="1">1 pass</option>
                        <option value="2">2 passes</option>
                        <option value="3">3 passes</option>
                      </select>
                      <span className="muted" style={{ fontSize: 12.5 }}>QC the lyrics — length, singability, hook — before the track is rendered</span>
                    </div>
                    <AutoVal k="auto_song_critic_passes" />
                  </div>
                  <div style={{ paddingLeft: 26 }}>
                    <div className="row center gap-10">
                      <span className="muted" style={{ fontSize: 12.5 }}>Singing voice</span>
                      <select className="select" value={av.auto_song_voice || ''}
                        onChange={(e) => setAuto('auto_song_voice', e.target.value)} style={{ maxWidth: 260 }}>
                        <option value="">The model’s own vocalist</option>
                        {(meta.voices || []).filter((v) => v !== 'Default (F5-TTS)').map((v) => (
                          <option key={v} value={v}>{voiceLabel(v, voiceMetaMap(cfg.voices))}</option>
                        ))}
                      </select>
                      <span className="muted" style={{ fontSize: 12.5 }}>Described to the music model (gender, age, tone) — not cloned</span>
                    </div>
                    <AutoVal k="auto_song_voice" />
                  </div>
                  <div>
                    <Check checked={!!av.auto_song_revoice} disabled={!av.auto_song_voice}
                      onChange={(v) => setAuto('auto_song_revoice', v)}
                      label="Re-voice the finished track as that voice (voice conversion, runs on the controller) — the sung original is kept as a version either way" />
                    <AutoVal k="auto_song_revoice" />
                  </div>
                  <div>
                    <Check checked={!!av.auto_song_approve} onChange={(v) => setAuto('auto_song_approve', v)}
                      label="Auto-approve songs — carry straight on into the story. Off, automation stops once the song exists and parks it in the Song tab, so no film is built on a song you haven't heard" />
                    <AutoVal k="auto_song_approve" />
                  </div>
                </>)}
              </>)}
            </div>
          </Card>

          {scope !== '' ? null : (<>
          {/* ── YouTube automation — queue- and channel-wide, Global only ── */}
          <Card span={12} className="reveal reveal-d1">
            <span className="label-sm">YouTube automation</span>
            <div className="stack gap-16 mt-16">
              <Check checked={fullyAutomated} onChange={setFullyAutomated} label="⚡ Fully automated mode — turns on every global step, here and above" />
              <Check checked={!!cfg.youtube_auto_fetch_evaluate} onChange={(v) => set('youtube_auto_fetch_evaluate', v)} label="Fetch & evaluate comments on a schedule" />
              <Field label="Minutes between comment sweeps (each sweep spends YouTube API quota per channel; also paces X mentions)">
                <input className="input" type="number" min={5} max={1440} step={5} value={cfg.comment_poll_minutes ?? 60}
                  onChange={(e) => set('comment_poll_minutes', Number(e.target.value) || 60)} style={{ width: 110 }} />
              </Field>
              <Check checked={!!cfg.youtube_auto_approve_comments} onChange={(v) => set('youtube_auto_approve_comments', v)} label="Auto-approve requests above the confidence threshold" />
              {/* The AI-ideas top-up itself is per style now — it lives in
                   "What automation makes" above, like the other per-film flags. */}
              <div className="row center between row--wrap gap-10">
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
          </>)
        })()}

      </div>
    </div>
  )
}
