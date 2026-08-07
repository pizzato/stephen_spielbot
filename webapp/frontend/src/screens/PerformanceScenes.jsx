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


function SceneCard({ scene, seconds, jobId, voiceOpts, voiceMeta, onChanged }) {
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

      {/* ── What happens, and what is said ── */}
      <div className="row gap-16 row--wrap" style={{ alignItems: 'flex-start' }}>
        <div className="stack gap-8" style={{ flex: '1 1 320px', minWidth: 0 }}>
          <span className="label-sm">Action</span>
          {scene.beats.map((b, i) => (
            <div key={i} style={{ fontSize: 13.5 }}>
              <code style={{ opacity: 0.7 }}>{b.t0}s–{b.t1}s</code> {b.action}
            </div>
          ))}
          {!scene.beats.length && <span className="muted" style={{ fontSize: 13 }}>No beats.</span>}
        </div>
        <div className="stack gap-8" style={{ flex: '1 1 320px', minWidth: 0 }}>
          <span className="label-sm">Dialogue</span>
          {scene.lines.map((l, i) => (
            <div key={i} style={{ fontSize: 13.5 }}>
              <strong>{l.speaker}</strong>
              <span className="muted"> ({l.delivery})</span><br />“{l.text}”
            </div>
          ))}
          {!scene.lines.length && <span className="muted" style={{ fontSize: 13 }}>Nobody speaks.</span>}
        </div>
      </div>

      <div className="row gap-16 row--wrap" style={{ fontSize: 13 }}>
        <div style={{ flex: '1 1 320px' }}><span className="label-sm">Camera</span><div>{scene.camera || '—'}</div></div>
        <div style={{ flex: '1 1 320px' }}><span className="label-sm">Sound</span><div>{scene.soundscape || '—'}</div></div>
      </div>

      <details>
        <summary style={{ cursor: 'pointer', fontSize: 12.5 }} className="muted">
          Exact prompt sent to the video model
        </summary>
        <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, marginTop: 10,
                      background: 'var(--well, rgba(127,127,127,.08))', padding: 12,
                      borderRadius: 8, overflowX: 'auto' }}>{scene.prompt}</pre>
      </details>
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
        <SceneCard key={s.id} scene={s} seconds={s.seconds} jobId={jobId}
          voiceOpts={voiceOpts} voiceMeta={voiceMeta} onChanged={load} />
      ))}
      {!data.scenes.length && (
        <Card span={12} well><p className="muted" style={{ fontSize: 13, margin: 0 }}>
          This script has no performance scenes.</p></Card>
      )}
    </>
  )
}
