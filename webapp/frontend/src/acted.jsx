import { useState } from 'react'
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
