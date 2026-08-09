import { useEffect, useState } from 'react'
import { api, fileUrl } from '../api.js'
import { Card, Button, Banner, Icon, GuidedRegenButton, voiceLabel } from '../components.jsx'

// Performance films are conditioned on CHARACTERS, not on a scene still, and the
// prompt refers to them by slot number ("<Picture 1>", "<Audio 1>"). Showing the
// prompt alone would leave you guessing which reference each number is, so every
// scene shows its slots as the thing itself: the portrait that IS Picture 1, the
// voice clip that IS Audio 1. Everything for a scene sits in one card — no tab
// hopping between the script, the characters and the voices.

function CastMember({ c, picture, audio, jobId, voiceOpts, voiceMeta, onChanged }) {
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
            : 'Not in this script\u2019s cast or your catalogue.'}
        </span>
      )}

      {c.speaks && (
        <label className="stack gap-4">
          <span className="label-sm">Voice</span>
          <select className="input" value={c.voice || ''} disabled={!editable || !!busy}
            onChange={(e) => run('voice', () => api.updateScriptCharacter(jobId, c.id, { voice: e.target.value }))}>
            <option value="">Let the model invent it</option>
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


// The dialogue and the assembled prompt are editable in place. Lines are staged
// locally and saved on demand (typing a line shouldn't re-render the film view
// on every keystroke); the prompt saves as an OVERRIDE — once set, the scene's
// fields stop rebuilding it, and "Rebuild from the scene" drops it again.
function SceneEditor({ scene, jobId, onSaved }) {
  const [lines, setLines] = useState(scene.lines)
  const [prompt, setPrompt] = useState(scene.prompt)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  // A refresh (or another scene's save) reloads the script: adopt the new text
  // unless this card is mid-edit.
  useEffect(() => { setLines(scene.lines); setPrompt(scene.prompt) },
    [scene.lines, scene.prompt])

  const dirtyLines = JSON.stringify(lines) !== JSON.stringify(scene.lines)
  const dirtyPrompt = prompt !== scene.prompt

  const save = async (what, body) => {
    setBusy(what); setErr('')
    try {
      await api.saveScene(jobId, scene.id, {
        title: scene.title, image_prompt: scene.image_prompt,
        video_prompt: scene.video_prompt, narration: scene.narration,
        mode: scene.mode, ...body,
      })
      await onSaved()
    } catch (e) { setErr(e.message) } finally { setBusy('') }
  }

  const setLine = (i, key, v) => setLines(lines.map((l, j) => (i === j ? { ...l, [key]: v } : l)))
  const addLine = () => setLines([...lines, { speaker: scene.cast[0]?.name || '', delivery: 'even, natural', text: '' }])

  return (
    <>
      {err && <Banner tone="danger">{err}</Banner>}
      <div className="stack gap-10">
        <div className="row between center">
          <span className="label-sm">Dialogue</span>
          <span className="muted" style={{ fontSize: 11.5 }}>
            About {Math.round(scene.seconds)}s of screen time — longer dialogue makes a longer clip.
          </span>
        </div>
        {lines.map((l, i) => (
          <div key={i} className="row gap-8" style={{ alignItems: 'flex-start' }}>
            <select className="input" style={{ width: 130, flex: '0 0 auto' }} value={l.speaker}
              onChange={(e) => setLine(i, 'speaker', e.target.value)}>
              {[...new Set([...scene.cast.map((c) => c.name), l.speaker].filter(Boolean))]
                .map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
            <input className="input" style={{ width: 150, flex: '0 0 auto' }} value={l.delivery || ''}
              placeholder="delivery" onChange={(e) => setLine(i, 'delivery', e.target.value)} />
            <textarea className="textarea" rows={2} style={{ flex: '1 1 220px', minWidth: 0 }}
              value={l.text} onChange={(e) => setLine(i, 'text', e.target.value)} />
            <Button variant="ghost" size="sm" icon="trash" title="Remove this line"
              onClick={() => setLines(lines.filter((_, j) => j !== i))} />
          </div>
        ))}
        <div className="row gap-8">
          <Button variant="ghost" size="sm" icon="plus" onClick={addLine}>Add a line</Button>
          <Button variant={dirtyLines ? 'primary' : 'ghost'} size="sm" disabled={!dirtyLines || !!busy}
            onClick={() => save('lines', { lines })}>
            {busy === 'lines' ? 'Saving…' : 'Save dialogue'}
          </Button>
          {dirtyLines && (
            <Button variant="ghost" size="sm" disabled={!!busy}
              onClick={() => setLines(scene.lines)}>Discard</Button>
          )}
        </div>
      </div>

      <details open={scene.prompt_edited}>
        <summary style={{ cursor: 'pointer', fontSize: 12.5 }} className="muted">
          Exact prompt sent to the video model{scene.prompt_edited ? ' — hand-edited' : ''}
        </summary>
        <div className="stack gap-8" style={{ marginTop: 10 }}>
          <textarea className="textarea mono" rows={14} value={prompt} style={{ fontSize: 12 }}
            onChange={(e) => setPrompt(e.target.value)} />
          <div className="row gap-8">
            <Button variant={dirtyPrompt ? 'primary' : 'ghost'} size="sm"
              disabled={!dirtyPrompt || !!busy} onClick={() => save('prompt', { prompt })}>
              {busy === 'prompt' ? 'Saving…' : 'Save prompt'}
            </Button>
            {dirtyPrompt && (
              <Button variant="ghost" size="sm" disabled={!!busy}
                onClick={() => setPrompt(scene.prompt)}>Discard</Button>
            )}
            {scene.prompt_edited && (
              <Button variant="ghost" size="sm" disabled={!!busy}
                onClick={() => save('prompt', { prompt: '' })}>
                {busy === 'prompt' ? 'Rebuilding…' : 'Rebuild from the scene'}
              </Button>
            )}
          </div>
          <span className="muted" style={{ fontSize: 11.5 }}>
            {scene.prompt_edited
              ? 'This scene renders from the text above — the cast, beats and sound below no longer rebuild it.'
              : 'Assembled from the scene. Editing it pins this exact text for the render.'}
          </span>
        </div>
      </details>
    </>
  )
}


function SceneCard({ scene, seconds, jobId, workDir, voiceOpts, voiceMeta, onChanged }) {
  const [reshoot, setReshoot] = useState('')
  const rerender = async (instruction) => {
    setReshoot('busy')
    try {
      await api.rerenderFilmScene(workDir, scene.id, 'video', instruction)
      setReshoot('queued')
    } catch (e) { setReshoot(e.message) }
  }
  return (
    <Card span={12} className="stack gap-16">
      <div className="row between center row--wrap gap-10">
        <div>
          <span className="label-sm">Scene {scene.id}</span>
          <h3 style={{ margin: '2px 0 0', fontSize: 17 }}>{scene.title}</h3>
        </div>
        <span className="muted" style={{ fontSize: 12.5 }}>
          {Math.round(scene.seconds || seconds)}s · one continuous shot
        </span>
      </div>

      {scene.has_video && (
        <div className="stack gap-8">
          <video controls preload="metadata" src={scene.video_url}
            style={{ width: '100%', maxWidth: 360, borderRadius: 10, background: '#000' }} />
          {/* The take on screen was shot from the text below — after an edit it
              is stale until the scene is shot again. */}
          <div className="row gap-8 center">
            <GuidedRegenButton size="sm" variant="ghost" icon="clapperboard"
              label="Shoot this scene again" busyLabel="Queued…"
              busy={reshoot === 'busy'} disabled={reshoot === 'busy'}
              onRegen={rerender} />
            {reshoot === 'queued' && (
              <span className="muted" style={{ fontSize: 12 }}>Re-rendering — watch it in Activity.</span>
            )}
            {reshoot && reshoot !== 'busy' && reshoot !== 'queued' && (
              <span style={{ fontSize: 12, color: 'var(--danger)' }}>{reshoot}</span>
            )}
          </div>
        </div>
      )}

      {/* ── Cast: each numbered slot IS the portrait / the voice clip, and the
             look and voice are set right here rather than in another tab. ── */}
      <div className="row gap-16 row--wrap" style={{ alignItems: 'flex-start' }}>
        {scene.cast.map((c) => (
          <CastMember key={c.name} c={c}
            audio={scene.audios.find((a) => a.name === c.name)}
            picture={scene.pictures.find((p) => p.name === c.name)}
            jobId={jobId} voiceOpts={voiceOpts} voiceMeta={voiceMeta} onChanged={onChanged} />
        ))}
      </div>

      {(scene.missing_portraits.length > 0 || scene.unvoiced.length > 0) && (
        <Banner tone="warn">
          {scene.missing_portraits.length > 0 && (
            <div>No portrait for {scene.missing_portraits.join(', ')} — the model will
              invent their look, and it will change between scenes. Add a look image
              in Characters.</div>
          )}
          {scene.unvoiced.length > 0 && (
            <div>No cast voice for {scene.unvoiced.join(', ')} — the model will invent
              a voice, and it will drift between scenes.</div>
          )}
        </Banner>
      )}

      {/* ── What happens ── */}
      <div className="stack gap-8">
        <span className="label-sm">Action</span>
        {scene.beats.map((b, i) => (
          <div key={i} style={{ fontSize: 13.5 }}>
            <code style={{ opacity: 0.7 }}>{b.t0}s–{b.t1}s</code> {b.action}
          </div>
        ))}
        {!scene.beats.length && <span className="muted" style={{ fontSize: 13 }}>No beats.</span>}
      </div>

      {/* ── What is said, and the exact prompt — both editable ── */}
      <SceneEditor scene={scene} jobId={jobId} onSaved={onChanged} />

      <div className="row gap-16 row--wrap" style={{ fontSize: 13 }}>
        <div style={{ flex: '1 1 320px' }}><span className="label-sm">Camera</span><div>{scene.camera || '—'}</div></div>
        <div style={{ flex: '1 1 320px' }}><span className="label-sm">Sound</span><div>{scene.soundscape || '—'}</div></div>
      </div>
    </Card>
  )
}

export default function PerformanceScenes({ workDir, jobId, voiceOpts = [], voiceMeta = {} }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    if (!workDir) return
    setBusy(true)
    try {
      setData(await api.loadPerformanceScript(workDir))
      setError('')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => { load() }, [workDir])

  if (error) return <Banner tone="danger">{error}</Banner>
  if (!data) return <Card span={12} well><p className="muted" style={{ fontSize: 13, margin: 0 }}>Loading the performance…</p></Card>

  return (
    <>
      <Card span={12} well>
        <div className="row between center row--wrap gap-10">
          <span style={{ fontSize: 13 }}>
            {data.scenes.length} acted scene{data.scenes.length === 1 ? '' : 's'} · characters speak
            on screen · no narrator, no music · <strong>{data.engine?.label}</strong>
          </span>
          <Button variant="ghost" icon="rotate" disabled={busy} onClick={load}>
            {busy ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
      </Card>
      {data.scenes.map((s) => (
        <SceneCard key={s.id} scene={s} seconds={s.seconds} jobId={jobId || data.job_id}
          workDir={workDir}
          voiceOpts={voiceOpts} voiceMeta={voiceMeta} onChanged={load} />
      ))}
      {!data.scenes.length && (
        <Card span={12} well><p className="muted" style={{ fontSize: 13, margin: 0 }}>
          This script has no performance scenes.</p></Card>
      )}
    </>
  )
}
