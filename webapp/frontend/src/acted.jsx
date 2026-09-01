import { useEffect, useState } from 'react'
import { api } from './api.js'
import { Banner, Button, GuidedRegenButton, Icon, VideoVersionStrip, voiceLabel } from './components.jsx'

// The acted-scene staging blocks, shared by the Script and film editors' scene
// cards so the two can never drift apart: a take is
// conditioned on CHARACTERS, and the prompt refers to them by slot number
// ("<Picture 1>", "<Audio 1>"), so every scene shows its slots as the thing
// itself — the portrait that IS Picture 1, the voice clip that IS Audio 1.

export function CastMember({ c, picture, audio, jobId, voiceOpts, voiceMeta, onChanged }) {
  const [busy, setBusy] = useState('')
  const run = async (what, fn) => {
    setBusy(what)
    try { await fn(); await onChanged() } finally { setBusy('') }
  }
  // A catalogue character is shared with every other film that uses it, so it is
  // edited in Settings rather than silently rewritten from one film's screen.
  const editable = c.editable && jobId

  return (
    <div className="stack gap-8" style={{ width: 230, minWidth: 0 }}>
      <div className="row gap-8 center">
        <span className="label-sm">{picture ? `Picture ${picture.slot}` : 'No picture'}</span>
        {audio && <span className="label-sm">· Audio {audio.slot}</span>}
      </div>
      {c.image_url
        ? <img src={c.image_url} alt={c.name}
            style={{ width: 104, height: 104, objectFit: 'cover', borderRadius: 10,
                     border: '1px solid var(--line, #ddd)' }} />
        : <div style={{ width: 104, height: 104, borderRadius: 10, display: 'flex',
                        alignItems: 'center', justifyContent: 'center',
                        background: 'var(--well, rgba(127,127,127,.10))',
                        border: '1px dashed var(--line, #ccc)' }}>
            <Icon name="user" style={{ color: 'var(--ink-3)', fontSize: 22 }} />
          </div>}
      <div>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{c.name}</span>
        {c.scope === 'catalogue' && <span className="muted" style={{ fontSize: 11.5 }}> · catalogue</span>}
      </div>

      {editable ? (
        <GuidedRegenButton block size="sm" variant="ghost" icon="rotate-right"
          label={c.has_image ? 'Regenerate look' : 'Generate look'} busyLabel="Painting…"
          busy={busy === 'look'} disabled={!!busy}
          onRegen={(instr) => run('look', () => api.generateScriptCharacterPortrait(jobId, c.id, instr))} />
      ) : (
        <span className="muted" style={{ fontSize: 11.5 }}>
          {c.scope === 'catalogue' ? 'Look and voice come from your catalogue — change them in Settings → Characters.'
            : 'Not in this script’s cast or your catalogue.'}
        </span>
      )}

      {c.speaks && (
        <label className="stack gap-4">
          <span className="label-sm">Voice</span>
          <select className="input" value={c.voice || ''} disabled={!editable || !!busy}
            onChange={(e) => run('voice', () => api.updateScriptCharacter(jobId, c.id, { voice: e.target.value }))}>
            <option value="">Let the model invent it</option>
            {/* The assigned voice always appears, even when the library list
                hasn't loaded — otherwise the select silently shows "invent"
                for a character that HAS a voice. */}
            {c.voice && !voiceOpts.includes(c.voice) && <option value={c.voice}>{c.voice}</option>}
            {voiceOpts.map((v) => <option key={v} value={v}>{voiceLabel(v, voiceMeta)}</option>)}
          </select>
          {!c.voice && (
            <span className="muted" style={{ fontSize: 11.5 }}>
              No reference — the model picks a voice, and it changes between scenes.
            </span>
          )}
        </label>
      )}
    </div>
  )
}

// A non-character reference slot — location, wardrobe, continuity frame — as a
// small picture, so every <Picture N> the take renders from is visible on the
// card, not just the people.
export function RefTile({ p, busy = false, onRemoveFrame }) {
  return (
    <div className="stack gap-8" style={{ width: 110 }}>
      <span className="label-sm">Picture {p.slot}</span>
      {p.image_url
        ? <img src={p.image_url} alt={p.name}
            style={{ width: 104, height: 104, objectFit: 'cover', borderRadius: 10,
                     border: '1px solid var(--line, #ddd)' }} />
        : <div style={{ width: 104, height: 104, borderRadius: 10, display: 'flex',
                        alignItems: 'center', justifyContent: 'center',
                        background: 'var(--well, rgba(127,127,127,.10))',
                        border: '1px dashed var(--line, #ccc)' }}>
            <Icon name={p.kind === 'wardrobe' ? 'shirt' : 'location-dot'}
              style={{ color: 'var(--ink-3)', fontSize: 20 }} />
          </div>}
      <span className="muted" style={{ fontSize: 11.5 }}>{p.name} · {p.kind}</span>
      {p.kind === 'frame' && onRemoveFrame && (
        <Button variant="quiet" size="sm" icon="trash-can" disabled={!!busy}
          title="Remove the first frame — the take renders from portraits and visuals only"
          onClick={onRemoveFrame} />
      )}
    </div>
  )
}

// The take's SOUNDTRACK input: the stretch of the film's song this scene is
// generated against (audio-driven H3). Playable directly — the #t media
// fragment plays exactly the pinned window.
export function SoundtrackSlice({ window: win, songUrl }) {
  if (!win || !songUrl) return null
  return (
    <div className="stack gap-6">
      <span className="label-sm">
        Soundtrack · {Number(win[0]).toFixed(1)}s–{Number(win[1]).toFixed(1)}s of the film's song
      </span>
      <audio controls preload="none" style={{ width: '100%', height: 32 }}
        src={`${songUrl}#t=${win[0]},${win[1]}`} />
      <span className="muted" style={{ fontSize: 12 }}>
        Pinned into this take — the performance is generated to match this exact
        slice. The take above carries it too, so it can be watched against its
        own music, and it plays under the scene in the final film.
      </span>
    </div>
  )
}

// What this beat is asked to SING, as a field beside the dialogue instead of
// prose inside the prompt: the lyric lines, and when each is heard in the
// clip's OWN seconds. The prompt's [SONG] words and its "a voice sings only
// from…" window are assembled from these (and the caption cues are dated off
// the times), so trimming a line here is how a shot that was asked to sing
// more than its slice carries gets fixed — without hand-editing, and thereby
// freezing, the whole prompt.
export function SungLines({ scene, onSave, disabled = false }) {
  const [rows, setRows] = useState(null) // null ⟹ mirror the scene
  const [saving, setSaving] = useState(false)
  // Our own save refreshes the scene — fall back to mirroring it then (and on
  // a scene switch); an unrelated refresh carries the same values and is a
  // no-op here.
  useEffect(() => { setRows(null) },
    [scene?.id, scene?.sings, JSON.stringify(scene?.line_times || [])])
  if (!scene?.singing) return null

  const times = Array.isArray(scene.line_times) ? scene.line_times : []
  const mirror = String(scene.sings || '').split('\n')
    .map((l) => l.trim()).filter(Boolean)
    .map((text, i) => ({
      text,
      t0: times[i]?.[0] != null ? String(times[i][0]) : '',
      t1: times[i]?.[1] != null ? String(times[i][1]) : '',
    }))
  const shown = rows || mirror

  const commit = async (next) => {
    const kept = next.filter((r) => r.text.trim())
    const sings = kept.map((r) => r.text.trim()).join('\n')
    // Times only count as a set: captions pair them with the lines one-to-one,
    // so one blank or backwards pair drops them all rather than mis-pairing.
    const complete = kept.length > 0 && kept.every((r) =>
      r.t0 !== '' && r.t1 !== '' && Number(r.t1) > Number(r.t0) && Number(r.t0) >= 0)
    const lineTimes = complete
      ? kept.map((r) => [Number(r.t0), Number(r.t1)]) : []
    if (sings === mirror.map((r) => r.text).join('\n') &&
        JSON.stringify(lineTimes) === JSON.stringify(
          mirror.every((r) => r.t0 !== '' && r.t1 !== '')
            ? mirror.map((r) => [Number(r.t0), Number(r.t1)]) : [])) {
      setRows(next.length ? next : null)
      return
    }
    setSaving(true)
    try { await onSave(sings, lineTimes) } finally { setSaving(false) }
  }

  const edit = (i, k, v) => setRows(shown.map((r, j) => (j === i ? { ...r, [k]: v } : r)))
  const addRow = () => {
    const last = shown[shown.length - 1]
    const t0 = last && last.t1 !== '' ? Number(last.t1) : 0
    setRows([...shown, { text: '', t0: String(t0), t1: String(t0 + 2) }])
  }

  const vr = scene.vocal_ranges
  const fmt = (x) => Number(x).toFixed(1)
  const vocal = vr == null
    ? 'Singing not measured — the prompt treats the whole clip as sung.'
    : !vr.length
      ? 'Measured instrumental — no voice is heard in this shot, and the prompt asks nobody to sing.'
      : `A voice is heard ${vr.map((r) => `${fmt(r[0])}–${fmt(r[1])}s`).join(', ')} into this clip — outside that, the prompt keeps every mouth closed.`

  const off = disabled || saving
  return (
    <div className="stack gap-6">
      <span className="label-sm">Sung in this shot</span>
      <span className="muted" style={{ fontSize: 12 }}>{vocal}</span>
      {shown.map((r, i) => (
        <div key={i} className="row gap-6 center">
          <input type="number" className="input" style={{ width: 78 }} step="0.1" min="0"
            value={r.t0} placeholder="start" disabled={off}
            onChange={(e) => edit(i, 't0', e.target.value)} onBlur={() => commit(shown)} />
          <span className="muted">–</span>
          <input type="number" className="input" style={{ width: 78 }} step="0.1" min="0"
            value={r.t1} placeholder="end" disabled={off}
            onChange={(e) => edit(i, 't1', e.target.value)} onBlur={() => commit(shown)} />
          <span className="muted" style={{ fontSize: 12 }}>s</span>
          <input className="input" style={{ flex: 1, minWidth: 160 }} value={r.text}
            placeholder="lyric line" disabled={off}
            onChange={(e) => edit(i, 'text', e.target.value)} onBlur={() => commit(shown)} />
          <Button variant="ghost" icon="trash-can" size="sm" disabled={off}
            onClick={() => commit(shown.filter((_, j) => j !== i))} />
        </div>
      ))}
      <div className="row gap-8 center">
        <Button variant="ghost" icon="plus" size="sm" disabled={off} onClick={addRow}>Add line</Button>
        <span className="muted" style={{ fontSize: 12 }}>
          The words the cast mouths and when each line is heard, in seconds into
          this clip. The prompt and the burned captions follow; clearing every
          line marks the shot instrumental.
        </span>
      </div>
      {scene.prompt_edited && (
        <span className="muted" style={{ fontSize: 12 }}>
          This scene's prompt is hand-edited (pinned) — rebuild it for these
          changes to reach the render.
        </span>
      )}
    </div>
  )
}

export function StagingWarnings({ missingPortraits = [], unvoiced = [] }) {
  if (!missingPortraits.length && !unvoiced.length) return null
  return (
    <Banner tone="warn">
      {missingPortraits.length > 0 && (
        <div>No portrait for {missingPortraits.join(', ')} — the model will
          invent their look, and it will change between scenes. Add a look image
          in Characters.</div>
      )}
      {unvoiced.length > 0 && (
        <div>No cast voice for {unvoiced.join(', ')} — the model will invent
          a voice, and it will drift between scenes.</div>
      )}
    </Banner>
  )
}

// The rendered take(s), once the scene has been shot: the current take with a
// re-shoot button, and every kept take as a strip — flipping swaps the
// canonical final, so Reassemble picks up whichever take is selected.
// `rewriteButton` is the parent's own LLM-rewrite control, rendered beside the
// re-shoot so the two actions read as the pair they are.
export function ActedTakes({ scene, workDir, onChanged, rewriteButton = null, hint = '' }) {
  const [reshoot, setReshoot] = useState('')
  const [takeBusy, setTakeBusy] = useState(false)
  if (!scene.has_video) return null
  const rerender = async (instruction) => {
    setReshoot('busy')
    try {
      await api.rerenderFilmScene(workDir, scene.id, 'video', instruction)
      setReshoot('queued')
    } catch (e) { setReshoot(e.message) }
  }
  const takeOp = async (fn) => {
    setTakeBusy(true)
    try { await fn(); await onChanged() } catch (e) { setReshoot(e.message) } finally { setTakeBusy(false) }
  }
  return (
    <div className="stack gap-8">
      <video controls preload="metadata" src={scene.video_url}
        style={{ width: '100%', maxWidth: 360, borderRadius: 10, background: '#000' }} />
      {/* The take on screen was shot from the scene's text — after an edit it
          is stale until the scene is shot again. */}
      <div className="row gap-8 center row--wrap">
        <GuidedRegenButton size="sm" variant="ghost" icon="clapperboard"
          label="Shoot this scene again" busyLabel="Queued…"
          busy={reshoot === 'busy'} disabled={reshoot === 'busy'}
          onRegen={rerender} />
        {rewriteButton}
        {reshoot === 'queued' && (
          <span className="muted" style={{ fontSize: 12 }}>Re-rendering — watch it in Activity.</span>
        )}
        {reshoot && reshoot !== 'busy' && reshoot !== 'queued' && (
          <span style={{ fontSize: 12, color: 'var(--danger)' }}>{reshoot}</span>
        )}
      </div>
      {hint && <span className="muted" style={{ fontSize: 12 }}>{hint}</span>}
      <VideoVersionStrip versions={scene.video_history?.versions}
        selected={scene.video_history?.selected}
        onSelect={(vid) => takeOp(() => api.selectFilmVideo(workDir, scene.id, vid))}
        onDelete={(vid) => takeOp(() => api.deleteFilmVideo(workDir, scene.id, vid))}
        aspect="9 / 16" busy={takeBusy || reshoot === 'busy'}
        label="Takes" hint="every re-shoot is kept — click to use" />
    </div>
  )
}
