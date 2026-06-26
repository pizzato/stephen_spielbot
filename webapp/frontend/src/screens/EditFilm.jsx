import { useState, useEffect, useCallback, useRef } from 'react'
import { Card, Field, Button, Chip, Icon, Banner, RegenLabel, VersionStrip, VideoVersionStrip, InpaintModal } from '../components.jsx'
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
  const [fieldBusy, setFieldBusy] = useState('')   // which text field is regenerating (issue #88)
  const [taskId, setTaskId] = useState(null)
  const [taskStatus, setTaskStatus] = useState(null)
  const [error, setError] = useState('')
  const [confirmDel, setConfirmDel] = useState(false)
  const [history, setHistory] = useState(scene.history)
  const [videoHistory, setVideoHistory] = useState(scene.video_history)
  const [selecting, setSelecting] = useState(false)
  const [inpaint, setInpaint] = useState(false)
  const [inpaintErr, setInpaintErr] = useState('')
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

  // Regenerate one text field with the LLM, keeping the other edits as context (issue #88).
  const setters = { title: setTitle, narration: setNarration, image_prompt: setImagePrompt, video_prompt: setVideoPrompt }
  const regenField = async (field) => {
    setFieldBusy(field); setError('')
    try {
      const r = await api.regenField(jobId, scene.id, field, { title, narration, image_prompt: imagePrompt, video_prompt: videoPrompt })
      setters[field](r.value)
    } catch (e) { setError(e.message) } finally { setFieldBusy('') }
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
        // 'cancelled' = the film was deleted out from under the task; stop
        // polling without the error banner an actual failure gets.
        if (r.status === 'done' || r.status === 'cancelled') {
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

  // Keep local history in sync when the parent reloads scenes (e.g. after an
  // image or video re-render adds a new version).
  useEffect(() => { setHistory(scene.history) }, [scene.history])
  useEffect(() => { setVideoHistory(scene.video_history) }, [scene.video_history])

  const selectVersion = async (versionId) => {
    setSelecting(true)
    setError('')
    try {
      const r = await api.selectFilmPreview(workDir, scene.id, versionId)
      setHistory(r.history)
    } catch (e) {
      setError(e.message)
    } finally {
      setSelecting(false)
    }
  }

  const selectVideoVersion = async (versionId) => {
    setSelecting(true)
    setError('')
    try {
      const r = await api.selectFilmVideo(workDir, scene.id, versionId)
      setVideoHistory(r.video_history)
    } catch (e) {
      setError(e.message)
    } finally {
      setSelecting(false)
    }
  }

  const applyInpaint = async (mask, editPrompt, denoise) => {
    setBusy('inpaint'); setInpaintErr('')
    try {
      const r = await api.inpaintFilmScene(workDir, scene.id, mask, editPrompt, denoise)
      setHistory(r.history)
      setInpaint(false)
    } catch (e) { setInpaintErr(e.message) } finally { setBusy('') }
  }

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
  // Prefer the selected kept version (each has a unique URL, so switching updates
  // the frame instantly without cache-busting); fall back to the canonical preview.
  const selectedVersion = history?.versions?.find((v) => v.id === history.selected)
  const previewUrl = selectedVersion ? fileUrl(selectedVersion.path)
    : (scene.preview_url || (scene.preview_path ? fileUrl(scene.preview_path) : ''))
  // Prefer the selected kept take (each has a unique URL, so switching updates the
  // player instantly without cache-busting); fall back to the canonical final.
  const selectedTake = videoHistory?.versions?.find((v) => v.id === videoHistory.selected)
  const videoUrl = selectedTake ? fileUrl(selectedTake.path) : scene.video_url
  // Match the panels to the film's real orientation so portrait frames aren't
  // cropped into a landscape box. Derived from the resolution name (e.g.
  // "Portrait FHD (1080×1920)"), falling back to 16/9 when it's unparseable.
  const aspect = (() => { const m = /\((\d+)[×x](\d+)\)/.exec(resolution || ''); return m ? `${m[1]} / ${m[2]}` : '16 / 9' })()

  return (
    <>
      {inpaint && (
        <InpaintModal src={previewUrl} aspect={aspect} busy={busy === 'inpaint'} error={inpaintErr}
          onApply={applyInpaint} onClose={() => setInpaint(false)} />
      )}

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
        <div>
          {/* Media: initial frame + film, side by side, equal width
              (stacks on mobile via .film-media). */}
          <div className="film-media" style={{ display: 'flex', gap: 1, background: 'var(--line)' }}>
            {/* Initial frame */}
            <div
              onClick={() => previewUrl && setLightbox(true)}
              style={{
                flex: 1, position: 'relative',
                background: 'var(--paper-2)', aspectRatio: aspect,
                cursor: previewUrl ? 'zoom-in' : 'default',
              }}
            >
              {previewUrl
                ? <img src={previewUrl} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain' }} />
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
              <span style={{ position: 'absolute', top: 6, right: 8, fontSize: 10, letterSpacing: '.04em', textTransform: 'uppercase', color: '#fff', background: 'rgba(0,0,0,.5)', padding: '2px 7px', borderRadius: 4, pointerEvents: 'none' }}>
                Initial frame
              </span>
              {previewUrl && !isRendering && (
                <span style={{ position: 'absolute', bottom: 6, right: 8, color: '#fff', opacity: 0.7, fontSize: 13 }}>
                  <Icon name="magnifying-glass-plus" />
                </span>
              )}
            </div>

            {/* Film */}
            <div style={{ flex: 1, position: 'relative', background: '#000', aspectRatio: aspect }}>
              {videoUrl
                ? <video src={videoUrl} controls style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain', background: '#000' }} />
                : (
                  <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 6, color: 'rgba(255,255,255,.55)' }}>
                    <Icon name="film" style={{ fontSize: 22 }} />
                    <span style={{ fontSize: 12 }}>No video yet</span>
                  </div>
                )
              }
              <span style={{ position: 'absolute', top: 6, left: 8, fontSize: 10, letterSpacing: '.04em', textTransform: 'uppercase', color: '#fff', background: 'rgba(0,0,0,.5)', padding: '2px 7px', borderRadius: 4, pointerEvents: 'none' }}>
                Film
              </span>
            </div>
          </div>

          {/* Content */}
          <div style={{ padding: '14px 16px' }}>
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
                <Field label={<RegenLabel busy={fieldBusy === 'title'} onRegen={() => regenField('title')}>Title</RegenLabel>}>
                  <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
                </Field>
                <Field label={<RegenLabel busy={fieldBusy === 'narration'} onRegen={() => regenField('narration')} icon="microphone-lines">Narration</RegenLabel>}>
                  <textarea className="textarea" rows={3} value={narration} onChange={(e) => setNarration(e.target.value)} />
                </Field>
                <Field label={<RegenLabel busy={fieldBusy === 'image_prompt'} onRegen={() => regenField('image_prompt')} icon="image">Image prompt</RegenLabel>} hint="FLUX — static frame">
                  <textarea className="textarea" rows={3} value={imagePrompt} onChange={(e) => setImagePrompt(e.target.value)} />
                </Field>
                <Field label={<RegenLabel busy={fieldBusy === 'video_prompt'} onRegen={() => regenField('video_prompt')} icon="film">Video prompt</RegenLabel>} hint="LTX — motion & camera">
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
              <Button variant="ghost" icon="wand-magic-sparkles" size="sm" disabled={isRendering || !previewUrl}
                onClick={() => { setInpaintErr(''); setInpaint(true) }}>
                Edit image
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

            <VersionStrip versions={history?.versions} selected={history?.selected}
              onSelect={selectVersion} aspect={aspect} busy={selecting || isRendering} />

            <VideoVersionStrip versions={videoHistory?.versions} selected={videoHistory?.selected}
              onSelect={selectVideoVersion} aspect={aspect} busy={selecting || isRendering} />
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
