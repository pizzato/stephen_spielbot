import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Card, Button, Banner, GuidedRegenButton, ActedPrompt } from '../components.jsx'
import { CastMember, RefTile, SoundtrackSlice, StagingWarnings, ActedTakes } from '../acted.jsx'

// Performance films are conditioned on CHARACTERS, not on a scene still, and the
// prompt refers to them by slot number ("<Picture 1>", "<Audio 1>"). Showing the
// prompt alone would leave you guessing which reference each number is, so every
// scene shows its slots as the thing itself: the portrait that IS Picture 1, the
// voice clip that IS Audio 1 — via the staging blocks in acted.jsx, shared with
// the Script editor's scene cards so the two screens can never drift apart.


// The dialogue and the assembled prompt are editable in place. Lines are staged
// locally and saved on demand (typing a line shouldn't re-render the film view
// on every keystroke); the prompt saves as an OVERRIDE — once set, the scene's
// fields stop rebuilding it, and "Rebuild from the scene" drops it again.
function SceneEditor({ scene, jobId, onSaved }) {
  const [lines, setLines] = useState(scene.lines)
  const prompt = scene.prompt
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  // A refresh (or another scene's save) reloads the script: adopt the new text
  // unless this card is mid-edit.
  useEffect(() => { setLines(scene.lines) }, [scene.lines])

  const dirtyLines = JSON.stringify(lines) !== JSON.stringify(scene.lines)

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
      {/* A performed SILENT take is the same shoot with nobody speaking, so it
          gets no dialogue editor: a line typed here would turn the beat into a
          conversation (and pull TTS-less speech into a wordless film). */}
      {scene.silent ? (
        <div className="stack gap-4">
          <span className="label-sm">Dialogue</span>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {scene.singing
              ? (scene.performs === false
                  ? <>♪ A music-video beat where nobody sings — the song plays over the
                      shot, but the cast doesn't mime it on camera. Re-generate the scene
                      to change that.</>
                  : <>♪ A music-video beat — the cast performs the film's song on camera
                      (the take ships muted; the sung track is the film's audio). The words
                      live in the Song tab.</>)
              : <>Nobody speaks — this beat is performed silent. Give it words by switching
                  the scene to <strong>Dialogue</strong> in the Scenes editor.</>}
          </span>
        </div>
      ) : (
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
      )}

      <ActedPrompt prompt={prompt} edited={scene.prompt_edited} busy={!!busy}
        refs={scene.pictures} audios={scene.audios}
        onSave={(text) => save('prompt', { prompt: text })}
        onRebuild={() => save('prompt', { prompt: '' })} />
    </>
  )
}


function SceneCard({ scene, seconds, jobId, workDir, voiceOpts, voiceMeta, onChanged, songUrl = '' }) {
  const [regen, setRegen] = useState('')  // '' | 'busy' | an error message
  const [opErr, setOpErr] = useState('')
  const [opBusy, setOpBusy] = useState(false)
  // The LLM rewrite of the whole scene, offered beside the re-shoot: rewrite
  // the take, then shoot it again. Singing scenes lead with the one instruction
  // music videos keep needing — a beat where the cast should NOT be miming the
  // song (the rewrite flips the scene's performs flag, which is what actually
  // removes the singing directives from the prompt).
  const rewrite = async (instr) => {
    setRegen('busy')
    try { await api.regenActedScene(jobId, scene.id, instr); setRegen(''); await onChanged() }
    catch (e) { setRegen(e.message) }
  }
  const regenButton = (
    <GuidedRegenButton size="sm" variant="ghost" icon="rotate-right"
      label="Re-generate scene" busyLabel="Rewriting…"
      busy={regen === 'busy'} disabled={regen === 'busy' || !jobId}
      chips={scene.singing
        ? ['Nobody sings in this shot', 'More movement', 'Closer in', 'Different setting']
        : scene.silent
          ? ['Slower', 'More movement', 'Closer in', 'Different setting']
          : ['Funnier', 'Simpler words', 'More back-and-forth', 'Different setting']}
      onRegen={rewrite} />
  )
  const regenHint = scene.silent
    ? 'Re-generate rewrites the whole take — action, setting, camera — and rebuilds the prompt. It stays silent.'
    : 'Re-generate rewrites the whole take — dialogue, action, setting — and rebuilds the prompt.'
  // A take-op failure (e.g. removing the first frame) must be visible even
  // before the scene has a rendered take.
  const removeFrame = async () => {
    setOpBusy(true); setOpErr('')
    try { await api.removeScenePreview(jobId, scene.id); await onChanged() }
    catch (e) { setOpErr(e.message) } finally { setOpBusy(false) }
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
          {scene.singing ? (scene.performs === false ? ' · ♪ music, nobody sings' : ' · ♪ singing')
            : scene.silent ? ' · silent' : ''}
        </span>
      </div>

      <ActedTakes scene={scene} workDir={workDir} onChanged={onChanged}
        rewriteButton={regenButton}
        hint={`${regenHint} Then shoot the scene again to film the new take.`} />

      {/* No take yet: the rewrite is still offered (there is nothing to
          re-shoot). */}
      {!scene.has_video && (
        <div className="stack gap-8">
          <div className="row gap-8 center row--wrap">{regenButton}</div>
          <span className="muted" style={{ fontSize: 12 }}>{regenHint}</span>
        </div>
      )}
      {regen && regen !== 'busy' && (
        <span style={{ fontSize: 12, color: 'var(--danger)' }}>{regen}</span>
      )}
      {opErr && <span style={{ fontSize: 12, color: 'var(--danger)' }}>{opErr}</span>}

      <SoundtrackSlice window={scene.song_window} songUrl={songUrl} />

      {/* ── Cast: each numbered slot IS the portrait / the voice clip, and the
             look and voice are set right here rather than in another tab. ── */}
      <div className="row gap-16 row--wrap" style={{ alignItems: 'flex-start' }}>
        {scene.cast.map((c) => (
          <CastMember key={c.name} c={c}
            audio={scene.audios.find((a) => a.name === c.name)}
            picture={scene.pictures.find((p) => p.name === c.name)}
            jobId={jobId} voiceOpts={voiceOpts} voiceMeta={voiceMeta} onChanged={onChanged} />
        ))}
        {scene.pictures.filter((p) => p.kind && p.kind !== 'character').map((p) => (
          <RefTile key={`ref-${p.slot}`} p={p} busy={opBusy}
            onRemoveFrame={p.kind === 'frame' ? removeFrame : null} />
        ))}
      </div>

      <StagingWarnings missingPortraits={scene.missing_portraits} unvoiced={scene.unvoiced} />

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
  const [assembling, setAssembling] = useState(false)
  const [assembleMsg, setAssembleMsg] = useState('')

  // Re-shot a scene? The published final still holds the old take until the
  // film is reassembled — same action as the classic Scenes tab.
  const reassemble = async () => {
    setAssembling(true); setError(''); setAssembleMsg('')
    try {
      const r = await api.reassembleFilm(workDir)
      setAssembleMsg(`Film reassembled from ${r.scene_count} scene(s).${r.note ? ' ' + r.note : ''}`)
    } catch (e) { setError(e.message) } finally { setAssembling(false) }
  }

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
          {/* Every take this film shoots on the reference engine, however it
              was written: the spoken ones and the performed silent beats. */}
          <span style={{ fontSize: 13 }}>
            {data.scenes.length} acted scene{data.scenes.length === 1 ? '' : 's'}
            {(() => {
              const silent = data.scenes.filter((s) => s.silent).length
              if (!silent) return ' · characters speak on screen'
              if (silent === data.scenes.length) return ' · performed silent, nobody speaks'
              return ` · ${silent} of them performed silent`
            })()} · no narrator, no music · <strong>{data.engine?.label}</strong>
          </span>
          <div className="row gap-8 center">
            {data.scenes.some((s) => s.has_video) && (
              <Button variant="primary" icon="circle-nodes" disabled={assembling || busy} onClick={reassemble}>
                {assembling ? 'Assembling…' : 'Reassemble film'}
              </Button>
            )}
            <Button variant="ghost" icon="rotate" disabled={busy} onClick={load}>
              {busy ? 'Refreshing…' : 'Refresh'}
            </Button>
          </div>
        </div>
      </Card>
      {assembleMsg && <Banner tone="ok">{assembleMsg}</Banner>}
      {data.scenes.map((s) => (
        <SceneCard key={s.id} scene={s} seconds={s.seconds} jobId={jobId || data.job_id}
          workDir={workDir} songUrl={data.song_url || ''}
          voiceOpts={voiceOpts} voiceMeta={voiceMeta} onChanged={load} />
      ))}
      {!data.scenes.length && (
        <Card span={12} well><p className="muted" style={{ fontSize: 13, margin: 0 }}>
          This script has no performance scenes.</p></Card>
      )}

    </>
  )
}
