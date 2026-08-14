import { useState, useEffect, useRef } from 'react'
import { Card, Chip, Button, ProgressBar, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

const STATUS_TONE = {
  running: 'ok', leased: 'info', queued: 'accent', succeeded: 'neutral',
  failed_retryable: 'warn', failed_terminal: 'danger', lost: 'warn', cancelled: 'neutral',
}

// What each render worker is doing right now (issue #98 — no dedicated ui workers).
const WORKER_TONE = { working: 'ok', reserved: 'info', idle: 'neutral' }
const WORKER_LABEL = { working: 'working', reserved: 'UI', idle: 'idle' }
const workerLine = (w) =>
  w.state === 'working' ? w.job : w.state === 'reserved' ? 'Reserved for the UI' : 'Idle'

// Short display name from a worker URL or bare host (e.g. http://s1:8188 → s1).
const shortHost = (url) => { try { return new URL(url).hostname } catch { return url } }

// How far along one scene is, from the files it has on disk so far.
const sceneStage = (s) =>
  s.has_final ? ['ok', 'Rendered']
    : s.has_video ? ['info', 'Video']
      : s.has_narration ? ['accent', 'Voiced']
        : ['neutral', 'Waiting']

// One read-only scene on the wall: the clip as soon as it exists, else the
// first frame, else a placeholder — so a running render can be checked without
// opening the work folder (or risking the film editor's mutating actions).
function SceneTile({ scene, index, aspect }) {
  const [tone, label] = sceneStage(scene)
  return (
    <div style={{ border: '1px solid var(--line)', borderRadius: 'var(--r-md)', overflow: 'hidden', background: 'var(--paper-2)' }}>
      <div style={{ position: 'relative', aspectRatio: aspect, background: '#000' }}>
        {scene.video_url
          ? <video src={scene.video_url} poster={scene.preview_url || undefined} preload="none" controls
              style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain', background: '#000' }} />
          : scene.preview_url
            ? <img src={scene.preview_url} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain' }} />
            : <div className={`gfill g${index % 6}`} style={{ position: 'absolute', inset: 0 }} />
        }
        <span style={{ position: 'absolute', top: 6, left: 8, fontWeight: 700, fontSize: 11, color: '#fff', background: 'rgba(0,0,0,.5)', padding: '2px 7px', borderRadius: 4, pointerEvents: 'none' }}>
          {String(index + 1).padStart(2, '0')}
        </span>
      </div>
      <div style={{ padding: '8px 10px' }}>
        <div className="row center between gap-6">
          <div style={{ fontSize: 12.5, fontWeight: 600, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {scene.title || `Scene ${index + 1}`}
          </div>
          <Chip tone={tone} dot>{label}</Chip>
        </div>
        {!scene.video_url && scene.has_narration && (
          <audio src={scene.narration_url} controls preload="none" style={{ width: '100%', height: 30, marginTop: 6 }} />
        )}
      </div>
    </div>
  )
}

export default function Progress({ workDir, job, go, onOpenScript }) {
  const [p, setP] = useState(null)
  const [wall, setWall] = useState(null)   // /api/films/scenes payload — the read-only scene wall
  const [error, setError] = useState('')
  const [action, setAction] = useState('')
  const timer = useRef(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const data = await api.getProgress(workDir)
        if (alive) { setP(data); setError('') }
      } catch (e) { if (alive) setError(e.message) }
    }
    tick()
    timer.current = setInterval(tick, 2500)
    return () => { alive = false; clearInterval(timer.current) }
  }, [workDir])

  // Poll the scene files too (same source as the film editor), so intermediary
  // clips and narrations are visible while the render is still going. Quietly
  // absent until the work dir and script exist.
  const wallDir = p?.work_dir || workDir
  useEffect(() => {
    if (!wallDir) return
    let alive = true
    const tick = async () => {
      try {
        const data = await api.filmScenes(wallDir)
        if (alive) setWall(data)
      } catch { /* work dir not created or script not divided yet */ }
    }
    tick()
    const t = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [wallDir])

  const pct = p?.pct ?? 0
  const done = p?.done
  const tasks = p?.tasks || []
  const counts = p?.counts || {}
  const title = p?.title || job?.title || 'Rendering'
  const eta = p?.eta
  const fmtSecs = (s) => (s < 90 ? `${Math.round(s)}s` : `${Math.round(s / 60)} min`)

  const doAction = async (fn) => {
    setAction('busy'); setError('')
    try { const r = await fn(p?.work_dir || workDir); if (r?.message) setError('') } catch (e) { setError(e.message) } finally { setAction('') }
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Rendering</span>
          <h1 className="display-md reveal reveal-d1">{title}</h1>
        </div>
        <div className="row center gap-10 reveal reveal-d1">
          <Button variant="ghost" icon="feather-pointed" disabled={action === 'script' || !(p?.work_dir || workDir)}
            onClick={async () => {
              setAction('script'); setError('')
              try { await onOpenScript(p?.work_dir || workDir) } catch (e) { setError(e.message); setAction('') }
            }}>{action === 'script' ? 'Opening…' : 'Edit script'}</Button>
          {!done && eta?.eta_text && <Chip tone="accent" dot>{eta.eta_text} left</Chip>}
          {done ? <Chip tone="ok" dot>Done</Chip> : <Chip tone="info" dot>{Math.round(pct)}%</Chip>}
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>

      <div className="bento">
        <Card span={8} padLg className="reveal reveal-d1">
          <div className="row center between">
            <span style={{ fontWeight: 600 }}>{p?.msg || 'Waiting to start…'}</span>
            <span className="muted mono">{Object.entries(counts).map(([k, v]) => `${k}:${v}`).join('  ·  ')}</span>
          </div>
          <div className="mt-16"><ProgressBar pct={pct} /></div>

          {done && (
            <div className="row gap-10 mt-24">
              <Button variant="primary" icon="sliders" onClick={() => go('editfilm', { workDir: p?.work_dir || workDir })}>Edit film</Button>
              {p?.final_url && <a className="btn btn--ghost" href={p.final_url} download><Icon name="download" /> Download</a>}
            </div>
          )}

          <div className="mt-24">
            <span className="label-sm">Tasks</span>
            <div className="stack mt-16" style={{ maxHeight: 360, overflow: 'auto' }}>
              {tasks.length === 0 && <div className="muted" style={{ fontSize: 13 }}>No durable tasks recorded yet.</div>}
              {tasks.map((t, i) => (
                <div key={i} className="row center between" style={{ padding: '8px 0', borderTop: i ? '1px solid var(--line)' : 'none' }}>
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 13.5, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.name}</div>
                    {t.error ? <div className="muted" style={{ fontSize: 12, color: 'var(--danger)' }}>{String(t.error).slice(0, 100)}</div> : null}
                  </div>
                  <span className="muted mono" style={{ fontSize: 11 }}>{t.attempt}/{t.max_attempts}</span>
                  <Chip tone={STATUS_TONE[t.status] || 'neutral'} dot>{t.status}</Chip>
                </div>
              ))}
            </div>
          </div>
        </Card>

        <div className="col-4 stack gap-16">
          {!done && eta && (
            <Card className="reveal reveal-d1">
              <div className="row center between">
                <span className="label-sm">Time estimate</span>
                {eta.confidence === 'rough'
                  ? <Chip tone="warn" dot>rough</Chip>
                  : <Chip tone="ok" dot>learned</Chip>}
              </div>
              <div className="row center gap-10 mt-16" style={{ alignItems: 'baseline' }}>
                <span style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.02em' }}>{eta.eta_text}</span>
                <span className="muted" style={{ fontSize: 13 }}>remaining</span>
              </div>
              <div className="muted mono mt-8" style={{ fontSize: 11 }}>
                full render {eta.total_text} · {eta.workers.comfy}× comfy{eta.workers.comfy_reserved ? ` (+${eta.workers.comfy_reserved} held for UI)` : ''} · {eta.workers.tts}× tts
              </div>
              {eta.confidence === 'rough' && (
                <div className="muted mt-8" style={{ fontSize: 11.5 }}>
                  First render builds the timing table — estimates sharpen next time.
                </div>
              )}
              <div className="stack gap-6 mt-16">
                {(eta.table || []).map((r, i) => (
                  <div key={i} className="row center between" style={{ fontSize: 12.5 }}>
                    <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {r.label}{r.estimated ? <span className="muted"> · est</span> : null}
                    </span>
                    <span className="muted mono" style={{ whiteSpace: 'nowrap', paddingLeft: 8 }}>
                      {fmtSecs(r.seconds)}{r.count > 1 ? ` ×${r.count}` : ''}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card className="reveal reveal-d2">
            <span className="label-sm">Workers</span>
            <div className="stack gap-10 mt-16">
              {(p?.workers || []).length === 0 && <div className="muted" style={{ fontSize: 13 }}>No workers configured.</div>}
              {(p?.workers || []).map((w, i) => (
                <div key={i} className="row center between gap-10">
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{workerLine(w)}</div>
                    <div className="muted" style={{ fontSize: 11 }}>{shortHost(w.endpoint)} · {w.kind}</div>
                  </div>
                  <Chip tone={WORKER_TONE[w.state] || 'neutral'} dot>{WORKER_LABEL[w.state] || w.state}</Chip>
                </div>
              ))}
            </div>
          </Card>

          {p?.cover_url && (
            <Card className="reveal reveal-d3">
              <span className="label-sm">Thumbnail</span>
              <div className="mt-16" style={{ position: 'relative', borderRadius: 'var(--r-md)', overflow: 'hidden', aspectRatio: '16/9' }}>
                <img src={p.cover_url} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
              </div>
            </Card>
          )}

          <Card well className="reveal reveal-d4">
            <span className="label-sm">Controls</span>
            <div className="row gap-10 mt-16 row--wrap">
              <Button variant="ghost" icon="pause" disabled={action === 'busy' || done} onClick={() => doAction(api.pauseJob)}>Pause</Button>
              <Button variant="ghost" icon="rotate-right" disabled={action === 'busy'} onClick={() => doAction(api.retryJob)}>Retry failed</Button>
              <Button variant="ghost" icon="play" disabled={action === 'busy'} onClick={() => doAction(api.resumeJob)}>Resume</Button>
              <Button variant="danger" icon="stop" disabled={action === 'busy'} onClick={() => doAction(api.cancelJob)}>Cancel</Button>
            </div>
            <div className="mt-10">
              <Button variant="danger" icon="trash-can" disabled={action === 'busy'} onClick={async () => {
                setAction('busy'); setError('')
                try { await api.deleteJob(p?.work_dir || workDir); go('library') }
                catch (e) { setError(e.message); setAction('') }
              }}>Delete job &amp; files</Button>
            </div>
          </Card>
        </div>

        {wall?.scenes?.length > 0 && (() => {
          const m = /\((\d+)[×x](\d+)\)/.exec(wall.resolution || '')
          const aspect = m ? `${m[1]} / ${m[2]}` : '16 / 9'
          const rendered = wall.scenes.filter((s) => s.has_final).length
          return (
            <Card span={12} padLg className="reveal reveal-d2">
              <div className="row center between">
                <span className="label-sm">Scenes</span>
                <span className="muted mono" style={{ fontSize: 11 }}>{rendered}/{wall.scenes.length} rendered</span>
              </div>
              <div className="mt-16" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 14 }}>
                {wall.scenes.map((s, i) => <SceneTile key={s.id} scene={s} index={i} aspect={aspect} />)}
              </div>
            </Card>
          )
        })()}
      </div>
    </div>
  )
}
