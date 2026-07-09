import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Card, Field, Button, Chip, Icon, Banner, Segmented, RegenLabel,
  VersionStrip, VideoVersionStrip, MusicVersionStrip, InpaintModal,
} from '../components.jsx'
import { api, fileUrl } from '../api.js'

const resPixels = (name) => {
  const m = /\((\d+)[×x](\d+)\)/.exec(name || '')
  return m ? Number(m[1]) * Number(m[2]) : 0
}

// ── Per-scene card (Scenes tab) ───────────────────────────────────────────────

function SceneCard({
  scene, index, total, jobId, workDir, resolution, style,
  voices, filmVoice,
  onDelete, onMove, onSaved, onRerenderStart, onRerenderDone, initialTask,
}) {
  const [editing, setEditing] = useState(false)
  const [lightbox, setLightbox] = useState(false)
  const [title, setTitle] = useState(scene.title || '')
  const [narration, setNarration] = useState(scene.narration || '')
  const [voice, setVoice] = useState(scene.voice || '')
  const [imagePrompt, setImagePrompt] = useState(scene.image_prompt || '')
  const [videoPrompt, setVideoPrompt] = useState(scene.video_prompt || '')
  const [busy, setBusy] = useState('')
  const [fieldBusy, setFieldBusy] = useState('')
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
    setVoice(scene.voice || '')
    setImagePrompt(scene.image_prompt || '')
    setVideoPrompt(scene.video_prompt || '')
    setEditing(false)
  }, [scene.id])

  const persist = async () => {
    try {
      await api.saveScene(jobId, scene.id, { title, narration, voice, image_prompt: imagePrompt, video_prompt: videoPrompt })
    } catch (e) {
      setError(e.message)
    }
  }

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

  useEffect(() => {
    if (initialTask && initialTask.task_id !== resumedRef.current && !pollRef.current) {
      resumedRef.current = initialTask.task_id
      if (initialTask.status === 'error') {
        // A re-render that failed while we were away — surface it so the user
        // knows to retry instead of silently seeing the old frame/clip. It's
        // terminal, so don't start polling; the next re-render clears it.
        setError(initialTask.error || 'Re-render failed')
      } else {
        setBusy(initialTask.component || '')
        startPolling(initialTask.task_id)
      }
    }
  }, [initialTask, startPolling])

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
  const selectedVersion = history?.versions?.find((v) => v.id === history.selected)
  const previewUrl = selectedVersion ? fileUrl(selectedVersion.path)
    : (scene.preview_url || (scene.preview_path ? fileUrl(scene.preview_path) : ''))
  const selectedTake = videoHistory?.versions?.find((v) => v.id === videoHistory.selected)
  const videoUrl = selectedTake ? fileUrl(selectedTake.path) : scene.video_url
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
          <div className="film-media" style={{ display: 'flex', gap: 1, background: 'var(--line)' }}>
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

          <div style={{ padding: '14px 16px' }}>
            {!editing ? (
              <>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, marginBottom: 6 }}>
                  <div style={{ flex: 1, fontWeight: 700, fontSize: 14 }}>{title || `Scene ${index + 1}`}</div>
                  <div className="row gap-6" style={{ flexShrink: 0 }}>
                    <Chip>{scene.effective_voice || filmVoice || 'Default (F5-TTS)'}</Chip>
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
                <Field label="Narrator voice" hint="Leave on film narrator unless this scene should use a different voice. Re-render narration after changing it.">
                  <select className="select" value={voice} onChange={(e) => setVoice(e.target.value)}>
                    <option value="">Film narrator ({filmVoice || 'Default (F5-TTS)'})</option>
                    {(voices || []).map((v) => <option key={v} value={v}>{v}</option>)}
                  </select>
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

            <div className="row gap-6 mt-14" style={{ flexWrap: 'wrap' }}>
              {!editing ? (
                <Button variant="ghost" icon="pencil" size="sm" onClick={() => setEditing(true)}>Edit</Button>
              ) : (
                <>
                  <Button variant="primary" icon="check" size="sm" onClick={saveAndClose}>Save</Button>
                  <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
                </>
              )}

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
              <div style={{ flex: 1 }} />

              <Button variant="ghost" icon="chevron-up" size="sm" disabled={index === 0 || isRendering} onClick={() => onMove(index, index - 1)} />
              <Button variant="ghost" icon="chevron-down" size="sm" disabled={index >= total - 1 || isRendering} onClick={() => onMove(index, index + 1)} />

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

// ── Film tab: final cut + whole-video metadata + audio remix ──────────────────

function FilmTab({ workDir, go, meta, filmTitle, onTitleChange }) {
  const [data, setData] = useState(null)
  const [vol, setVol] = useState({ voice: 100, music: 18, ambient: 0 })
  const [musicDesc, setMusicDesc] = useState('')
  const [voice, setVoice] = useState('')
  const [musicHistory, setMusicHistory] = useState(null)
  const [videoHistory, setVideoHistory] = useState(null)
  const [musicBusy, setMusicBusy] = useState(false)
  const [narratorBusy, setNarratorBusy] = useState(false)
  const [upscaleBusy, setUpscaleBusy] = useState(false)
  const [upscaleResolution, setUpscaleResolution] = useState('')
  const [upscaleMode, setUpscaleMode] = useState('fast')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [confirmDel, setConfirmDel] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [approving, setApproving] = useState(false)
  const [aspect, setAspect] = useState('16 / 9')
  const [portrait, setPortrait] = useState(false)
  const [videoDims, setVideoDims] = useState(null)

  // Whole-video metadata (like Script Cover)
  const [coverTitle, setCoverTitle] = useState(filmTitle || '')
  const [description, setDescription] = useState('')
  const [ytBusy, setYtBusy] = useState('')
  const [metaMsg, setMetaMsg] = useState('')

  const onVideoMeta = (e) => {
    const w = e.target.videoWidth, h = e.target.videoHeight
    if (w && h) { setAspect(`${w} / ${h}`); setPortrait(h > w); setVideoDims({ w, h }) }
  }

  useEffect(() => {
    if (filmTitle) setCoverTitle((t) => t || filmTitle)
  }, [filmTitle])

  useEffect(() => {
    setError('')
    api.loadRemix(workDir)
      .then((d) => {
        setData(d)
        setVol({ voice: d.voice_vol, music: d.music_vol, ambient: d.ambient_vol })
        setVoice(d.voice || d.voices?.[0] || '')
        setMusicDesc(d.music_desc || '')
        setMusicHistory(d.music_history)
        setVideoHistory(d.video_history)
      })
      .catch((e) => setError(e.message))

    // Title + description for the finished film (same store as Cover / Publish).
    api.ytPostPrefill(workDir).then((p) => {
      if (p.title) {
        setCoverTitle(p.title)
        onTitleChange?.(p.title)
      }
      if (p.description) setDescription(p.description)
    }).catch(() => {})
  }, [workDir])

  const set = (k) => (e) => setVol((v) => ({ ...v, [k]: +e.target.value }))
  const currentResolution = data?.resolution || meta.default_resolution || ''
  const orientation = videoDims ? (videoDims.h > videoDims.w ? 'Portrait' : 'Landscape') : String(currentResolution || '').split(' ')[0]
  const currentPixels = videoDims ? videoDims.w * videoDims.h : resPixels(currentResolution)
  const upscaleOptions = (meta?.resolutions || [])
    .filter((r) => String(r || '').startsWith(`${orientation} `) || String(r || '').startsWith(`${orientation} (`))
    .filter((r) => resPixels(r) > currentPixels)

  useEffect(() => {
    if (!upscaleOptions.length) {
      setUpscaleResolution('')
      return
    }
    if (!upscaleOptions.includes(upscaleResolution)) {
      setUpscaleResolution(upscaleOptions[0])
    }
  }, [upscaleOptions.join('|')])

  const regenTitle = async () => {
    setYtBusy('title'); setError(''); setMetaMsg('')
    try {
      const r = await api.ytPostTitle(workDir, coverTitle || filmTitle || '')
      setCoverTitle(r.title || '')
      onTitleChange?.(r.title || '')
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const genDescription = async () => {
    setYtBusy('desc'); setError(''); setMetaMsg('')
    try {
      const r = await api.ytDescribe({ work_dir: workDir, title: coverTitle || filmTitle || '' })
      setDescription(r.description || '')
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const saveMeta = async () => {
    setYtBusy('save'); setError(''); setMetaMsg('')
    try {
      const title = coverTitle.trim()
      await api.ytPostSave({ work_dir: workDir, title, description })
      onTitleChange?.(title)
      setMetaMsg('Title and description saved.')
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const regenMusic = async () => {
    setMusicBusy(true); setError(''); setStatus('')
    try {
      const { task_id } = await api.regenMusic({ work_dir: data.work_dir, music_desc: musicDesc })
      await new Promise((resolve, reject) => {
        const poll = setInterval(async () => {
          try {
            const t = await api.filmTaskStatus(task_id)
            if (t.status === 'done') {
              clearInterval(poll)
              if (t.final_url) setData((d) => ({ ...d, final_url: t.final_url }))
              if (t.music_history) setMusicHistory(t.music_history)
              setStatus('Regenerated the music and re-muxed the film.')
              resolve()
            } else if (t.status === 'error' || t.status === 'cancelled') {
              clearInterval(poll); reject(new Error(t.error || `Music regen ${t.status}.`))
            }
          } catch (e) { clearInterval(poll); reject(e) }
        }, 3000)
      })
    } catch (e) { setError(e.message) } finally { setMusicBusy(false) }
  }

  const selectMusic = async (versionId) => {
    setMusicBusy(true); setError(''); setStatus('')
    try {
      const r = await api.selectMusic(data.work_dir, versionId)
      if (r.final_url) setData((d) => ({ ...d, final_url: r.final_url }))
      if (r.music_history) setMusicHistory(r.music_history)
      const chosen = r.music_history?.versions?.find((v) => v.id === r.music_history.selected)
      if (chosen && chosen.desc) setMusicDesc(chosen.desc)
      setStatus('Switched the soundtrack and re-muxed the film.')
    } catch (e) { setError(e.message) } finally { setMusicBusy(false) }
  }

  const upscaleVideo = async () => {
    setUpscaleBusy(true); setError(''); setStatus('')
    try {
      const { task_id } = await api.upscaleRemixVideo({
        work_dir: data.work_dir,
        target_resolution: upscaleResolution,
        upscale_mode: upscaleMode,
      })
      await new Promise((resolve, reject) => {
        const poll = setInterval(async () => {
          try {
            const t = await api.filmTaskStatus(task_id)
            if (t.status === 'done') {
              clearInterval(poll)
              if (t.final_url) setData((d) => ({ ...d, final_url: t.final_url, resolution: upscaleResolution }))
              if (t.video_history) setVideoHistory(t.video_history)
              setStatus('Created an upscaled final-video version.')
              resolve()
            } else if (t.status === 'error' || t.status === 'cancelled') {
              clearInterval(poll); reject(new Error(t.error || `Video upscale ${t.status}.`))
            } else if (t.step === 'final_upscale') {
              setStatus('Upscaling the final video…')
            }
          } catch (e) { clearInterval(poll); reject(e) }
        }, 3000)
      })
    } catch (e) { setError(e.message) } finally { setUpscaleBusy(false) }
  }

  const selectVideoVersion = async (versionId) => {
    setUpscaleBusy(true); setError(''); setStatus('')
    try {
      const r = await api.selectRemixVideo(data.work_dir, versionId)
      if (r.final_url) setData((d) => ({ ...d, final_url: r.final_url }))
      if (r.video_history) setVideoHistory(r.video_history)
      setStatus('Switched the final-video version.')
    } catch (e) { setError(e.message) } finally { setUpscaleBusy(false) }
  }

  const regenNarrator = async () => {
    setNarratorBusy(true); setError(''); setStatus('')
    try {
      const { task_id } = await api.regenNarrator({ work_dir: data.work_dir, voice })
      await new Promise((resolve, reject) => {
        const poll = setInterval(async () => {
          try {
            const t = await api.filmTaskStatus(task_id)
            if (t.status === 'done') {
              clearInterval(poll)
              if (t.final_url) setData((d) => ({ ...d, final_url: t.final_url }))
              setStatus('Regenerated narration and reassembled the film.')
              resolve()
            } else if (t.status === 'error' || t.status === 'cancelled') {
              clearInterval(poll); reject(new Error(t.error || `Narrator regen ${t.status}.`))
            } else if (t.step === 'narration' && t.scene_id) {
              setStatus(`Regenerating narration for scene ${t.scene_id}${t.total ? ` (${t.current}/${t.total})` : ''}…`)
            } else if (t.step === 'finalize') {
              setStatus('Reassembling the film…')
            }
          } catch (e) { clearInterval(poll); reject(e) }
        }, 3000)
      })
      setData((d) => ({ ...d, voice }))
    } catch (e) { setError(e.message) } finally { setNarratorBusy(false) }
  }

  const remix = async () => {
    setBusy(true); setError(''); setStatus('')
    try {
      const r = await api.applyRemix({
        work_dir: data.work_dir, voice_vol: vol.voice, music_vol: vol.music, ambient_vol: vol.ambient,
      })
      setStatus(r.message)
      if (r.final_url) setData((d) => ({ ...d, final_url: r.final_url + `&t=${Date.now()}` }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const approve = async () => {
    setApproving(true); setError(''); setStatus('')
    try {
      await api.publishApprove(data.work_dir || workDir)
      setData((d) => ({ ...d, approved: true, awaiting_approval: false }))
      setStatus('Approved — it will publish on the normal schedule.')
    } catch (e) { setError(e.message) } finally { setApproving(false) }
  }

  const del = async () => {
    setDeleting(true); setError('')
    try { await api.deleteFilm(data.work_dir || workDir); go('library') }
    catch (e) { setError(e.message); setConfirmDel(false) } finally { setDeleting(false) }
  }

  if (error && !data) {
    return (
      <div>
        <Banner tone="info">{error}</Banner>
        <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          The final cut isn’t ready yet — switch to <strong>Scenes</strong> to edit individual shots, or reassemble after rendering.
        </p>
      </div>
    )
  }
  if (!data) return <p className="muted">Loading final cut…</p>

  const anyBusy = busy || musicBusy || narratorBusy || upscaleBusy

  return (
    <div>
      <div className="row gap-10 row--wrap" style={{ marginBottom: 16 }}>
        {data.final_url && <a className="btn btn--ghost" href={data.final_url} download><Icon name="download" /> Download</a>}
        {data.awaiting_approval && (
          <Button variant="primary" icon="check" disabled={approving} onClick={approve}>{approving ? 'Approving…' : 'Approve'}</Button>
        )}
        <Button variant={data.awaiting_approval ? 'ghost' : 'primary'} icon="upload" onClick={() => go('publish', { publishWorkDir: data.work_dir || workDir })}>Publish</Button>
        {confirmDel ? (
          <>
            <Button variant="danger" icon="trash-can" disabled={deleting} onClick={del}>{deleting ? 'Deleting…' : 'Confirm delete'}</Button>
            <Button variant="ghost" disabled={deleting} onClick={() => setConfirmDel(false)}>Cancel</Button>
          </>
        ) : (
          <Button variant="danger" icon="trash-can" onClick={() => setConfirmDel(true)}>Delete</Button>
        )}
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}
      {metaMsg && <Banner tone="ok">{metaMsg}</Banner>}

      <div className="bento">
        <Card span={8} className="reveal reveal-d1" style={{ padding: 0, overflow: 'hidden' }}>
          <video src={data.final_url} controls onLoadedMetadata={onVideoMeta}
            style={{ display: 'block', background: '#15171a', aspectRatio: aspect, margin: '0 auto',
              width: portrait ? 'auto' : '100%', height: portrait ? '78vh' : 'auto', maxHeight: '78vh' }} />
          <div className="row center between" style={{ padding: '16px 20px' }}>
            <Chip tone="ok" dot>Final cut</Chip>
            <span className="muted mono">{data.work_dir}</span>
          </div>
          {(videoHistory?.versions?.length || 0) > 1 && (
            <div style={{ padding: '0 20px 18px' }}>
              <VideoVersionStrip versions={videoHistory?.versions} selected={videoHistory?.selected}
                onSelect={selectVideoVersion} aspect={aspect} busy={anyBusy}
                label="Final versions" hint="click to use" />
            </div>
          )}
        </Card>

        {/* Whole-video title + description (Cover-style) */}
        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm">Film details</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>Title and description for the whole video — reused when publishing.</p>
          <div className="stack gap-16 mt-24">
            <Field label={<RegenLabel busy={ytBusy === 'title'} onRegen={regenTitle}>Title</RegenLabel>} hint="Max 100 characters.">
              <input className="input" value={coverTitle} maxLength={100}
                onChange={(e) => { setCoverTitle(e.target.value); setMetaMsg('') }} />
            </Field>
            <Field label={
              <span className="row center between">
                <span>YouTube description</span>
                <button type="button" className="btn btn--quiet" style={{ padding: '4px 10px', fontSize: 12 }}
                  disabled={ytBusy === 'desc'} onClick={genDescription}>
                  <Icon name="wand-magic-sparkles" /> {ytBusy === 'desc' ? 'Writing…' : 'Generate'}
                </button>
              </span>
            }>
              <textarea className="textarea" rows={8} value={description}
                onChange={(e) => { setDescription(e.target.value); setMetaMsg('') }}
                placeholder="Written automatically when the script is generated — click Generate to rewrite it." />
            </Field>
            <Button variant="primary" block icon="floppy-disk" disabled={ytBusy === 'save' || !coverTitle.trim()} onClick={saveMeta}>
              {ytBusy === 'save' ? 'Saving…' : 'Save title & description'}
            </Button>
          </div>
        </Card>

        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm">Re-mix audio</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>Balance the levels and re-mux without re-rendering the video.</p>
          <div className="stack gap-22 mt-24">
            {[['voice', 'Voice', 'microphone-lines'], ['music', 'Music', 'music'], ['ambient', 'Ambient', 'wind']].map(([k, label, ic]) => (
              <Field key={k} label={<span className="row center gap-10"><Icon name={ic} style={{ color: 'var(--ink-3)', width: 16 }} /> {label}</span>} hint={`${vol[k]}%`}>
                <input className="slider" type="range" min={0} max={150} value={vol[k]} onChange={set(k)} />
              </Field>
            ))}
          </div>
          <div className="mt-24"><Button variant="primary" block icon="sliders" disabled={anyBusy} onClick={remix}>{busy ? 'Re-mixing…' : 'Re-mix film'}</Button></div>
        </Card>

        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm row center gap-10"><Icon name="microphone-lines" style={{ color: 'var(--ink-3)', width: 16 }} /> Narrator</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>Change the narrator for every scene and rebuild the final audio.</p>
          <div className="mt-24">
            <Field label="Narrator voice">
              <select className="select" value={voice} disabled={anyBusy}
                onChange={(e) => setVoice(e.target.value)}>
                {(data.voices || []).map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            </Field>
          </div>
          <div className="mt-24">
            <Button variant="primary" block icon="microphone-lines" disabled={anyBusy} onClick={regenNarrator}>
              {narratorBusy ? 'Regenerating…' : 'Regenerate narration'}
            </Button>
          </div>
        </Card>

        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm row center gap-10"><Icon name="up-right-and-down-left-from-center" style={{ color: 'var(--ink-3)', width: 16 }} /> Upscale video</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>Upscale the whole finished film and keep it as a selectable final version.</p>
          <div className="stack gap-14 mt-24">
            <Field label="Target resolution">
              <select className="select" value={upscaleResolution} disabled={anyBusy || upscaleOptions.length === 0}
                onChange={(e) => setUpscaleResolution(e.target.value)}>
                {upscaleOptions.length === 0
                  ? <option value="">No larger target</option>
                  : upscaleOptions.map((r) => <option key={r} value={r}>{r.replace(`${orientation} `, '')}</option>)}
              </select>
            </Field>
            <Field label="Mode">
              <select className="select" value={upscaleMode} disabled={anyBusy || !upscaleResolution}
                onChange={(e) => setUpscaleMode(e.target.value)}>
                <option value="fast">Fast</option>
                <option value="temporal_ai">AI temporal</option>
              </select>
            </Field>
          </div>
          <div className="mt-24">
            <Button variant="primary" block icon="up-right-and-down-left-from-center"
              disabled={anyBusy || !upscaleResolution}
              onClick={upscaleVideo}>
              {upscaleBusy ? 'Upscaling…' : 'Upscale film'}
            </Button>
          </div>
        </Card>

        <Card span={12} padLg className="reveal reveal-d2">
          <span className="label-sm row center gap-10"><Icon name="music" style={{ color: 'var(--ink-3)', width: 16 }} /> Background music</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>Edit the music prompt and regenerate the soundtrack. This re-runs the music model on a GPU worker, then re-muxes the film with your current levels.</p>
          <div className="mt-24">
            <Field label="Music prompt" hint="What the soundtrack should sound like">
              <textarea className="textarea" rows={3} value={musicDesc} disabled={musicBusy || upscaleBusy}
                onChange={(e) => setMusicDesc(e.target.value)}
                placeholder="cinematic orchestral background music, atmospheric, instrumental" />
            </Field>
          </div>
          <div className="mt-24"><Button variant="primary" icon="wand-magic-sparkles" disabled={anyBusy} onClick={regenMusic}>{musicBusy ? 'Regenerating music…' : 'Regenerate music'}</Button></div>
          <MusicVersionStrip versions={musicHistory?.versions} selected={musicHistory?.selected}
            onSelect={selectMusic} busy={anyBusy} />
        </Card>
      </div>
    </div>
  )
}

// ── Scenes tab ────────────────────────────────────────────────────────────────

function ScenesTab({ workDir, onTitle, onSwitchToFilm }) {
  const [scenes, setScenes] = useState([])
  const [jobId, setJobId] = useState('')
  const [resolution, setResolution] = useState('')
  const [style, setStyle] = useState('')
  const [voices, setVoices] = useState([])
  const [filmVoice, setFilmVoice] = useState('')
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
      onTitle?.(r.title || '')
      setResolution(r.resolution || '')
      setStyle(r.style || '')
      setVoices(r.voices || [])
      setFilmVoice(r.voice || 'Default (F5-TTS)')
    } catch (e) {
      setError(e.message)
    } finally {
      setLoaded(true)
    }
  }, [workDir, onTitle])

  useEffect(() => { load() }, [load])

  // On (re)load, pick up re-renders the server still knows about so returning to
  // this page restores state: running ones resume the per-scene spinner + the
  // "Re-rendering…" banner; recently-failed ones surface their error on the
  // scene (otherwise a failure while we were away just shows the old frame/clip).
  useEffect(() => {
    api.filmTasksForWorkDir(workDir).then((r) => {
      const tasks = r.tasks || []
      const byScene = {}
      tasks.forEach((t) => {
        // A running re-render always wins over a stale error for the same scene.
        const prev = byScene[t.scene_id]
        if (!prev || (prev.status === 'error' && t.status === 'running')) byScene[t.scene_id] = t
      })
      setResumeTasks(byScene)
      setActiveRenders(tasks.filter((t) => t.status === 'running').length)
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

  if (!loaded) return <p className="muted">Loading scenes…</p>

  return (
    <div>
      <div className="row gap-10 row--wrap" style={{ marginBottom: 16 }}>
        <Button variant="primary" icon="circle-nodes" disabled={assembling || activeRenders > 0 || scenes.length === 0} onClick={reassemble}>
          {assembling ? 'Assembling…' : 'Reassemble film'}
        </Button>
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
              <button type="button" className="btn btn--quiet" style={{ fontSize: 12.5, padding: 0 }} onClick={onSwitchToFilm}>View final cut</button>
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
              voices={voices}
              filmVoice={filmVoice}
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

// ── Unified Edit Film screen ──────────────────────────────────────────────────

export default function EditFilm({ workDir, go, meta = {}, initialTab = 'film' }) {
  const [tab, setTab] = useState(initialTab === 'scenes' ? 'scenes' : 'film')
  const [filmTitle, setFilmTitle] = useState('')

  // Prefill page title from film scenes (lightweight enough for the head).
  useEffect(() => {
    if (!workDir) return
    api.filmScenes(workDir).then((r) => {
      if (r.title) setFilmTitle(r.title)
    }).catch(() => {})
  }, [workDir])

  useEffect(() => {
    setTab(initialTab === 'scenes' ? 'scenes' : 'film')
  }, [workDir, initialTab])

  const label = workDir ? workDir.replace(/\/+$/, '').split('/').pop() : 'Film'
  const displayTitle = filmTitle || label

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Edit film</span>
          <h1 className="display-md reveal reveal-d1">{displayTitle}</h1>
        </div>
        <div className="row gap-10 reveal reveal-d1">
          <Button variant="ghost" icon="arrow-left" onClick={() => go('library')}>Films</Button>
        </div>
      </div>

      <div className="reveal reveal-d1" style={{ marginBottom: 20 }}>
        <Segmented value={tab} onChange={setTab} options={[
          { value: 'film', label: 'Film' },
          { value: 'scenes', label: 'Scenes' },
        ]} />
      </div>

      {tab === 'film' && (
        <FilmTab
          workDir={workDir}
          go={go}
          meta={meta}
          filmTitle={filmTitle}
          onTitleChange={setFilmTitle}
        />
      )}
      {tab === 'scenes' && (
        <ScenesTab
          workDir={workDir}
          onTitle={setFilmTitle}
          onSwitchToFilm={() => setTab('film')}
        />
      )}
    </div>
  )
}
