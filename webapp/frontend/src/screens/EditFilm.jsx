import React, { useState, useEffect, useCallback, useRef } from 'react'
import { Card, Field, Button, Chip, Icon, Banner } from '../components.jsx'
import { api, fileUrl } from '../api.js'

function SceneCard({
  scene, index, total, jobId, workDir, resolution, style,
  onDelete, onMove, onSaved, onRerenderStart, onRerenderDone, initialTask,
}) {
  const [editing, setEditing] = useState(false)
  const [lightbox, setLightbox] = useState(false)
  const [title, setTitle] = useState(scene.title || '')
  const [narration, setNarration] = useState(scene.narration || '')
  const [imagePrompt, setImagePrompt] = useState(scene.image_prompt || '')
  const [videoPrompt, setVideoPrompt] = useState(scene.video_prompt || '')
  const [busy, setBusy] = useState('')
  const [taskId, setTaskId] = useState(null)
  const [taskStatus, setTaskStatus] = useState(null)
  const [error, setError] = useState('')
  const [confirmDel, setConfirmDel] = useState(false)
  const pollRef = useRef(null)
  const resumedRef = useRef(null)

  useEffect(() => {
    setTitle(scene.title || '')
    setNarration(scene.narration || '')
    setImagePrompt(scene.image_prompt || '')
    setVideoPrompt(scene.video_prompt || '')
    setEditing(false)
  }, [scene.id])

  const persist = async () => {
    try {
      await api.saveScene(jobId, scene.id, { title, narration, image_prompt: imagePrompt, video_prompt: videoPrompt })
    } catch (e) {
      setError(e.message)
    }
  }

  const saveAndClose = async () => {
    await persist()
    setEditing(false)
    onSaved()
  }

  const startPolling = useCallback((tid) => {
    if (pollRef.current) clearInterval(pollRef.current)
    setTaskId(tid)
    setTaskStatus('running')
    pollRef.current = setInterval(async () => {
      try {
        const r = await api.filmTaskStatus(tid)
        if (r.status === 'done') {
          clearInterval(pollRef.current)
          pollRef.current = null
          setTaskId(null)
          setTaskStatus(null)
          setBusy('')
          onRerenderDone()
        } else if (r.status === 'error') {
          clearInterval(pollRef.current)
          pollRef.current = null
          setTaskId(null)
          setTaskStatus(null)
          setBusy('')
          setError(r.error || 'Re-render failed')
          onRerenderDone()
        }
      } catch (e) {
        clearInterval(pollRef.current)
        pollRef.current = null
        setTaskId(null)
        setTaskStatus(null)
        setBusy('')
        setError(e.message)
        onRerenderDone()
      }
    }, 3000)
  }, [onRerenderDone])

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  // Resume polling for a re-render already running on the server (e.g. the user
  // left the edit page and came back). The parent counts these in activeRenders,
  // so we don't call onRerenderStart here. resumedRef makes this idempotent: the
  // effect must not restart polling for a task it already picked up (startPolling
  // changes identity on every parent re-render).
  useEffect(() => {
    if (initialTask && initialTask.task_id !== resumedRef.current && !pollRef.current) {
      resumedRef.current = initialTask.task_id
      setBusy(initialTask.component || '')
      startPolling(initialTask.task_id)
    }
  }, [initialTask, startPolling])

  const rerender = async (component) => {
    await persist()
    setBusy(component)
    setError('')
    onRerenderStart(component)
    try {
      const r = await api.rerenderFilmScene(workDir, scene.id, component)
      if (r.task_id) {
        startPolling(r.task_id)
      } else {
        setBusy('')
        onRerenderDone()
      }
    } catch (e) {
      setBusy('')
      setError(e.message)
      onRerenderDone()
    }
  }

  const isRendering = !!busy || !!taskId
  const previewUrl = scene.preview_url || (scene.preview_path ? fileUrl(scene.preview_path) : '')
  const videoUrl = scene.video_url

  return (
    <>
      {lightbox && previewUrl && (
        <div
          onClick={() => setLightbox(false)}
          style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            background: 'rgba(0,0,0,.88)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            cursor: 'zoom-out',
          }}
        >
          <img
            src={previewUrl}
            alt=""
            style={{ maxWidth: '92vw', maxHeight: '92vh', objectFit: 'contain', borderRadius: 6 }}
            onClick={(e) => e.stopPropagation()}
          />
          <button
            type="button"
            onClick={() => setLightbox(false)}
            style={{
              position: 'fixed', top: 18, right: 22,
              background: 'rgba(255,255,255,.15)', border: 'none', borderRadius: '50%',
              width: 36, height: 36, cursor: 'pointer',
              color: '#fff', fontSize: 18, display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            <Icon name="xmark" />
          </button>
        </div>
      )}

      <Card span={12} className="reveal" style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ display: 'flex', gap: 0 }}>
          {/* Thumbnail */}
          <div
            onClick={() => previewUrl && setLightbox(true)}
            style={{
              width: 160, flexShrink: 0, position: 'relative',
              background: 'var(--paper-2)', aspectRatio: '16/9', minHeight: 90,
              cursor: previewUrl ? 'zoom-in' : 'default',
            }}
          >
            {previewUrl
              ? <img src={previewUrl} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
              : <div className={`gfill g${index % 6}`} style={{ position: 'absolute', inset: 0 }} />
            }
            {isRendering && (
              <div style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,.6)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <Icon name="spinner" spin style={{ color: '#fff', fontSize: 20 }} />
              </div>
            )}
            <span style={{ position: 'absolute', top: 6, left: 8, fontWeight: 700, fontSize: 11, color: '#fff', background: 'rgba(0,0,0,.5)', padding: '2px 7px', borderRadius: 4 }}>
              {String(index + 1).padStart(2, '0')}
            </span>
            {previewUrl && !isRendering && (
              <span style={{ position: 'absolute', bottom: 6, right: 8, color: '#fff', opacity: 0.7, fontSize: 13 }}>
                <Icon name="magnifying-glass-plus" />
              </span>
            )}
          </div>

          {/* Content */}
          <div style={{ flex: 1, padding: '14px 16px', minWidth: 0 }}>
            {!editing ? (
              <>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, marginBottom: 6 }}>
                  <div style={{ flex: 1, fontWeight: 700, fontSize: 14 }}>{title || `Scene ${index + 1}`}</div>
                  <div className="row gap-6" style={{ flexShrink: 0 }}>
                    {videoUrl
                      ? <Chip tone={scene.has_final ? 'ok' : 'warn'} dot>{scene.has_final ? 'Rendered' : 'Partial'}</Chip>
                      : <Chip tone="warn">No video</Chip>
                    }
                  </div>
                </div>
                {narration && (
                  <p style={{ fontSize: 12.5, color: 'var(--ink-2)', margin: 0, lineHeight: 1.5, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                    {narration}
                  </p>
                )}
                {(imagePrompt || videoPrompt) && (
                  <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 2 }}>
                    {imagePrompt && (
                      <div style={{ fontSize: 11.5, color: 'var(--ink-3)', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
                        <span style={{ fontWeight: 600, marginRight: 4 }}>Image:</span>{imagePrompt}
                      </div>
                    )}
                    {videoPrompt && (
                      <div style={{ fontSize: 11.5, color: 'var(--ink-3)', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
                        <span style={{ fontWeight: 600, marginRight: 4 }}>Video:</span>{videoPrompt}
                      </div>
                    )}
                  </div>
                )}
              </>
            ) : (
              <div className="stack gap-14">
                <Field label="Title">
                  <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
                </Field>
                <Field label="Narration">
                  <textarea className="textarea" rows={3} value={narration} onChange={(e) => setNarration(e.target.value)} />
                </Field>
                <Field label="Image prompt" hint="FLUX — static frame">
                  <textarea className="textarea" rows={3} value={imagePrompt} onChange={(e) => setImagePrompt(e.target.value)} />
                </Field>
                <Field label="Video prompt" hint="LTX — motion & camera">
                  <textarea className="textarea" rows={3} value={videoPrompt} onChange={(e) => setVideoPrompt(e.target.value)} />
                </Field>
              </div>
            )}

            {error && <div style={{ fontSize: 12, color: 'var(--danger)', marginTop: 6 }}>{error}</div>}

            {/* Action row */}
            <div className="row gap-6 mt-14" style={{ flexWrap: 'wrap' }}>
              {/* Edit / Save */}
              {!editing ? (
                <Button variant="ghost" icon="pencil" size="sm" onClick={() => setEditing(true)}>Edit</Button>
              ) : (
                <>
                  <Button variant="primary" icon="check" size="sm" onClick={saveAndClose}>Save</Button>
                  <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
                </>
              )}

              {/* Re-render buttons */}
              <Button variant="ghost" icon="microphone-lines" size="sm" disabled={isRendering}
                onClick={() => rerender('narration')}>
                {busy === 'narration' ? 'Rendering…' : 'Narration'}
              </Button>
              <Button variant="ghost" icon="image" size="sm" disabled={isRendering}
                onClick={() => rerender('image')}>
                {busy === 'image' ? 'Rendering…' : 'Image'}
              </Button>
              <Button variant="ghost" icon="film" size="sm" disabled={isRendering}
                onClick={() => rerender('video')}>
                {busy === 'video' ? 'Rendering…' : 'Video'}
              </Button>

              {/* Spacer */}
              <div style={{ flex: 1 }} />

              {/* Move up/down */}
              <Button variant="ghost" icon="chevron-up" size="sm" disabled={index === 0 || isRendering} onClick={() => onMove(index, index - 1)} />
              <Button variant="ghost" icon="chevron-down" size="sm" disabled={index >= total - 1 || isRendering} onClick={() => onMove(index, index + 1)} />

              {/* Delete */}
              {confirmDel ? (
                <>
                  <Button variant="danger" icon="trash-can" size="sm" onClick={async () => { setConfirmDel(false); onDelete(scene.id) }}>Confirm</Button>
                  <Button variant="ghost" size="sm" onClick={() => setConfirmDel(false)}>Cancel</Button>
                </>
              ) : (
                <Button variant="ghost" icon="trash-can" size="sm" disabled={isRendering} onClick={() => setConfirmDel(true)}>Delete</Button>
              )}
            </div>

            {/* Video preview */}
            {videoUrl && (
              <div style={{ marginTop: 12 }}>
                <video src={videoUrl} controls style={{ width: '100%', maxWidth: 480, borderRadius: 6, background: '#000' }} />
              </div>
            )}
          </div>
        </div>
      </Card>
    </>
  )
}

export default function EditFilm({ workDir, go }) {
  const [scenes, setScenes] = useState([])
  const [jobId, setJobId] = useState('')
  const [title, setTitle] = useState('')
  const [resolution, setResolution] = useState('')
  const [style, setStyle] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [assembling, setAssembling] = useState(false)
  const [assembleResult, setAssembleResult] = useState(null)
  const [activeRenders, setActiveRenders] = useState(0)
  const [resumeTasks, setResumeTasks] = useState({})

  const load = useCallback(async () => {
    setError('')
    try {
      const r = await api.filmScenes(workDir)
      setScenes(r.scenes || [])
      setJobId(r.job_id || '')
      setTitle(r.title || '')
      setResolution(r.resolution || '')
      setStyle(r.style || '')
    } catch (e) {
      setError(e.message)
    } finally {
      setLoaded(true)
    }
  }, [workDir])

  useEffect(() => { load() }, [load])

  // On (re)load, pick up any re-render still running on the server so returning
  // to this page resumes the per-scene spinner + the "Re-rendering…" banner.
  useEffect(() => {
    api.filmTasksForWorkDir(workDir).then((r) => {
      const tasks = r.tasks || []
      const byScene = {}
      tasks.forEach((t) => { byScene[t.scene_id] = t })
      setResumeTasks(byScene)
      setActiveRenders(tasks.length)
    }).catch(() => {})
  }, [workDir])

  const handleDelete = async (sceneId) => {
    setError('')
    try {
      await api.deleteFilmScene(workDir, sceneId)
      await load()
    } catch (e) {
      setError(e.message)
    }
  }

  const handleMove = async (fromIdx, toIdx) => {
    if (toIdx < 0 || toIdx >= scenes.length) return
    const newScenes = [...scenes]
    const [moved] = newScenes.splice(fromIdx, 1)
    newScenes.splice(toIdx, 0, moved)
    setScenes(newScenes)
    try {
      await api.reorderFilmScenes(workDir, newScenes.map((s) => s.id))
    } catch (e) {
      setError(e.message)
    }
  }

  const reassemble = async () => {
    setAssembling(true)
    setError('')
    setAssembleResult(null)
    try {
      const r = await api.reassembleFilm(workDir)
      setAssembleResult(r)
    } catch (e) {
      setError(e.message)
    } finally {
      setAssembling(false)
    }
  }

  if (!loaded) {
    return (
      <div>
        <div className="page-head">
          <div className="page-head__intro">
            <span className="label-sm">Edit film</span>
            <h1 className="display-md">Loading…</h1>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Edit film · {scenes.length} scenes</span>
          <h1 className="display-md reveal reveal-d1">{title || 'Film editor'}</h1>
        </div>
        <div className="row gap-10 reveal reveal-d1">
          <Button variant="ghost" icon="arrow-left" onClick={() => go('library')}>Back</Button>
          <Button variant="primary" icon="circle-nodes" disabled={assembling || activeRenders > 0 || scenes.length === 0} onClick={reassemble}>
            {assembling ? 'Assembling…' : 'Reassemble film'}
          </Button>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>

      {activeRenders > 0 && (
        <Banner tone="info">
          <Icon name="spinner" spin /> Re-rendering {activeRenders} scene{activeRenders > 1 ? 's' : ''}… Reassemble when done.
        </Banner>
      )}

      {assembleResult && (
        <div style={{ marginBottom: 16, padding: '14px 18px', background: 'var(--ok-soft)', borderRadius: 'var(--r-md)', display: 'flex', alignItems: 'center', gap: 14 }}>
          <Icon name="circle-check" style={{ color: 'var(--ok)', fontSize: 20 }} />
          <div>
            <div style={{ fontWeight: 700, fontSize: 14 }}>Film reassembled — {assembleResult.scene_count} scenes</div>
            <div style={{ fontSize: 12.5, marginTop: 2 }}>
              <a href={assembleResult.final_url} target="_blank" rel="noopener" style={{ color: 'var(--ok)' }}>Download final video</a>
              {' · '}
              <button type="button" className="btn btn--quiet" style={{ fontSize: 12.5, padding: 0 }} onClick={() => go('remix', { workDir })}>Open in Remix</button>
            </div>
          </div>
        </div>
      )}

      {scenes.length === 0 ? (
        <Card span={12} well>
          <p className="muted" style={{ margin: 0 }}>No scenes found for this film.</p>
        </Card>
      ) : (
        <div className="bento" style={{ rowGap: 8 }}>
          {scenes.map((scene, i) => (
            <SceneCard
              key={scene.id}
              scene={scene}
              index={i}
              total={scenes.length}
              jobId={jobId}
              workDir={workDir}
              resolution={resolution}
              style={style}
              onDelete={handleDelete}
              onMove={handleMove}
              onSaved={load}
              onRerenderStart={() => setActiveRenders((n) => n + 1)}
              onRerenderDone={() => { setActiveRenders((n) => Math.max(0, n - 1)); load() }}
              initialTask={resumeTasks[scene.id]}
            />
          ))}
        </div>
      )}

      {scenes.length > 0 && (
        <div style={{ marginTop: 24, display: 'flex', justifyContent: 'flex-end' }}>
          <Button variant="primary" icon="circle-nodes" disabled={assembling || activeRenders > 0} onClick={reassemble}>
            {assembling ? 'Assembling…' : 'Reassemble film'}
          </Button>
        </div>
      )}
    </div>
  )
}
