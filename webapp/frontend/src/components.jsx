// Reusable primitives for the redesign — ported from the design system's
// components.jsx / App.jsx into ES modules.

import { useRef, useEffect, useState } from 'react'
import { fileUrl } from './api'
import { resolveStyle } from './styleUtils.js'

export function Icon({ name, brand, style, spin }) {
  return <i className={`${brand ? 'fa-brands' : 'fa-solid'} fa-${name}${spin ? ' fa-spin' : ''}`} style={style}></i>
}

// Voice characteristics for the pickers. Build a name → "gender, age, accent"
// map from the full voice catalogue (meta.config.voices / cfg.voices), then
// label any voice name with it. Voices without metadata just show their name.
export function voiceMetaMap(fullVoices) {
  const m = {}
  for (const v of (fullVoices || [])) {
    if (v && v.name) m[v.name] = [v.gender, v.age, v.accent].filter(Boolean).join(', ')
  }
  return m
}
export function voiceLabel(name, metaMap) {
  const c = metaMap && metaMap[name]
  return c ? `${name} — ${c}` : name
}

// ── Voice cadence (words/minute) — mirror of pipeline/cadence.py ─────────────
// Scenes are 10–15 s of narration (12 s target); legacy scene counts meant
// ~9 s each; unmeasured voices fall back to the server's default cadence.
// These are display estimates only — the backend plan is authoritative.
export const SCENE_TARGET_SECS = 12
export const LEGACY_SCENE_SECS = 9

// A voice's natural cadence from meta.voice_cadences ("<voice>|<engine>" keys).
export function voiceWpm(meta, voice, engine) {
  let name = (voice || '').trim()
  if (name.toLowerCase() === 'default (f5-tts)') name = ''
  const key = `${name || '__default__'}|${(engine || 'openf5').trim()}`
  const entry = (meta?.voice_cadences || {})[key]
  if (entry && Number(entry.wpm) > 0) return { wpm: Number(entry.wpm), measured: true }
  return { wpm: Number(meta?.default_wpm) || 150, measured: false }
}

// The cadence narration will actually play at for a resolved style: the target
// cadence when set, else the voice's natural pace × any legacy speed multiplier.
export function effectiveWpm(meta, eff, voiceOverride) {
  const voice = voiceOverride !== undefined && voiceOverride !== '' ? voiceOverride : (eff?.voice || '')
  const { wpm: nat, measured } = voiceWpm(meta, voice, eff?.tts_engine)
  const target = Number(eff?.voice_cadence_wpm || 0)
  if (target > 0) return { wpm: target, measured }
  const speed = Number(eff?.voice_speed || 1) || 1
  return { wpm: Math.round(nat * speed), measured }
}

// A style's effective target length in minutes (video_minutes, else what its
// legacy scene count used to produce).
export function styleMinutes(eff) {
  const m = Number(eff?.video_minutes || 0)
  if (m > 0) return m
  const n = parseInt(eff?.n_scenes, 10) || 6
  return Math.round((n * LEGACY_SCENE_SECS / 60) * 100) / 100
}

// ── Scene length — mirror of pipeline/cadence.py + pipeline/performance.py ───
// A scene runs SCENE_TARGET_SECS of narration, or one ~10 s take in a film
// made of clips (dialogue/silent). An explicit scene count divides the length
// instead, between the floor and what the engine renders in one take.
export const SCENE_FLOOR_SECS = 5
export const ACTED_SCENE_SECS = 10       // performance.SCENE_SECONDS
export const ACTED_CEIL_SECS = 12        // performance.MAX_SCENE_SECONDS
export const LTX_SCENE_CEIL_SECS = 40    // cadence.LTX_SCENE_CEIL_SECS
const CHAIN_CLIPS = 2                    // cadence.CHAIN_CLIPS
const CHAIN_JOIN_SECS = 22 / 24          // cadence.CHAIN_JOIN_SECS

// { secs, ceil } — the default and longest scene for a resolved style in a
// given format. Chaining joins two takes into one longer scene.
export function sceneBounds(eff, format = 'narration') {
  const chain = (v) => v * CHAIN_CLIPS - CHAIN_JOIN_SECS * (CHAIN_CLIPS - 1)
  const h3 = String(eff?.video_engine || '').startsWith('minimax-h3')
  const acted = format === 'dialogue' || format === 'silent' || format === 'song'
  const chained = !!eff?.h3_chain_scenes && (h3 || acted)
  if (acted) {
    return { secs: chained ? chain(ACTED_SCENE_SECS) : ACTED_SCENE_SECS,
             ceil: chained ? chain(ACTED_CEIL_SECS) : ACTED_CEIL_SECS }
  }
  return { secs: chained ? chain(SCENE_TARGET_SECS) : SCENE_TARGET_SECS,
           ceil: h3 ? (chained ? chain(ACTED_CEIL_SECS) : ACTED_CEIL_SECS) : LTX_SCENE_CEIL_SECS }
}

// How long one scene runs when *nScenes* of them share *minutes* — the length
// gives way at the bounds, exactly as cadence.plan_script does.
export function sceneSecsFor(minutes, nScenes, bounds) {
  const n = parseInt(nScenes, 10) || 0
  if (n <= 0) return bounds.secs
  const raw = Math.max(0.25, Number(minutes) || 0) * 60 / n
  return Math.min(Math.max(raw, SCENE_FLOOR_SECS), bounds.ceil)
}

// minutes × cadence → estimated words + scene count (pause-aware). *bounds*
// and *nScenes* re-centre it on a scene length the brief asked for.
export function lengthEstimate(minutes, wpm, sentencePause = 0, nScenes = 0,
                               bounds = { secs: SCENE_TARGET_SECS, ceil: LTX_SCENE_CEIL_SECS }) {
  const mins = Math.max(0.25, Number(minutes) || 0)
  const rate = Number(wpm) > 0 ? Number(wpm) : 150
  const gap = Math.max(0, Math.min(Number(sentencePause) || 0, 5))
  const secs = sceneSecsFor(mins, nScenes, bounds)
  const n = parseInt(nScenes, 10) || Math.max(1, Math.round(mins * 60 / secs))
  const wordsScene = Math.max(1, Math.round(rate * (secs - gap) / 60))
  return { nScenes: n, words: n * wordsScene, wordsScene, sceneSecs: secs,
           minutes: n * secs / 60 }
}

// Compact "≈ 300 words · 10 scenes of ~12 s" line for length pickers.
export function lengthEstimateLabel(minutes, wpm, sentencePause = 0, nScenes = 0,
                                    bounds = { secs: SCENE_TARGET_SECS, ceil: LTX_SCENE_CEIL_SECS }) {
  const est = lengthEstimate(minutes, wpm, sentencePause, nScenes, bounds)
  return `≈ ${est.words} words · ${est.nScenes} scene${est.nScenes === 1 ? '' : 's'} of ~${Math.round(est.sceneSecs)} s`
}

// "2 min 30 s" / "2 min" / "45 s" from a float-minutes value — lengths are
// stored as minutes but always DISPLAYED as a time.
export function fmtDuration(minutes) {
  const total = Math.round((Number(minutes) || 0) * 60)
  const m = Math.floor(total / 60)
  const s = total % 60
  if (!m) return `${s} s`
  return s ? `${m} min ${s} s` : `${m} min`
}

// Minutes + seconds inputs writing a single float-minutes value (the unit the
// config/API use). Clamped to 15 s .. maxMinutes.
export function DurationInput({ value, onChange, disabled, maxMinutes = 40 }) {
  const total = Math.round((Number(value) || 0) * 60)
  const m = Math.floor(total / 60)
  const s = total % 60
  const set = (mins, secs) => {
    const t = Math.max(15, Math.min(maxMinutes * 60, (Number(mins) || 0) * 60 + (Number(secs) || 0)))
    onChange(Math.round((t / 60) * 10000) / 10000)
  }
  return (
    <div className="row center gap-6">
      <input className="input" type="number" min={0} max={maxMinutes} value={m} disabled={disabled}
        onChange={(e) => set(e.target.value, s)} style={{ width: 74 }} />
      <span className="muted" style={{ fontSize: 12 }}>min</span>
      <input className="input" type="number" min={-5} max={60} step={5} value={s} disabled={disabled}
        onChange={(e) => set(m, e.target.value)} style={{ width: 74 }} />
      <span className="muted" style={{ fontSize: 12 }}>s</span>
    </div>
  )
}

// Scene-type controls shared by the Script editor and the video edit screen so
// they stay identical. Renders the Narration | Dialogue | Silent selector and,
// for dialogue, the shot sequence editor (speaking + silent shots); for silent,
// the duration — plus, when the style performs its silent beats, everything an
// acted scene has. `scene` = {mode, lines, duration}; onChange(patch) merges into
// it; onCommit() persists (called on blur and after discrete edits). The
// narration textarea stays in each parent (it owns its own regen wiring).
// An ACTED scene is written through its fields — who is on screen, where, what
// they say, when it happens — and the prompt the video model receives is
// assembled from them (ActedPrompt below shows it). Nothing is typed twice.
export const ACTED_MODES = ['dialogue', 'performance']
export const isActedMode = (m) => ACTED_MODES.includes(m || '')

// Does this scene get the ACTED setup on screen? A dialogue scene always; a
// silent one when the style performs its silent beats on H3 (h3_silent_scenes)
// — the take is then built from the very same fields, so the editor is the same
// minus the dialogue. A SINGING scene (a music-video film's beat) is acted by
// its own metadata, whatever the style says. Mirrors performance.renders_acted
// on the backend.
export const hasActedShape = (mode, actedSilent, singing = false) =>
  isActedMode(mode) || (mode === 'silent' && (!!actedSilent || !!singing))

function BeatRows({ beats, seconds, onChange, onCommit }) {
  const set = (i, k, v) => onChange(beats.map((b, j) => (i === j ? { ...b, [k]: v } : b)))
  return (
    <div className="stack gap-8">
      {beats.map((b, i) => (
        <div key={i} className="row gap-8 center">
          <input className="input" type="number" min={0} max={seconds} step={0.5} style={{ width: 78 }}
            value={b.t0 ?? 0} onChange={(e) => set(i, 't0', Number(e.target.value))} onBlur={onCommit} />
          <span className="muted" style={{ fontSize: 12 }}>→</span>
          <input className="input" type="number" min={0} max={seconds} step={0.5} style={{ width: 78 }}
            value={b.t1 ?? seconds} onChange={(e) => set(i, 't1', Number(e.target.value))} onBlur={onCommit} />
          <span className="muted" style={{ fontSize: 12 }}>s</span>
          <input className="input" style={{ flex: 1 }} placeholder="What happens in that window"
            value={b.action || ''} onChange={(e) => set(i, 'action', e.target.value)} onBlur={onCommit} />
          <Button variant="quiet" icon="trash" title="Remove beat"
            onClick={() => onChange(beats.filter((_, j) => j !== i))} />
        </div>
      ))}
      <div>
        <Button variant="ghost" size="sm" icon="plus"
          onClick={() => onChange([...beats, { t0: 0, t1: seconds, action: '' }])}>Add beat</Button>
      </div>
    </div>
  )
}

export function SceneTypeControls({ scene = {}, castOpts = [], actedSilent = false,
  isFirst = false, onChange, onCommit, onConvert }) {
  // onChange(patch, commit): merge patch into the scene; when commit is true the
  // parent persists the merged value immediately (discrete edits). Text inputs
  // pass commit=false and persist on blur via onCommit(). Passing the computed
  // patch (rather than a deferred callback) avoids a stale-closure save.
  const mode = scene.mode || 'narration'
  const lines = Array.isArray(scene.lines) ? scene.lines : []
  const cast = Array.isArray(scene.cast) ? scene.cast : []
  // Older scripts can carry beats as one prose string — never crash on it.
  const beats = Array.isArray(scene.beats) ? scene.beats
    : scene.beats ? [{ action: String(scene.beats) }] : []
  // A dialogue scene's length is counted from its words; a silent one's is
  // authored, and lives in `duration` — the beats have to sit inside whichever
  // of the two this scene actually runs for.
  const silent = mode === 'silent'
  const actedShape = hasActedShape(mode, actedSilent, scene.singing)
  const seconds = Math.round((silent ? scene.duration || scene.seconds || 5
    : scene.seconds) || 10)
  const commit = () => onCommit && onCommit()
  const setLine = (i, k, v, doCommit = false) =>
    onChange({ lines: lines.map((ln, idx) => idx === i ? { ...ln, [k]: v } : ln) }, doCommit)
  const addLine = () => onChange(
    { lines: [...lines, { speaker: cast[0] || castOpts[0] || '', delivery: 'even, natural', text: '' }] }, true)
  const removeShot = (i) => onChange({ lines: lines.filter((_, idx) => idx !== i) }, true)
  const moveShot = (i, di) => {
    const j = i + di
    if (j < 0 || j >= lines.length) return
    const next = [...lines]
    ;[next[i], next[j]] = [next[j], next[i]]
    onChange({ lines: next }, true)
  }
  const [converting, setConverting] = useState(false)
  const setMode = async (m) => {
    if (m === mode || (m === 'dialogue' && isActedMode(mode))) return
    if (onConvert) {
      // Conversion keeps the theme and the old version: the server rewrites
      // the content into the other shape (or restores the stashed one).
      setConverting(true)
      try { await onConvert(m) } finally { setConverting(false) }
    } else {
      onChange({ mode: m }, true)
    }
  }
  const speakerOpts = [...new Set([...cast, ...castOpts].filter(Boolean))]

  return (
    <>
      <Field label="Scene type"
        hint={onConvert
          ? 'Switching converts the scene — same theme and feel, the other shape — and keeps the version you leave: switch back and it is restored, not redone.'
          : 'Narration = voice-over over a still that moves. Dialogue = the characters act and speak on camera, in one take. Silent = visuals only, no voice.'}>
        <div className="row gap-8 center">
          {/* A singing scene IS silent under the hood (no lines, take muted),
              but it should say what it is: a music-video beat. */}
          {[['narration', 'Narration'], ['dialogue', 'Dialogue'],
            ['silent', scene.singing ? '♪ Music video' : 'Silent']].map(([m, lbl]) => (
            <Button key={m} variant={(m === 'dialogue' ? isActedMode(mode) : mode === m) ? 'primary' : 'ghost'}
              disabled={converting} onClick={() => setMode(m)}>{lbl}</Button>
          ))}
          {converting && <span className="muted" style={{ fontSize: 12.5 }}><Icon name="spinner" spin /> Converting…</span>}
        </div>
      </Field>
      {silent && scene.singing && (
        <Banner tone="info">♪ A music-video beat — the cast performs the film's song on
          camera (visibly singing, take shipped muted); the sung track is the film's
          audio. Edit the words in the Song tab.</Banner>
      )}

      {/* Cross-scene continuation: the render picks this shot up from the
          previous scene (motion context on acted takes, closing frame on
          narrated ones) and joins them without a fade. Not on the first scene
          (nothing to continue) or singing beats (their take holds the song's
          exact timeline). */}
      {!isFirst && !scene.singing && (
        <Check checked={!!scene.continues_previous}
          onChange={(v) => onChange({ continues_previous: v }, true)}
          label="Continues the previous scene — pick up its shot without a cut (the two render in sequence)" />
      )}

      {actedShape && (
        <>
          <Field label="On screen"
            hint={silent
              ? 'Who appears in this silent beat, in order. The first is Picture 1, the second Picture 2 — their portraits are wired in alongside the scene\'s own first frame, so a face here matches the acted scenes. Leave it empty for a beat with nobody in it. Keep it to two.'
              : 'Who appears, in order. The first is Picture 1, the second Picture 2 — the model is told each one defines that person\'s face and nothing else. Keep it to two: a third face is where they start swapping.'}>
            <div className="row gap-8 row--wrap">
              {castOpts.map((n) => {
                const on = cast.includes(n)
                return (
                  <Button key={n} variant={on ? 'primary' : 'ghost'} size="sm"
                    onClick={() => onChange({ cast: on ? cast.filter((c) => c !== n) : [...cast, n] }, true)}>
                    {on ? `${cast.indexOf(n) + 1}. ${n}` : n}
                  </Button>
                )
              })}
              {!castOpts.length && <span className="muted" style={{ fontSize: 12.5 }}>No characters yet — add them in Characters.</span>}
            </div>
          </Field>

          <Field label="Setting"
            hint={silent
              ? 'Where this happens and what is around them — the scenery the take is built in. Leave it empty and the scene\'s video prompt stands in.'
              : 'Where this happens and what is around them. This is the scenery the model builds — there is no separate first frame for an acted scene.'}>
            <textarea className="textarea" rows={2} value={scene.setting || ''}
              onChange={(e) => onChange({ setting: e.target.value }, false)} onBlur={commit} />
          </Field>

          {silent ? (
            <Field label="Duration (seconds)"
              hint="How long the take runs — and the window the beats below sit in. H3 holds this to 5–12 s, or up to ~23 s with Chained scenes on.">
              <input className="input" type="number" min={1} step={1} style={{ maxWidth: 120 }}
                value={scene.duration || 5}
                onChange={(e) => {
                  // Both, always: the renderer sizes an acted take from
                  // `seconds` and would otherwise hold a stale length.
                  const v = Number(e.target.value) || 0
                  onChange({ duration: v, seconds: v }, false)
                }} onBlur={commit} />
            </Field>
          ) : (
          <Field label="Dialogue" hint="One continuous take: each line is acted in that character's own voice. About 2.5 words a second, so roughly 22 words fit in a ten-second scene.">
            <div className="stack gap-10">
              {lines.length === 0 && <span className="muted" style={{ fontSize: 12.5 }}>Nobody speaks — the scene plays silent.</span>}
              {lines.map((ln, i) => (
                <div key={i} className="stack gap-8"
                  style={{ border: '1px solid var(--line)', borderRadius: 'var(--r-md)', padding: 10, background: 'var(--paper-2)' }}>
                  <div className="row center between">
                    <Chip tone="accent" dot>Line {i + 1}</Chip>
                    <div className="row center gap-4">
                      <Button variant="quiet" icon="chevron-up" title="Move up" disabled={i === 0} onClick={() => moveShot(i, -1)} />
                      <Button variant="quiet" icon="chevron-down" title="Move down" disabled={i === lines.length - 1} onClick={() => moveShot(i, 1)} />
                      <Button variant="quiet" icon="trash" title="Remove line" onClick={() => removeShot(i)} />
                    </div>
                  </div>
                  <div className="row gap-8 center">
                    <select className="input" style={{ maxWidth: 170 }} value={ln.speaker || ''}
                      onChange={(e) => setLine(i, 'speaker', e.target.value, true)}>
                      {!speakerOpts.length && <option value="">(no characters yet)</option>}
                      {ln.speaker && !speakerOpts.includes(ln.speaker) && <option value={ln.speaker}>{ln.speaker} (unknown)</option>}
                      {speakerOpts.map((n) => <option key={n} value={n}>{n}</option>)}
                    </select>
                    <input className="input" style={{ maxWidth: 170 }} placeholder="delivery — e.g. quiet, certain"
                      value={ln.delivery || ''} onChange={(e) => setLine(i, 'delivery', e.target.value)} onBlur={commit} />
                  </div>
                  <textarea className="textarea" rows={2} placeholder="What they say…"
                    value={ln.text || ''} onChange={(e) => setLine(i, 'text', e.target.value)} onBlur={commit} />
                </div>
              ))}
              <div><Button variant="ghost" icon="plus" onClick={addLine}>Add line</Button></div>
            </div>
          </Field>
          )}

          <Field label={`Action — ${seconds}s`}
            hint="Timed beats inside the take. These reach the model as [0s-4s] windows, so they are how you place a move at a moment.">
            <BeatRows beats={beats} seconds={seconds}
              onChange={(next) => onChange({ beats: next }, false)} onCommit={commit} />
          </Field>

          <div className="row gap-16 row--wrap">
            <Field label="Camera" hint="One continuous shot — describe the framing and at most one move.">
              <input className="input" style={{ minWidth: 260 }} value={scene.camera || ''}
                onChange={(e) => onChange({ camera: e.target.value }, false)} onBlur={commit} />
            </Field>
            <Field label="Sound" hint="Diegetic sound only — what is audible in the room. No score.">
              <input className="input" style={{ minWidth: 260 }} value={scene.soundscape || ''}
                onChange={(e) => onChange({ soundscape: e.target.value }, false)} onBlur={commit} />
            </Field>
          </div>
          {/* Wardrobe references (film visuals / catalogue outfits) normally
              dress the cast; this scene can opt back to the portraits' own
              clothes — a flashback or a costume change inside one film. */}
          <Check checked={!!scene.no_wardrobe}
            onChange={(v) => onChange({ no_wardrobe: v }, true)}
            label="Portrait clothes only — ignore wardrobe references in this scene" />
        </>
      )}

      {/* A silent scene the style ANIMATES from a first frame: no take is
          staged, so it keeps the plain length + cast pair. (When the style
          acts its silent scenes it gets the full setup above instead.) */}
      {silent && !actedShape && (
        <>
          <Field label="Duration (seconds)" hint="Silent scene — visuals only, no voice-over. The Image prompt and Video prompt drive the frame and motion.">
            <input className="input" type="number" min={1} step={1} style={{ maxWidth: 120 }}
              value={scene.duration || 5} onChange={(e) => onChange({ duration: Number(e.target.value) || 0 }, false)} onBlur={commit} />
          </Field>

          {/* Only meaningful for a style that PERFORMS its silent scenes
              (Settings → Video models → "Silent scenes — act them on H3 too"),
              where these portraits ride alongside the scene's first frame and
              keep a face the same as in the acted scenes. Harmless otherwise. */}
          <Field label="On screen"
            hint="Who appears in this silent beat. If the style acts its silent scenes, their portraits are wired in alongside the scene's own first frame — so a face here matches the acted scenes. Leave it empty for a beat with nobody in it. Keep it to two.">
            <div className="row gap-8 row--wrap">
              {castOpts.map((n) => {
                const on = cast.includes(n)
                return (
                  <Button key={n} variant={on ? 'primary' : 'ghost'} size="sm"
                    onClick={() => onChange({ cast: on ? cast.filter((c) => c !== n) : [...cast, n] }, true)}>
                    {on ? `${cast.indexOf(n) + 1}. ${n}` : n}
                  </Button>
                )
              })}
              {!castOpts.length && <span className="muted" style={{ fontSize: 12.5 }}>No characters yet — add them in Characters.</span>}
            </div>
          </Field>
        </>
      )}
    </>
  )
}

// The assembled prompt: read-only, because it is built from the fields above.
// "Edit" pins whatever you type — the fields stop rebuilding it until you
// rebuild. `refs` is the resolved slot list, so the numbers in the text mean
// something on screen.
export function ActedPrompt({ prompt, refs = [], audios = [], edited, busy, onSave, onRebuild,
  label = 'Video prompt' }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(prompt || '')
  useEffect(() => { setDraft(prompt || '') }, [prompt])

  return (
    // `label` names it apart from a scene that ALSO keeps a plain video prompt —
    // a performed silent one, where this is the take's own assembled prompt.
    <Field label={label}
      hint="Assembled from the fields above — cast, setting, dialogue, beats, camera, sound — so nothing is written twice.">
      {(refs.length > 0 || audios.length > 0) && (
        <div className="stack gap-4" style={{ marginBottom: 10 }}>
          {refs.map((r) => (
            <div key={`p${r.slot}`} className="muted" style={{ fontSize: 12 }}>
              <code>&lt;Picture {r.slot}&gt;</code> {r.name}
              {r.kind && r.kind !== 'character' ? ` — ${r.kind}` : ' — their face'}
            </div>
          ))}
          {audios.map((a) => (
            <div key={`a${a.slot}`} className="muted" style={{ fontSize: 12 }}>
              <code>&lt;Audio {a.slot}&gt;</code> {a.name} — their voice
            </div>
          ))}
        </div>
      )}
      {editing ? (
        <div className="stack gap-8">
          <textarea className="textarea mono" rows={14} style={{ fontSize: 12 }}
            value={draft} onChange={(e) => setDraft(e.target.value)} />
          <div className="row gap-8">
            <Button variant="primary" size="sm" disabled={busy || draft === prompt}
              onClick={() => { onSave(draft); setEditing(false) }}>Save prompt</Button>
            <Button variant="ghost" size="sm" disabled={busy}
              onClick={() => { setDraft(prompt || ''); setEditing(false) }}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div className="stack gap-8">
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, margin: 0, maxHeight: 260, overflowY: 'auto',
                        background: 'var(--well, rgba(127,127,127,.08))', padding: 12, borderRadius: 8 }}>
            {prompt || 'Fill in the fields above and the prompt appears here.'}
          </pre>
          <div className="row gap-8 center">
            <Button variant="ghost" size="sm" icon="pen" disabled={busy}
              onClick={() => setEditing(true)}>Edit prompt</Button>
            {edited && (
              <>
                <Chip tone="warn" dot>hand-edited</Chip>
                <Button variant="ghost" size="sm" disabled={busy}
                  onClick={onRebuild}>Rebuild from the fields</Button>
              </>
            )}
          </div>
        </div>
      )}
    </Field>
  )
}

export function Button({ variant = 'ghost', size, block, icon, iconRight, brand, children, onClick, disabled, type = 'button', title }) {
  const cls = ['btn', `btn--${variant}`, size === 'lg' ? 'btn--lg' : size === 'sm' ? 'btn--sm' : '', block ? 'btn--block' : '']
    .filter(Boolean).join(' ')
  return (
    <button type={type} className={cls} onClick={onClick} disabled={disabled} title={title}>
      {icon ? <Icon name={icon} brand={brand} /> : null}
      {children}
      {iconRight ? <Icon name={iconRight} brand={brand} /> : null}
    </button>
  )
}

export function Chip({ tone = 'neutral', dot, children }) {
  return <span className={`chip chip--${tone}`}>{dot ? <span className="chip__dot"></span> : null}{children}</span>
}

export function Card({ span, rowSpan, well, padLg, link, onClick, href, className = '', children, style, id }) {
  const cls = ['card', link ? 'card-link' : '', well ? 'card--well' : '', padLg ? 'card--pad-lg' : '',
    span ? `col-${span}` : '', rowSpan ? 'row-2' : '', className].filter(Boolean).join(' ')
  if (href) return <a className={cls} id={id} href={href} target="_blank" rel="noopener" style={style}>{children}</a>
  return <div className={cls} id={id} onClick={onClick} style={style}>{children}</div>
}

// A "tell it how" popover: a small caret button beside a Re-generate control that
// opens a text box for a one-off regeneration instruction — "shorten it", "make it
// all robots". Calls onSubmit(instruction) then closes. `chips` are optional quick
// presets; `align` places the popover to the 'left' or 'right' of the caret.
export function RegenGuide({ busy, disabled, onSubmit, chips = [],
  placeholder = 'e.g. shorten it, make it all robots…', align = 'right' }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const wrapRef = useRef(null)
  const inputRef = useRef(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    const t = setTimeout(() => inputRef.current?.focus(), 0)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
      clearTimeout(t)
    }
  }, [open])

  const submit = () => { onSubmit((text || '').trim()); setText(''); setOpen(false) }

  return (
    <span className="regen-guide" ref={wrapRef}>
      <button type="button" className="btn btn--quiet regen-guide__caret"
        aria-label="Re-generate with an instruction" title="Re-generate with an instruction"
        disabled={disabled}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); setOpen((v) => !v) }}>
        <Icon name="chevron-down" />
      </button>
      {open && (
        <div className={`regen-guide__pop regen-guide__pop--${align}`} onClick={(e) => e.stopPropagation()}>
          <div className="regen-guide__title">Tell it how…</div>
          <textarea ref={inputRef} className="textarea regen-guide__input" rows={2} value={text}
            placeholder={placeholder} onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit() } }} />
          {chips.length ? (
            <div className="regen-guide__chips">
              {chips.map((c) => (
                <button key={c} type="button" className="regen-guide__chip" onClick={() => setText(c)}>{c}</button>
              ))}
            </div>
          ) : null}
          <div className="regen-guide__actions">
            <button type="button" className="btn btn--primary btn--sm" disabled={busy}
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); submit() }}>
              <Icon name="rotate" /> Re-generate
            </button>
          </div>
        </div>
      )}
    </span>
  )
}

// A field label with an inline "Re-generate" button on the right (issue #88) plus
// a companion caret that opens the "tell it how" instruction popover. `busy`
// disables the button and swaps in `busyLabel`; `onRegen(instruction)` runs the
// LLM regeneration ('' for a plain click). Pass it as the `label` of a <Field>.
export function RegenLabel({ children, busy, disabled, onRegen, icon,
  label = 'Re-generate', busyLabel = 'Writing…', chips }) {
  return (
    <span className="row center between">
      <span className="row center gap-10">{icon ? <Icon name={icon} style={{ color: 'var(--ink-3)', width: 16 }} /> : null}{children}</span>
      <span className="regen-split">
        <button type="button" className="btn btn--quiet regen-split__main"
          disabled={busy || disabled} onClick={(e) => { e.preventDefault(); e.stopPropagation(); onRegen('') }}>
          <Icon name="rotate" /> {busy ? busyLabel : label}
        </button>
        <RegenGuide busy={busy} disabled={busy || disabled} onSubmit={(instr) => onRegen(instr)} chips={chips} />
      </span>
    </span>
  )
}

// A Re-generate button with a companion "tell it how" caret, for the plain-button
// regen sites (scene image, cover, character look, film image/video re-render).
// A plain click runs onRegen(''); the caret popover runs onRegen(instruction).
export function GuidedRegenButton({ label, busyLabel, busy, disabled, icon = 'rotate-right',
  variant = 'ghost', size, block, onRegen, chips, align = 'right', placeholder }) {
  const sizeCls = size === 'sm' ? ' btn--sm' : size === 'lg' ? ' btn--lg' : ''
  return (
    <span className={`regen-split${block ? ' regen-split--block' : ''}`}>
      <button type="button" className={`btn btn--${variant}${sizeCls}${block ? ' regen-split__grow' : ''}`}
        disabled={busy || disabled} onClick={(e) => { e.preventDefault(); e.stopPropagation(); onRegen('') }}>
        <Icon name={icon} /> {busy ? (busyLabel || label) : label}
      </button>
      <RegenGuide busy={busy} disabled={busy || disabled} onSubmit={(instr) => onRegen(instr)}
        chips={chips} align={align} placeholder={placeholder} />
    </span>
  )
}

export function Field({ label, hint, children }) {
  return (
    <div className="field">
      {label ? <label className="field__label">{label}</label> : null}
      {children}
      {hint ? <div className="field__hint">{hint}</div> : null}
    </div>
  )
}

// Compact dropdown for list filters (channel, style, …). '' = no filter; the
// empty option wears `allLabel` ("All channels"). Options are strings or
// {value, label} pairs, mirroring Segmented.
export function FilterSelect({ value, onChange, options, allLabel = 'All' }) {
  return (
    <select className="select" style={{ width: 'auto', padding: '6px 10px', fontSize: 13 }}
      value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{allLabel}</option>
      {options.map((o) => (
        <option key={o.value ?? o} value={o.value ?? o}>{o.label ?? o}</option>
      ))}
    </select>
  )
}

export function Segmented({ options, value, onChange, className = '' }) {
  return (
    <div className={`seg ${className}`.trim()}>
      {options.map((o) => (
        <button key={o.value ?? o} className={`seg__opt ${(o.value ?? o) === value ? 'is-active' : ''}`}
          onClick={() => onChange(o.value ?? o)}>{o.label ?? o}</button>
      ))}
    </div>
  )
}

// Compose the backend resolution name string for an (orientation, pixel tier)
// pair, picked from the backend's own lists so the result is always a name it
// already understands. Names look like "<Orientation>[ <Label>] (<w>×<h>)"; the
// base tier has no label. Upscale-only tiers (QHD/4K) only exist in
// meta.upscale_resolutions — they are render TARGETS (render at the top render
// tier, then upscale), so the picker may offer them too. Returns "" if no
// matching name exists.
export function composeResolution(meta = {}, orientation, tierKey) {
  const tier = (meta.pixel_tiers || []).find((t) => t.key === tierKey)
  const label = tier ? tier.label : ''
  const prefix = label ? `${orientation} ${label} (` : `${orientation} (`
  return (meta.resolutions || []).find((n) => n.startsWith(prefix))
    || (meta.upscale_resolutions || []).find((n) => n.startsWith(prefix)) || ''
}

// Return the pixel-tier key (e.g. "fhd") of a resolution name string, or "" if
// it is not one of the known names. The base tier (no label) is matched last so
// a labelled name like "...HD (...)" is not mistaken for the base tier.
export function resolutionTier(meta = {}, name) {
  if (!name) return ''
  const tiers = meta.pixel_tiers || []
  const labelled = tiers.filter((t) => t.label)
  const base = tiers.filter((t) => !t.label)
  for (const t of [...labelled, ...base]) {
    const tag = t.label ? ` ${t.label} (` : ' ('
    if (name.includes(tag)) return t.key
  }
  return ''
}

// Resolution picker: an orientation toggle (Landscape/Portrait/Square) plus a
// pixels/quality toggle. Emits the same composed name string the backend uses
// (e.g. "Landscape FHD (1920×1080)").
export function ResolutionPicker({ value, onChange, meta = {} }) {
  const orientations = meta.orientations || []
  const tiers = meta.pixel_tiers || []

  // Parse the current value back into orientation + tier so the toggles reflect it.
  const current = (() => {
    for (const o of orientations) {
      for (const t of tiers) {
        if (value && composeResolution(meta, o, t.key) === value) return { orientation: o, tier: t.key }
      }
    }
    return { orientation: meta.default_orientation || '', tier: meta.default_pixels || '' }
  })()

  const emit = (orientation, tierKey) => {
    const name = composeResolution(meta, orientation, tierKey)
    if (name) onChange(name)
  }

  // QHD/4K are upscale-only targets: the film renders at the largest render
  // tier and a finishing upscale lifts it to the target — say so in place.
  const currentTier = tiers.find((t) => t.key === current.tier)
  const maxRenderTier = [...tiers].reverse().find((t) => !t.upscale_only)

  return (
    <div className="stack gap-6">
      <div className="row gap-10 row--wrap">
        <Segmented
          options={orientations.map((o) => ({ value: o, label: o }))}
          value={current.orientation}
          onChange={(o) => emit(o, current.tier)}
        />
        {/* Seven tiers outgrow narrow form columns; wrap rather than hide the
            largest ones behind the .seg control's invisible horizontal scroll. */}
        <Segmented
          className="seg--wrap"
          options={tiers.map((t) => ({ value: t.key, label: t.label || 'Standard' }))}
          value={current.tier}
          onChange={(k) => emit(current.orientation, k)}
        />
      </div>
      {currentTier?.upscale_only && (
        <div className="muted" style={{ fontSize: 12 }}>
          Renders at {maxRenderTier?.label || 'the largest render size'}, then
          upscales to {currentTier.label}.
        </div>
      )}
    </div>
  )
}

export function Check({ checked, onChange, label, disabled }) {
  return (
    <label className="check" style={disabled ? { opacity: 0.55, cursor: 'default' } : undefined}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange?.(e.target.checked)} />
      <span>{label}</span>
    </label>
  )
}

// Restyle: point an existing script at another style profile — or, under
// "No style", at a visual-style sentence of your own — without touching its
// content. Shared by the Script screen's Restyle modal and the film editor's
// "Restyle this film" card. `value` is { styleName, style, repaintCast };
// picking a style prefills its sentence and, like Create, locks the text while
// the style owns one (a style's look is set in Settings, so the two can never
// contradict each other on the prompt).
export const NO_STYLE = '(none)'   // must match app.NO_STYLE
export function RestyleForm({ styles = [], value, onChange, disabled }) {
  const profile = value.styleName && value.styleName !== NO_STYLE ? resolveStyle(styles, value.styleName) : null
  const locked = !!(profile?.visual_style || '').trim()
  const pick = (name) => {
    const resolved = name === NO_STYLE ? null : resolveStyle(styles, name)
    onChange({ ...value, styleName: name, style: resolved?.visual_style || '' })
  }
  return (
    <div className="stack gap-16">
      <Field label="Style">
        <select className="select" value={value.styleName} disabled={disabled} onChange={(e) => pick(e.target.value)}>
          {styles.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          <option value={NO_STYLE}>No style — write the look yourself</option>
        </select>
      </Field>
      <Field label="Visual style"
        hint={locked ? 'Set by the style — pick “No style” to write your own.'
          : 'Leads every scene\'s image prompt in place of the old style sentence.'}>
        <textarea className="textarea" rows={3} value={value.style} disabled={disabled || locked}
          placeholder="Cinematic 35mm, golden hour, painterly lighting"
          onChange={(e) => onChange({ ...value, style: e.target.value })} />
      </Field>
      <Check checked={!!value.repaintCast} disabled={disabled}
        onChange={(v) => onChange({ ...value, repaintCast: v })}
        label="Repaint the cast's looks in the new style (untick to keep uploaded portraits)" />
    </div>
  )
}

// A reference that comes from the shared catalogue: shown at the same level
// as the film's own, but read-only — it is shared across films, so it edits
// in Settings (Characters or Assets).
export function CatalogueRefCard({ name, kind, description, imageUrl, icon, editHint, voiceName, voiceUrl }) {
  return (
    <Card span={4} className="stack gap-10">
      <div className="row between center">
        <span className="label-sm">{kind}</span>
        <Chip tone="neutral" dot>catalogue</Chip>
      </div>
      {imageUrl
        ? <img src={imageUrl} alt={name}
            style={{ width: '100%', aspectRatio: '1', objectFit: 'cover', borderRadius: 10 }} />
        : <div style={{ width: '100%', aspectRatio: '1', borderRadius: 10, display: 'flex',
                        alignItems: 'center', justifyContent: 'center',
                        background: 'var(--well, rgba(127,127,127,.10))',
                        border: '1px dashed var(--line, #ccc)' }}>
            <Icon name={icon} style={{ color: 'var(--ink-3)', fontSize: 26 }} />
          </div>}
      <div>
        <span style={{ fontSize: 13.5, fontWeight: 600 }}>{name}</span>
        {description && <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{description}</div>}
      </div>
      {/* The audio reference is as real as the picture: this clip is what the
          character's voice is cloned from in every acted scene. */}
      {voiceName && (
        <div className="stack gap-4">
          <span className="label-sm">Voice · {voiceName}</span>
          {voiceUrl && <audio controls preload="none" src={voiceUrl} style={{ width: '100%', height: 32 }} />}
        </div>
      )}
      <span className="muted" style={{ fontSize: 11.5 }}>
        Shared across films — edit in <strong>{editHint}</strong>.
      </span>
    </Card>
  )
}

export function ProgressBar({ pct }) {
  return <div className="pbar"><div className="pbar__fill" style={{ width: `${Math.max(0, Math.min(100, pct))}%` }}></div></div>
}

// A scene thumbnail: a real preview image if we have one, else a colored
// gradient placeholder (or a shimmer skeleton while rendering).
export function Thumb({ variant = 0, loading, label, src, aspect }) {
  return (
    <div className="scene__img" style={aspect ? { aspectRatio: aspect, background: 'var(--paper-2)' } : undefined}>
      {src
        ? <img src={src} alt="" style={{ width: '100%', height: '100%', objectFit: aspect ? 'contain' : 'cover', position: 'absolute', inset: 0 }} />
        : <div className={`gfill ${loading ? 'skel' : 'g' + (variant % 6)}`}></div>}
      {label ? <span className="scene__no">{label}</span> : null}
      {loading ? <div className="scene__state">rendering…</div> : null}
    </div>
  )
}

// Tiny × badge on a version thumbnail: first click arms it (turns red), second
// click confirms the delete; leaving the thumb disarms. Rendered as a sibling of
// the thumb button (a nested <button> would be invalid HTML).
function VersionDeleteBadge({ armed, busy, onArm, onConfirm }) {
  return (
    <button type="button" disabled={busy}
      title={armed ? 'Click again to delete' : 'Delete this version'}
      onClick={() => { if (busy) return; armed ? onConfirm() : onArm() }}
      style={{
        position: 'absolute', right: 3, top: 3, width: 16, height: 16, borderRadius: '50%',
        border: 'none', padding: 0, fontSize: 9, cursor: 'pointer',
        background: armed ? 'var(--danger)' : 'rgba(20,22,24,.62)', color: '#fff',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
      <Icon name={armed ? 'trash-can' : 'xmark'} />
    </button>
  )
}

// Browse kept image regenerations and click to pick the one in use. Hidden when
// there's nothing to compare (fewer than two versions). `versions` items are
// { id, path }; `selected` is the id currently in use; `onSelect(id)` switches;
// `onDelete(id)` (optional) enables a confirm-armed × on the unused versions.
export function VersionStrip({ versions, selected, onSelect, onDelete, aspect = '16 / 9', busy }) {
  const [confirmDel, setConfirmDel] = useState(null)
  if (!versions || versions.length < 2) return null
  return (
    <div className="mt-16">
      <span className="label-sm">Versions <span className="muted">· click to use</span></span>
      <div className="row gap-8 mt-8" style={{ flexWrap: 'wrap' }}>
        {versions.map((v) => {
          const isSel = v.id === selected
          const armed = confirmDel === v.id
          return (
            <div key={v.id} style={{ position: 'relative' }}
              onMouseLeave={() => armed && setConfirmDel(null)}>
              <button
                type="button"
                onClick={() => !isSel && !busy && onSelect && onSelect(v.id)}
                disabled={busy}
                title={isSel ? 'Current image' : 'Use this image'}
                style={{
                  position: 'relative', display: 'block', padding: 0, width: 60, aspectRatio: aspect,
                  borderRadius: 'var(--r-sm)', overflow: 'hidden', background: 'var(--paper-2)',
                  cursor: isSel ? 'default' : 'pointer',
                  border: `2px solid ${isSel ? 'var(--accent)' : armed ? 'var(--danger)' : 'var(--line)'}`,
                  opacity: busy && !isSel ? 0.5 : 1,
                }}>
                <img src={fileUrl(v.path) + (v.t ? `&t=${v.t}` : '')} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
                {isSel && (
                  <span style={{
                    position: 'absolute', right: 3, top: 3, width: 16, height: 16, borderRadius: '50%',
                    background: 'var(--accent)', color: 'var(--accent-ink)', fontSize: 9,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    <Icon name="check" />
                  </span>
                )}
              </button>
              {onDelete && !isSel && (
                <VersionDeleteBadge armed={armed} busy={busy}
                  onArm={() => setConfirmDel(v.id)}
                  onConfirm={() => { setConfirmDel(null); onDelete(v.id) }} />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Full-screen image viewer with optional version navigation. `versions` is a
// [{id, path}] list (as VersionStrip takes); `start` is the index to open on;
// `fallback` is shown when there are no versions. ← → (or ↑ ↓) flip versions,
// Esc closes, clicking the backdrop closes. View-only — selection stays with the
// VersionStrip on the card.
const LB_BTN = {
  position: 'absolute', zIndex: 2, border: 'none', color: '#fff',
  background: 'rgba(20,22,24,.55)', backdropFilter: 'blur(6px)',
  width: 46, height: 46, borderRadius: '50%', fontSize: 18,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
}

export function ImageLightbox({ versions = [], start = 0, fallback = '', title = '', onClose }) {
  const [i, setI] = useState(start)
  const idx = Math.min(Math.max(0, i), Math.max(0, versions.length - 1))
  const multi = versions.length > 1
  const move = (d) => setI(() => Math.min(Math.max(0, idx + d), versions.length - 1))
  useEffect(() => {
    const onKey = (e) => {
      const k = e.key
      if (k === 'ArrowLeft' || k === 'ArrowUp') { e.preventDefault(); move(-1) }
      else if (k === 'ArrowRight' || k === 'ArrowDown') { e.preventDefault(); move(1) }
      else if (k === 'Escape') { e.preventDefault(); onClose() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [idx, versions.length])
  const src = versions[idx] ? fileUrl(versions[idx].path) : fallback
  const arrow = (icon, ttl, disabled, onPress, pos) => (
    <button type="button" title={ttl} disabled={disabled}
      onClick={(e) => { e.stopPropagation(); onPress() }}
      style={{ ...LB_BTN, ...pos, opacity: disabled ? 0.28 : 1, cursor: disabled ? 'default' : 'pointer' }}>
      <Icon name={icon} />
    </button>
  )
  return (
    <div onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.82)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24, cursor: 'zoom-out' }}>
      {src
        ? <img src={src} alt="" onClick={(e) => e.stopPropagation()}
            style={{ maxWidth: '90%', maxHeight: '90%', objectFit: 'contain', borderRadius: 8, boxShadow: '0 24px 70px rgba(0,0,0,.6)', cursor: 'default' }} />
        : <span onClick={(e) => e.stopPropagation()} style={{ color: 'rgba(255,255,255,.8)', fontSize: 14, cursor: 'default' }}>No image yet.</span>}

      <div onClick={(e) => e.stopPropagation()}
        style={{ position: 'absolute', top: 18, left: 22, color: 'rgba(255,255,255,.92)', fontSize: 13, fontWeight: 600, display: 'flex', gap: 10, cursor: 'default' }}>
        {title && <span>{title}</span>}
        {multi && <span style={{ opacity: 0.65 }}>· {idx + 1} / {versions.length}</span>}
      </div>

      <button type="button" title="Close (Esc)" onClick={(e) => { e.stopPropagation(); onClose() }}
        style={{ ...LB_BTN, top: 14, right: 16, width: 40, height: 40, fontSize: 16, cursor: 'pointer' }}>
        <Icon name="xmark" />
      </button>

      {multi && arrow('chevron-left', 'Previous (←)', idx <= 0, () => move(-1),
        { left: 18, top: '50%', transform: 'translateY(-50%)' })}
      {multi && arrow('chevron-right', 'Next (→)', idx >= versions.length - 1, () => move(1),
        { right: 18, top: '50%', transform: 'translateY(-50%)' })}
    </div>
  )
}

// Like VersionStrip, but for rendered video takes: each thumbnail is a muted
// <video> (preload=metadata shows the first frame) so the user can flip between
// re-renders of a scene and pick the best one. `onDelete(id)` (optional) enables
// a confirm-armed × on the unused takes.
export function VideoVersionStrip({ versions, selected, onSelect, onDelete, aspect = '16 / 9', busy, label = 'Video takes', hint = 'click to use' }) {
  const [confirmDel, setConfirmDel] = useState(null)
  if (!versions || versions.length < 2) return null
  return (
    <div className="mt-16">
      <span className="label-sm">{label} <span className="muted">· {hint}</span></span>
      <div className="row gap-8 mt-8" style={{ flexWrap: 'wrap' }}>
        {versions.map((v) => {
          const isSel = v.id === selected
          const armed = confirmDel === v.id
          return (
            <div key={v.id} style={{ position: 'relative' }}
              onMouseLeave={() => armed && setConfirmDel(null)}>
              <button
                type="button"
                onClick={() => !isSel && !busy && onSelect && onSelect(v.id)}
                disabled={busy}
                title={`${v.label || ''}${v.label ? ' — ' : ''}${isSel ? 'current video' : 'click to use'}`}
                style={{
                  position: 'relative', display: 'block', padding: 0, width: 84, aspectRatio: aspect,
                  borderRadius: 'var(--r-sm)', overflow: 'hidden', background: '#000',
                  cursor: isSel ? 'default' : 'pointer',
                  border: `2px solid ${isSel ? 'var(--accent)' : armed ? 'var(--danger)' : 'var(--line)'}`,
                  opacity: busy && !isSel ? 0.5 : 1,
                }}>
                <video src={fileUrl(v.path) + (v.t ? `&t=${v.t}` : '')} muted preload="metadata" tabIndex={-1}
                  style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', pointerEvents: 'none' }} />
                <span style={{
                  position: 'absolute', left: 3, bottom: 3, fontSize: 9, color: '#fff',
                  background: 'rgba(0,0,0,.55)', padding: '1px 5px', borderRadius: 3,
                  letterSpacing: '.04em',
                }}>
                  <Icon name="film" />{v.lang ? ` ${v.lang.toUpperCase()}` : ''}
                </span>
                {isSel && (
                  <span style={{
                    position: 'absolute', right: 3, top: 3, width: 16, height: 16, borderRadius: '50%',
                    background: 'var(--accent)', color: 'var(--accent-ink)', fontSize: 9,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    <Icon name="check" />
                  </span>
                )}
              </button>
              {onDelete && !isSel && (
                <VersionDeleteBadge armed={armed} busy={busy}
                  onArm={() => setConfirmDel(v.id)}
                  onConfirm={() => { setConfirmDel(null); onDelete(v.id) }} />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// A song the user supplies rather than generates: what the file picker offers,
// and the size the backend refuses past (a five-minute stereo WAV is ~50 MB).
export const SONG_FILE_ACCEPT = '.wav,.mp3,.m4a,.flac,.ogg,.aac,.opus,audio/*'
export const SONG_UPLOAD_MAX = 80 * 1024 * 1024

// Music is film-level audio, so its versions are a vertical list of players (not
// thumbnails): listen to each kept track, see the prompt that made it, and click
// "Use" to make it the selected soundtrack. `onSelect(versionId)` re-muxes the film.
// `onDelete(versionId)` (optional) adds a confirm-armed Delete to the ones not in
// use — landing a song takes many takes, and the list only ever grows.
export function MusicVersionStrip({ versions, selected, onSelect, onDelete, busy }) {
  const [confirmDel, setConfirmDel] = useState(null)
  if (!versions || versions.length < 2) return null
  return (
    <div className="mt-16">
      <span className="label-sm">Music versions <span className="muted">· listen and pick one</span></span>
      <div className="stack gap-8 mt-8">
        {versions.map((v) => {
          const isSel = v.id === selected
          const armed = confirmDel === v.id
          return (
            <div key={v.id} className="row center gap-10"
              onMouseLeave={() => armed && setConfirmDel(null)}
              style={{
                padding: '8px 10px', borderRadius: 'var(--r-sm)', background: 'var(--paper-2)',
                border: `2px solid ${isSel ? 'var(--accent)' : armed ? 'var(--danger)' : 'var(--line)'}`,
                opacity: busy && !isSel ? 0.5 : 1,
              }}>
              <audio src={fileUrl(v.path)} controls preload="none" style={{ height: 34, flex: '0 0 auto', maxWidth: '60%' }} />
              {/* A song film's versions come from two acts — the engine's own
                  singer, and a seed-vc re-voicing of it — so the voice is
                  called out rather than buried in the prompt text. */}
              {v.voice && <Chip>♪ {v.voice}</Chip>}
              <span className="muted" title={v.desc} style={{
                flex: 1, minWidth: 0, fontSize: 12, overflow: 'hidden',
                textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>{v.desc || `Version ${v.id}`}</span>
              {isSel
                ? <Chip tone="ok" dot>In use</Chip>
                : <Button variant="ghost" icon="check" disabled={busy} onClick={() => onSelect && onSelect(v.id)}>Use</Button>}
              {onDelete && !isSel && (
                <Button variant={armed ? 'danger' : 'quiet'} icon={armed ? 'trash-can' : 'xmark'}
                  disabled={busy}
                  title={armed ? 'Click again to delete' : 'Delete this version'}
                  onClick={() => (armed ? (setConfirmDel(null), onDelete(v.id)) : setConfirmDel(v.id))}>
                  {armed ? 'Delete' : ''}
                </Button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Masked image editor: paint over a region of an image, describe the change, and
// send it to a FLUX inpaint pass. Calls `onApply(maskDataUrl, prompt, denoise)`
// where maskDataUrl is a black/white PNG (white = the painted region to change)
// and denoise (0.3–1.0) is the edit strength. `busy` disables the controls while
// the edit runs; `error` shows a message.
const MASK_COLOR = 'rgba(168,85,247,0.55)'

export function InpaintModal({ src, aspect = '16 / 9', busy, error, onApply, onClose }) {
  const baseRef = useRef(null)
  const overlayRef = useRef(null)
  const drawing = useRef(false)
  const last = useRef(null)
  const [brush, setBrush] = useState(36)
  const [strength, setStrength] = useState(75)   // % → denoise 0.30–1.00
  const [prompt, setPrompt] = useState('')
  const [hasMask, setHasMask] = useState(false)
  const [ready, setReady] = useState(false)

  // Load the image and size both canvases to it (capped so painting stays smooth).
  useEffect(() => {
    if (!src) return
    let alive = true
    const img = new Image()
    img.onload = () => {
      if (!alive) return
      const max = 1024
      const scale = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight))
      const w = Math.max(1, Math.round(img.naturalWidth * scale))
      const h = Math.max(1, Math.round(img.naturalHeight * scale))
      for (const c of [baseRef.current, overlayRef.current]) {
        if (c) { c.width = w; c.height = h }
      }
      const bctx = baseRef.current?.getContext('2d')
      if (bctx) bctx.drawImage(img, 0, 0, w, h)
      overlayRef.current?.getContext('2d').clearRect(0, 0, w, h)
      setHasMask(false)
      setReady(true)
    }
    img.src = src
    return () => { alive = false }
  }, [src])

  const pos = (e) => {
    const c = overlayRef.current
    const r = c.getBoundingClientRect()
    return {
      x: (e.clientX - r.left) * (c.width / r.width),
      y: (e.clientY - r.top) * (c.height / r.height),
      r: brush * (c.width / r.width),
    }
  }
  const onDown = (e) => {
    if (busy) return
    overlayRef.current.setPointerCapture(e.pointerId)
    drawing.current = true
    const p = pos(e)
    last.current = p
    const ctx = overlayRef.current.getContext('2d')
    ctx.fillStyle = MASK_COLOR
    ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill()
    setHasMask(true)
  }
  const onMove = (e) => {
    if (!drawing.current) return
    const p = pos(e)
    const ctx = overlayRef.current.getContext('2d')
    ctx.strokeStyle = MASK_COLOR
    ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.lineWidth = p.r * 2
    ctx.beginPath(); ctx.moveTo(last.current.x, last.current.y); ctx.lineTo(p.x, p.y); ctx.stroke()
    last.current = p
  }
  const onUp = () => { drawing.current = false; last.current = null }

  const clear = () => {
    const c = overlayRef.current
    c?.getContext('2d').clearRect(0, 0, c.width, c.height)
    setHasMask(false)
  }

  // Convert the painted overlay into a black/white mask PNG: any painted pixel
  // becomes white, everything else black — what the inpaint workflow expects.
  const buildMask = () => {
    const o = overlayRef.current
    const W = o.width, H = o.height
    const src = o.getContext('2d').getImageData(0, 0, W, H).data
    const mask = document.createElement('canvas')
    mask.width = W; mask.height = H
    const mctx = mask.getContext('2d')
    const out = mctx.createImageData(W, H)
    for (let i = 0; i < src.length; i += 4) {
      const on = src[i + 3] > 10 ? 255 : 0
      out.data[i] = out.data[i + 1] = out.data[i + 2] = on
      out.data[i + 3] = 255
    }
    mctx.putImageData(out, 0, 0)
    return mask.toDataURL('image/png')
  }

  const apply = () => {
    if (busy || !hasMask || !prompt.trim()) return
    onApply(buildMask(), prompt.trim(), Math.round(strength) / 100)
  }

  return (
    <div onClick={() => !busy && onClose()}
      style={{ position: 'fixed', inset: 0, zIndex: 1100, background: 'rgba(0,0,0,.82)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
      <div onClick={(e) => e.stopPropagation()}
        style={{ background: 'var(--paper)', borderRadius: 'var(--r-lg)', padding: 20, width: 'min(760px, 96vw)', maxHeight: '92vh', overflowY: 'auto', boxShadow: '0 30px 80px rgba(0,0,0,.5)' }}>
        <div className="row center between" style={{ marginBottom: 12 }}>
          <span className="h-title">Edit image</span>
          <button type="button" className="btn btn--quiet" onClick={() => !busy && onClose()} disabled={busy}><Icon name="xmark" /></button>
        </div>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 0, marginBottom: 12 }}>
          Paint over the area to change, then describe the edit. Only the painted region is regenerated.
        </p>

        <div style={{ textAlign: 'center', background: 'var(--paper-2)', borderRadius: 'var(--r-md)', padding: 8 }}>
          <div style={{ position: 'relative', display: 'inline-block', lineHeight: 0, maxWidth: '100%' }}>
            <canvas ref={baseRef} style={{ display: 'block', maxWidth: '100%', maxHeight: '52vh', width: 'auto', height: 'auto', borderRadius: 8 }} />
            <canvas ref={overlayRef}
              onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}
              style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', cursor: busy ? 'default' : 'crosshair', touchAction: 'none', borderRadius: 8 }} />
            {!ready && <div className="muted" style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 13 }}>Loading…</div>}
          </div>
        </div>

        <div className="row center gap-14 mt-16" style={{ flexWrap: 'wrap' }}>
          <span className="label-sm" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Icon name="paintbrush" /> Brush
            <input type="range" min={8} max={80} value={brush} disabled={busy}
              onChange={(e) => setBrush(Number(e.target.value))} style={{ width: 120 }} />
          </span>
          <span className="label-sm" style={{ display: 'flex', alignItems: 'center', gap: 8 }}
            title="How much the painted area is allowed to change — lower keeps more of the original.">
            <Icon name="sliders" /> Strength
            <input type="range" min={30} max={100} value={strength} disabled={busy}
              onChange={(e) => setStrength(Number(e.target.value))} style={{ width: 120 }} />
            <span className="muted" style={{ width: 56 }}>{strength < 55 ? 'Subtle' : strength > 85 ? 'Strong' : 'Medium'}</span>
          </span>
          <Button variant="ghost" size="sm" icon="eraser" disabled={busy || !hasMask} onClick={clear}>Clear</Button>
        </div>

        <div className="mt-16">
          <Field label="Describe the change" hint="e.g. “give him red sunglasses” or “remove the cup on the table”.">
            <textarea className="textarea" rows={2} value={prompt} disabled={busy}
              onChange={(e) => setPrompt(e.target.value)} placeholder="What should change in the painted area?" />
          </Field>
        </div>

        {error && <Banner tone="danger">{error}</Banner>}

        <div className="row gap-10 mt-16" style={{ justifyContent: 'flex-end' }}>
          <Button variant="ghost" onClick={() => !busy && onClose()} disabled={busy}>Cancel</Button>
          <Button variant="primary" icon="wand-magic-sparkles" disabled={busy || !hasMask || !prompt.trim()} onClick={apply}>
            {busy ? 'Editing…' : 'Apply edit'}
          </Button>
        </div>
      </div>
    </div>
  )
}

// Trim a rendered scene clip: drag the handle to pick a new end point (the player
// seeks there so you see the frame you'd cut on), then apply. Calls
// `onApply(endSeconds)`. The cut takes the audio with it — the note says so.
export function TrimModal({ src, busy, error, onApply, onClose }) {
  const videoRef = useRef(null)
  const [duration, setDuration] = useState(0)
  const [end, setEnd] = useState(0)

  const onMeta = () => {
    const d = videoRef.current?.duration
    if (!d || !isFinite(d)) return
    setDuration(d)
    setEnd(d)
  }

  // Seeking while dragging is the whole point: the frame under the handle is the
  // last frame the trimmed scene will hold.
  const seek = (v) => {
    setEnd(v)
    const el = videoRef.current
    if (el) { el.pause(); el.currentTime = Math.max(0, Math.min(v, (el.duration || v) - 0.01)) }
  }

  const cut = Math.max(0, duration - end)
  const canApply = !busy && duration > 0 && end >= 0.5 && cut > 0.05

  return (
    <div onClick={() => !busy && onClose()}
      style={{ position: 'fixed', inset: 0, zIndex: 1100, background: 'rgba(0,0,0,.82)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
      <div onClick={(e) => e.stopPropagation()}
        style={{ background: 'var(--paper)', borderRadius: 'var(--r-lg)', padding: 20, width: 'min(760px, 96vw)', maxHeight: '92vh', overflowY: 'auto', boxShadow: '0 30px 80px rgba(0,0,0,.5)' }}>
        <div className="row center between" style={{ marginBottom: 12 }}>
          <span className="h-title">Trim scene</span>
          <button type="button" className="btn btn--quiet" onClick={() => !busy && onClose()} disabled={busy}><Icon name="xmark" /></button>
        </div>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 0, marginBottom: 12 }}>
          Drag the handle to the new end point — everything after it is cut, narration included.
        </p>

        {/* The player sizes itself from the clip — a portrait scene must not grow
            past the dialog and push the handle out of reach. */}
        <div style={{ background: '#000', borderRadius: 'var(--r-md)', display: 'flex', justifyContent: 'center' }}>
          <video ref={videoRef} src={src} controls onLoadedMetadata={onMeta}
            style={{ display: 'block', maxWidth: '100%', maxHeight: '52vh' }} />
        </div>

        <div className="mt-16">
          <input type="range" min={0.5} max={Math.max(duration, 0.5)} step={0.04} value={end} disabled={busy || !duration}
            onChange={(e) => seek(Number(e.target.value))} style={{ width: '100%' }} />
          <div className="row center between mt-8" style={{ fontSize: 12.5 }}>
            <span className="muted">Was {duration.toFixed(1)}s</span>
            <span style={{ fontWeight: 600 }}>Keeps {end.toFixed(1)}s{cut > 0.05 ? ` · cuts ${cut.toFixed(1)}s` : ''}</span>
          </div>
        </div>

        {error && <div className="mt-16"><Banner tone="danger">{error}</Banner></div>}

        <div className="row gap-10 mt-16" style={{ justifyContent: 'flex-end' }}>
          <Button variant="ghost" onClick={() => !busy && onClose()} disabled={busy}>Cancel</Button>
          <Button variant="primary" icon="scissors" disabled={!canApply} onClick={() => canApply && onApply(Number(end.toFixed(2)))}>
            {busy ? 'Trimming…' : 'Trim'}
          </Button>
        </div>
      </div>
    </div>
  )
}

// Shortest and longest continuation H3 will render (pipeline/comfyui.py clamps
// a clip to 4–15s; 12 is the same margin acted scenes are written to).
const CONTINUE_MIN_SECONDS = 4
const CONTINUE_MAX_SECONDS = 12

export function ContinueModal({ src, castOpts = [], busy, error, onApply, onClose }) {
  const [seconds, setSeconds] = useState(CONTINUE_MIN_SECONDS)
  const [direction, setDirection] = useState('')
  const [lines, setLines] = useState([])
  const videoRef = useRef(null)

  // Park the player on the last frame: that is the moment the new clip picks up
  // from, and seeing it is how you decide what should happen next.
  const onMeta = () => {
    const el = videoRef.current
    if (el && isFinite(el.duration)) el.currentTime = Math.max(0, el.duration - 0.05)
  }

  const setLine = (i, k, v) => setLines(lines.map((ln, idx) => idx === i ? { ...ln, [k]: v } : ln))
  const addLine = () => setLines([...lines, { speaker: castOpts[0] || '', delivery: 'even, natural', text: '' }])
  const spoken = lines.filter((ln) => (ln.text || '').trim() && (ln.speaker || '').trim())

  return (
    <div onClick={() => !busy && onClose()}
      style={{ position: 'fixed', inset: 0, zIndex: 1100, background: 'rgba(0,0,0,.82)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}>
      <div onClick={(e) => e.stopPropagation()}
        style={{ background: 'var(--paper)', borderRadius: 'var(--r-lg)', padding: 20, width: 'min(760px, 96vw)', maxHeight: '92vh', overflowY: 'auto', boxShadow: '0 30px 80px rgba(0,0,0,.5)' }}>
        <div className="row center between" style={{ marginBottom: 12 }}>
          <span className="h-title">Continue scene</span>
          <button type="button" className="btn btn--quiet" onClick={() => !busy && onClose()} disabled={busy}><Icon name="xmark" /></button>
        </div>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 0, marginBottom: 12 }}>
          Shoots a few more seconds carrying on from the last frame — same room, same take, no cut.
          Leave the dialogue empty and the moment simply plays on.
        </p>

        <div style={{ background: '#000', borderRadius: 'var(--r-md)', display: 'flex', justifyContent: 'center' }}>
          <video ref={videoRef} src={src} controls onLoadedMetadata={onMeta}
            style={{ display: 'block', maxWidth: '100%', maxHeight: '40vh' }} />
        </div>

        <div className="mt-16">
          <input type="range" min={CONTINUE_MIN_SECONDS} max={CONTINUE_MAX_SECONDS} step={0.5}
            value={seconds} disabled={busy}
            onChange={(e) => setSeconds(Number(e.target.value))} style={{ width: '100%' }} />
          <div className="row center between mt-8" style={{ fontSize: 12.5 }}>
            <span className="muted">How much longer</span>
            <span style={{ fontWeight: 600 }}>+{seconds.toFixed(1)}s</span>
          </div>
        </div>

        <Field label="What happens next" hint="A note for this clip only — “she finally looks up”, “he keeps walking”. Optional.">
          <textarea className="textarea" rows={2} value={direction} disabled={busy}
            onChange={(e) => setDirection(e.target.value)} />
        </Field>

        <Field label="Dialogue" hint="What is said in the continuation, in the same voices. About 2.5 words a second.">
          <div className="stack gap-8">
            {lines.map((ln, i) => (
              <div key={i} className="row gap-8 center">
                <select className="input" style={{ maxWidth: 170 }} value={ln.speaker || ''} disabled={busy}
                  onChange={(e) => setLine(i, 'speaker', e.target.value)}>
                  {castOpts.map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
                <input className="input" style={{ flex: 1 }} value={ln.text || ''} disabled={busy}
                  placeholder="…and what they say" onChange={(e) => setLine(i, 'text', e.target.value)} />
                <Button variant="quiet" icon="trash" title="Remove line" disabled={busy}
                  onClick={() => setLines(lines.filter((_, idx) => idx !== i))} />
              </div>
            ))}
            <div>
              <Button variant="ghost" icon="plus" size="sm" disabled={busy || !castOpts.length} onClick={addLine}>
                Add line
              </Button>
              {!castOpts.length && <span className="muted" style={{ fontSize: 12.5, marginLeft: 8 }}>No characters to speak.</span>}
            </div>
          </div>
        </Field>

        {error && <div className="mt-16"><Banner tone="danger">{error}</Banner></div>}

        <div className="row gap-10 mt-16" style={{ justifyContent: 'flex-end' }}>
          <Button variant="ghost" onClick={() => !busy && onClose()} disabled={busy}>Cancel</Button>
          <Button variant="primary" icon="forward-step" disabled={busy}
            onClick={() => !busy && onApply({ seconds, direction: direction.trim(), lines: spoken })}>
            {busy ? 'Shooting…' : 'Continue'}
          </Button>
        </div>
      </div>
    </div>
  )
}

export function Banner({ tone = 'danger', children }) {
  if (!children) return null
  const bg = { danger: 'var(--danger-soft)', ok: 'var(--ok-soft)', info: 'var(--info-soft)', warn: 'var(--warn-soft)' }[tone]
  const fg = { danger: 'var(--danger)', ok: 'var(--ok)', info: 'var(--info)', warn: 'var(--warn)' }[tone]
  return (
    <div className="row center gap-10" style={{ padding: '10px 14px', background: bg, color: fg, borderRadius: 'var(--r-md)', fontSize: 13, marginBottom: 16 }}>
      <Icon name="circle-info" /> <span>{children}</span>
    </div>
  )
}

// Ordered to follow the production pipeline: ideation → production → publishing/audience.
const NAV = [
  { id: 'home', label: 'Home', icon: 'house' },
  { id: 'create', label: 'Create', icon: 'wand-magic-sparkles' },
  { id: 'ideas', label: 'AI ideas', icon: 'lightbulb' },
  { sep: true },
  { id: 'queue', label: 'Queue', icon: 'layer-group' },
  { id: 'script', label: 'Script', icon: 'feather-pointed' },
  { id: 'progress', label: 'Render', icon: 'gauge-high' },
  { id: 'activity', label: 'Activity', icon: 'clock-rotate-left' },
  { id: 'library', label: 'Films', icon: 'film' },
  { sep: true },
  { id: 'publish', label: 'Publishing', icon: 'upload' },
  { id: 'community', label: 'Community', icon: 'comments' },
  { id: 'analytics', label: 'Channel Analytics', icon: 'chart-simple' },
  { sep: true },
  { id: 'settings', label: 'Settings', icon: 'gear' },
]

// Whether a nav item is the active page. Channel Analytics owns the legacy
// #/engagement (Predictive Model) deep link too, so it highlights for both.
function navActive(id, route) {
  if (id === 'analytics') return route === 'analytics' || route === 'engagement'
  // The prompt editor is opened from Settings and belongs to it.
  if (id === 'settings') return route === 'settings' || route === 'prompts'
  return route === id
}

// Mailbox-style sidebar indicator for a nav item: a live "REC" pill while a
// render is running, otherwise a count of items needing attention.
function navIndicator(id, badges) {
  if (id === 'progress' && badges.render_active) {
    return (
      <span className="nav__rec" title="Rendering now">
        <span className="nav__rec-dot"></span>
        {badges.render_pct ? `${badges.render_pct}%` : 'REC'}
      </span>
    )
  }
  if (id === 'activity' && (badges.activity_live || badges.render_active)) {
    const n = badges.activity_live || (badges.render_active ? 1 : 0)
    return (
      <span className="nav__rec" title="Work in progress">
        <span className="nav__rec-dot"></span>
        {n > 1 ? n : 'LIVE'}
      </span>
    )
  }
  const counts = { queue: badges.queue, community: badges.youtube_attention, publish: badges.youtube_publishable, library: badges.films }
  const n = counts[id]
  return n ? <span className="nav__badge nav__badge--attn">{n}</span> : null
}

export function Sidebar({ route, go, badges = {} }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <img className="brand__mark" src="/assets/StephenSpielbot.png" alt="" />
        <div>
          <div className="brand__name">Spielbot</div>
          <div className="brand__sub">AI film studio</div>
        </div>
      </div>
      <nav className="nav">
        {NAV.map((n, i) => (
          n.sep
            ? <div key={'sep' + i} className="nav__sep"></div>
            : <button key={n.id} className={`nav__item ${navActive(n.id, route) ? 'is-active' : ''}`} onClick={() => go(n.id)}>
                <Icon name={n.icon} brand={n.brand} />
                <span>{n.label}</span>
                {navIndicator(n.id, badges)}
              </button>
        ))}
      </nav>
      <div className="sidebar__foot">
        <button type="button" className={`channel channel--link ${route === 'about' ? 'is-active' : ''}`}
          onClick={() => go('about')} title="About Stephen Spielbot">
          <span className="channel__avatar"><Icon name="youtube" brand /></span>
          <div className="grow">
            <div className="channel__name">@StephenSpielbot</div>
            <div className="channel__meta">AI slop video director</div>
          </div>
          <Icon name="circle-info" style={{ color: 'var(--ink-4)', fontSize: 13 }} />
        </button>
      </div>
    </aside>
  )
}
