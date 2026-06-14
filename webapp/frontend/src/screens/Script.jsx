import { useState, useEffect } from 'react'
import { Card, Field, Segmented, ResolutionPicker, Button, Chip, Icon, Thumb, Banner } from '../components.jsx'
import { api, fileUrl } from '../api.js'

export default function Script({ job, setJob, meta, onGenerate, go }) {
  const [view, setView] = useState(job ? 'cover' : 'scripts')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  // Scripts tab
  const [savedScripts, setSavedScripts] = useState([])
  const [confirmDel, setConfirmDel] = useState('')

  // Shared (used across Cover + Scenes)
  const [style, setStyle] = useState(job?.style || '')
  const [resolution, setResolution] = useState(job?.resolution || meta.default_resolution || '')

  // Cover tab
  const [coverTitle, setCoverTitle] = useState(job?.title || '')
  const [description, setDescription] = useState('')
  const [coverUrl, setCoverUrl] = useState('')
  const [ytBusy, setYtBusy] = useState('')

  // Scenes tab
  const [scenes, setScenes] = useState(job?.scenes || [])
  const [cur, setCur] = useState(0)
  const [lightbox, setLightbox] = useState(null)
  const [genAll, setGenAll] = useState(false)
  const [genAllMsg, setGenAllMsg] = useState('')
  const [regenStatus, setRegenStatus] = useState('')
  const [fieldBusy, setFieldBusy] = useState('')
  const [confirmDelScript, setConfirmDelScript] = useState(false)

  // Sync state and switch to Cover when a new job loads
  useEffect(() => {
    setScenes(job?.scenes || [])
    setCur(0)
    setStyle(job?.style || '')
    setResolution(job?.resolution || meta.config?.resolution || meta.default_resolution || '')
    setCoverTitle(job?.title || '')
    setDescription('')
    setCoverUrl('')
    if (job?.job_id) setView('cover')
  }, [job?.job_id, meta.config?.resolution, meta.default_resolution])

  // Load saved description + cover whenever the Cover tab is opened. A fresh
  // script's description is written by a background task right after
  // generation, so if it isn't there yet keep polling briefly until it lands.
  useEffect(() => {
    if (view !== 'cover' || !job?.work_dir) return
    let alive = true
    let tries = 0
    let timer = null
    const load = async () => {
      try {
        const p = await api.ytPostPrefill(job.work_dir)
        if (!alive) return
        if (p.description) setDescription((cur) => cur || p.description)
        setCoverUrl(p.cover_url || '')
        if (!p.description && tries++ < 10) timer = setTimeout(load, 3000)
      } catch { /* prefill is best-effort */ }
    }
    load()
    return () => { alive = false; clearTimeout(timer) }
  }, [view, job?.work_dir])

  const refreshScripts = () => api.listJobs()
    .then((d) => setSavedScripts(d.scripts || []))
    .catch(() => {})
  useEffect(() => { refreshScripts() }, [])

  // Generate any missing scene previews as soon as the script loads
  useEffect(() => {
    if (!job?.job_id) return
    if (!(job.scenes || []).some((s) => !s.has_preview)) return
    setGenAll(true)
    setGenAllMsg('Generating missing scene previews…')
    // Generate previews at the SAME resolution the render will use (what approve
    // sends), so the render reuses these images instead of regenerating them at a
    // different size. Mirrors the `resolution` state init below.
    api.generateAllPreviews(job.job_id, job.resolution || meta.config?.resolution || meta.default_resolution || '', job.style || '')
      .then((r) => {
        if (r.scenes) setScenes((prev) => prev.map((s) => {
          const u = r.scenes.find((x) => x.id === s.id)
          return u ? { ...s, preview_path: u.preview_path, has_preview: u.has_preview } : s
        }))
      })
      .catch((e) => setError(e.message))
      .finally(() => setGenAll(false))
  }, [job?.job_id])

  // ── Scripts tab ──────────────────────────────────────────────────────────────
  const loadScript = async (workDir) => {
    setBusy('load:' + workDir); setError('')
    try {
      const loaded = await api.loadScript(workDir)
      setJob({
        ...loaded,
        voice: loaded.voice || meta.config?.default_voice || '',
        voice_robotic: loaded.voice_robotic ?? !!meta.config?.default_voice_robotic,
        resolution: loaded.resolution || meta.config?.resolution || meta.default_resolution || '',
      })
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const deleteScript = async (workDir) => {
    setBusy('del:' + workDir); setError('')
    try {
      await api.deleteJob(workDir)
      setConfirmDel('')
      if (job?.work_dir === workDir) setJob(null)
      await refreshScripts()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  // ── Cover tab ─────────────────────────────────────────────────────────────────
  const genDescription = async () => {
    setYtBusy('desc'); setError('')
    try {
      const r = await api.ytDescribe({ work_dir: job.work_dir, title: coverTitle || job.title || '' })
      setDescription(r.description || '')
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const regenCover = async () => {
    setYtBusy('cover'); setError('')
    let pollTimer = null
    try {
      const { task_id: tid } = await api.ytCover({ work_dir: job.work_dir, title: coverTitle || job.title || '' })
      await new Promise((resolve, reject) => {
        const check = async () => {
          try {
            const s = await api.ytCoverStatus(tid)
            if (s.status === 'succeeded') { setCoverUrl(s.cover_url || ''); resolve() }
            else if (s.status === 'failed_terminal') reject(new Error(s.error || 'Cover generation failed'))
            else pollTimer = setTimeout(check, 2000)
          } catch (e) { reject(e) }
        }
        check()
      })
    } catch (e) { setError(e.message) } finally {
      clearTimeout(pollTimer)
      setYtBusy('')
    }
  }

  const deleteCurrent = async () => {
    setBusy('delete'); setError('')
    try {
      await api.deleteJob(job.work_dir)
      setJob(null)
      setView('scripts')
      await refreshScripts()
    } catch (e) { setError(e.message); setBusy('') }
  }

  const regenAll = async () => {
    setGenAll(true)
    setGenAllMsg('Regenerating all scene images — this may take several minutes…')
    setError('')
    setRegenStatus('')
    try {
      const r = await api.regenAllPreviews(job.job_id, resolution, style)
      if (r.scenes) setScenes((prev) => prev.map((s) => {
        const u = r.scenes.find((x) => x.id === s.id)
        return u ? { ...s, preview_path: u.preview_path, has_preview: u.has_preview, cb: Date.now() } : s
      }))
      const generated = r.generated ?? 0
      const failedCount = r.failed?.length ?? 0
      setRegenStatus(failedCount > 0
        ? `Regenerated ${generated} scene image${generated !== 1 ? 's' : ''} (${failedCount} failed)`
        : `Regenerated ${generated} scene image${generated !== 1 ? 's' : ''}`)
    } catch (e) { setError(e.message) } finally { setGenAll(false) }
  }

  // ── Scenes tab ────────────────────────────────────────────────────────────────
  const total = scenes.length
  const d = scenes[cur] || {}
  const setField = (k, v) => setScenes((arr) => arr.map((s, i) => i === cur ? { ...s, [k]: v } : s))
  const aspect = (() => { const m = /\((\d+)[×x](\d+)\)/.exec(resolution || ''); return m ? `${m[1]} / ${m[2]}` : '16 / 9' })()
  const imgUrl = (s) => (s && s.preview_path) ? fileUrl(s.preview_path) + (s.cb ? `&t=${s.cb}` : '') : ''

  const regenField = async (field) => {
    setFieldBusy(field); setError('')
    try {
      const r = await api.regenField(job.job_id, d.id, field, {
        title: d.title || '', narration: d.narration || '',
        image_prompt: d.image_prompt || '', video_prompt: d.video_prompt || '',
      })
      setField(field, r.value)
    } catch (e) { setError(e.message) } finally { setFieldBusy('') }
  }

  const fieldLabel = (text, field, icon) => (
    <span className="row center between">
      <span className="row center gap-10">{icon ? <Icon name={icon} style={{ color: 'var(--ink-3)', width: 16 }} /> : null}{text}</span>
      <button type="button" className="btn btn--quiet" style={{ padding: '3px 9px', fontSize: 11 }}
        disabled={fieldBusy === field} onClick={(e) => { e.preventDefault(); e.stopPropagation(); regenField(field) }}>
        <Icon name="rotate" /> {fieldBusy === field ? 'Writing…' : 'Re-generate'}
      </button>
    </span>
  )

  const persist = async (idx = cur) => {
    const s = scenes[idx]
    if (!s) return
    try {
      await api.saveScene(job.job_id, s.id, {
        title: s.title || '', image_prompt: s.image_prompt || '',
        video_prompt: s.video_prompt || '', narration: s.narration || '',
      })
    } catch (e) { setError(e.message) }
  }

  const move = async (to) => {
    if (to < 0 || to >= total) return
    await persist(cur)
    setCur(to)
  }

  const regen = async () => {
    setBusy('preview'); setError('')
    try {
      await persist(cur)
      const r = await api.regenPreview(job.job_id, scenes[cur].id, resolution, style)
      setScenes((arr) => arr.map((s, i) => i === cur ? { ...s, preview_path: r.preview_path, has_preview: true, cb: Date.now() } : s))
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const approve = async () => {
    setBusy('generate'); setError('')
    try {
      await persist(cur)
      const r = await api.queueFromJob({
        job_id: job.job_id, work_dir: job.work_dir,
        video_title: job.video_title || job.title || '', n_scenes: total,
        style, resolution, voice: job.voice || '', voice_robotic: job.voice_robotic,
        music_desc: job.music_desc || '',
        queue_item_id: job.queue_item_id || '',
        style_name: job.style_name || '',
      })
      setJob({ ...job, scenes, style })
      if (r.started) onGenerate(job.work_dir)
      else go('queue')
    } catch (e) { setError(e.message); setBusy('') }
  }

  // ── Render ────────────────────────────────────────────────────────────────────
  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Script{job && view === 'scenes' ? ` · ${total} scenes` : ''}</span>
          <h1 className="display-md reveal reveal-d1">{job ? job.title : 'Scripts'}</h1>
        </div>
        <div className="row gap-10 reveal reveal-d1 row--wrap">
          {view === 'scripts' && (
            <Button variant="primary" icon="plus" onClick={() => go('create')}>New script</Button>
          )}
          {view === 'cover' && job && (
            <>
              <Button variant="ghost" icon="rotate" onClick={() => go('create')}>Re-draft</Button>
              {confirmDelScript ? (
                <>
                  <Button variant="danger" icon="trash-can" disabled={busy === 'delete'} onClick={deleteCurrent}>
                    {busy === 'delete' ? 'Deleting…' : 'Confirm delete'}
                  </Button>
                  <Button variant="ghost" disabled={busy === 'delete'} onClick={() => setConfirmDelScript(false)}>Cancel</Button>
                </>
              ) : (
                <Button variant="ghost" icon="trash-can" onClick={() => setConfirmDelScript(true)}>Delete</Button>
              )}
            </>
          )}
          {view === 'scenes' && job && (
            <Button variant="primary" iconRight="layer-group" disabled={busy === 'generate'}
              onClick={approve}>{busy === 'generate' ? 'Approving…' : job.queue_item_id ? '2. Save to queue slot' : '2. Approve → queue'}</Button>
          )}
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {job?.queue_item_id && view === 'scenes' && <Banner tone="info">Editing a queued request — “Save to queue slot” keeps its position and lets it render straight from this script.</Banner>}
      {genAll && <Banner tone="info">{genAllMsg}</Banner>}
      {!genAll && regenStatus && <Banner tone="ok">{regenStatus}</Banner>}

      <div className="reveal reveal-d1" style={{ marginBottom: 20 }}>
        <Segmented value={view} onChange={(v) => { setView(v); setError('') }} options={[
          { value: 'scripts', label: 'Scripts' },
          { value: 'cover', label: 'Cover' },
          { value: 'scenes', label: 'Scenes' },
        ]} />
      </div>

      {/* ── Scripts tab ─────────────────────────────────────────────────────── */}
      {view === 'scripts' && (
        <div className="bento">
          {savedScripts.length === 0 && (
            <Card span={12} well>
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>No saved scripts yet. Click <strong>New script</strong> to create one.</p>
            </Card>
          )}
          {savedScripts.map((s, i) => (
            <Card key={s.work_dir} span={4} className={`reveal reveal-d${(i % 3) + 1}`}>
              <div className="row center between">
                <span style={{ fontWeight: 700 }}>{s.label}</span>
                {job?.work_dir === s.work_dir && <Chip tone="ok" dot>Loaded</Chip>}
              </div>
              <div className="row gap-10 mt-16 row--wrap">
                <Button variant="primary" icon="folder-open" disabled={!!busy} onClick={() => loadScript(s.work_dir)}>
                  {busy === 'load:' + s.work_dir ? 'Loading…' : job?.work_dir === s.work_dir ? 'Reload' : 'Load'}
                </Button>
                {confirmDel === s.work_dir ? (
                  <>
                    <Button variant="danger" icon="trash-can" disabled={busy === 'del:' + s.work_dir} onClick={() => deleteScript(s.work_dir)}>
                      {busy === 'del:' + s.work_dir ? 'Deleting…' : 'Confirm delete'}
                    </Button>
                    <Button variant="ghost" disabled={!!busy} onClick={() => setConfirmDel('')}>Cancel</Button>
                  </>
                ) : (
                  <Button variant="ghost" icon="trash-can" onClick={() => setConfirmDel(s.work_dir)}>Delete</Button>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* ── Cover tab (no script) ────────────────────────────────────────────── */}
      {view === 'cover' && !job && (
        <div className="bento">
          <Card span={7} well>
            <p className="body-1" style={{ margin: 0 }}>Load a script first to edit its cover settings.</p>
            <div className="row gap-10 mt-16">
              <Button variant="primary" icon="folder-open" onClick={() => setView('scripts')}>Browse scripts</Button>
              <Button variant="ghost" icon="wand-magic-sparkles" onClick={() => go('create')}>Create new</Button>
            </div>
          </Card>
        </div>
      )}

      {/* ── Cover tab (script loaded) ────────────────────────────────────────── */}
      {view === 'cover' && job && (
        <div className="bento">
          <Card span={8} padLg className="reveal reveal-d1">
            <div className="stack gap-22">
              <Field label="Title">
                <input className="input" value={coverTitle} onChange={(e) => setCoverTitle(e.target.value)} />
              </Field>
              <Field label="Visual style — applied to every scene">
                <input className="input" value={style} onChange={(e) => setStyle(e.target.value)} />
              </Field>
              <Field label="Resolution">
                <ResolutionPicker value={resolution} onChange={setResolution} meta={meta} />
              </Field>
              <div>
                <Button variant="ghost" icon="rotate-right" disabled={genAll} onClick={regenAll}>
                  {genAll ? 'Regenerating all scenes…' : 'Regenerate all scene images'}
                </Button>
              </div>
              <Field label={
                <span className="row center between">
                  <span>YouTube description</span>
                  <button className="btn btn--quiet" style={{ padding: '4px 10px', fontSize: 12 }}
                    disabled={ytBusy === 'desc'} onClick={genDescription}>
                    <Icon name="wand-magic-sparkles" /> {ytBusy === 'desc' ? 'Writing…' : 'Generate'}
                  </button>
                </span>
              }>
                <textarea className="textarea" rows={8} value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Written automatically when the script is generated — click Generate to rewrite it." />
              </Field>
            </div>
          </Card>

          <div className="col-4 stack gap-16">
            <Card className="reveal reveal-d2">
              <span className="label-sm">Cover image</span>
              <div className="mt-16" style={{ position: 'relative', borderRadius: 'var(--r-md)', overflow: 'hidden', aspectRatio: aspect }}>
                {coverUrl
                  ? <img src={coverUrl} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
                  : <div className="gfill g2" style={{ position: 'absolute', inset: 0 }}></div>}
              </div>
              <Button variant="ghost" block icon="rotate-right" disabled={ytBusy === 'cover'} onClick={regenCover}>
                {ytBusy === 'cover' ? 'Generating…' : coverUrl ? 'Regenerate cover' : 'Generate cover'}
              </Button>
            </Card>
            <Card well className="reveal reveal-d3">
              <div className="row center gap-10">
                <Icon name="circle-info" style={{ color: 'var(--ink-3)' }} />
                <span className="muted" style={{ fontSize: 12.5 }}>Description and cover are saved to the script folder and reused when publishing.</span>
              </div>
            </Card>
          </div>
        </div>
      )}

      {/* ── Scenes tab (no script) ───────────────────────────────────────────── */}
      {view === 'scenes' && !job && (
        <div className="bento">
          <Card span={7} well>
            <p className="body-1" style={{ margin: 0 }}>Load a script first to edit its scenes.</p>
            <div className="row gap-10 mt-16">
              <Button variant="primary" icon="folder-open" onClick={() => setView('scripts')}>Browse scripts</Button>
              <Button variant="ghost" icon="wand-magic-sparkles" onClick={() => go('create')}>Create new</Button>
            </div>
          </Card>
        </div>
      )}

      {/* ── Scenes tab (script loaded) ───────────────────────────────────────── */}
      {view === 'scenes' && job && (
        <>
          <div className="bento">
            <Card span={8} padLg className="reveal reveal-d2">
              <div className="row center between">
                <div className="row center gap-10">
                  <Button variant="quiet" icon="chevron-left" disabled={cur === 0} onClick={() => move(cur - 1)}>Prev</Button>
                  <span className="h-title">Scene {cur + 1}<span className="muted" style={{ fontWeight: 400 }}> / {total}</span></span>
                  <Button variant="quiet" iconRight="chevron-right" disabled={cur >= total - 1} onClick={() => move(cur + 1)}>Next</Button>
                </div>
                <Chip tone="accent" dot>~20s</Chip>
              </div>

              <div className="stack gap-22 mt-24">
                <Field label={fieldLabel('Scene title', 'title')}>
                  <input className="input" value={d.title || ''} onChange={(e) => setField('title', e.target.value)} onBlur={() => persist(cur)} />
                </Field>
                <Field label={fieldLabel('Narration', 'narration', 'microphone-lines')}>
                  <textarea className="textarea" rows={4} value={d.narration || ''} onChange={(e) => setField('narration', e.target.value)} onBlur={() => persist(cur)} />
                </Field>
                <Field label={fieldLabel('Image prompt', 'image_prompt', 'image')} hint="FLUX — static, highly detailed.">
                  <textarea className="textarea" rows={4} value={d.image_prompt || ''} onChange={(e) => setField('image_prompt', e.target.value)} onBlur={() => persist(cur)} />
                </Field>
                <Field label={fieldLabel('Video prompt', 'video_prompt', 'film')} hint="LTX — motion & camera.">
                  <textarea className="textarea" rows={5} value={d.video_prompt || ''} onChange={(e) => setField('video_prompt', e.target.value)} onBlur={() => persist(cur)} />
                </Field>
              </div>
            </Card>

            <div className="col-4 stack gap-16">
              <Card className="reveal reveal-d2">
                <span className="label-sm">First frame</span>
                <div className="mt-16" onClick={() => d.has_preview && setLightbox(imgUrl(d))}
                  style={{ position: 'relative', borderRadius: 'var(--r-md)', overflow: 'hidden', aspectRatio: aspect, background: 'var(--paper-2)', cursor: d.has_preview ? 'zoom-in' : 'default' }}>
                  {d.has_preview
                    ? <img src={imgUrl(d)} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain' }} />
                    : <div className={`gfill ${(busy === 'preview' || genAll) ? 'skel' : 'g' + (cur % 6)}`} style={{ position: 'absolute', inset: 0 }}></div>}
                  {d.has_preview && (
                    <span style={{ position: 'absolute', right: 8, bottom: 8, background: 'rgba(45,51,53,.72)', color: '#fff', fontSize: 11, fontWeight: 600, padding: '3px 8px', borderRadius: 6, display: 'inline-flex', alignItems: 'center', gap: 5, backdropFilter: 'blur(4px)' }}>
                      <Icon name="up-right-and-down-left-from-center" /> Full size
                    </span>
                  )}
                </div>
                <Button variant="ghost" block icon="rotate-right" disabled={busy === 'preview'} onClick={regen}>
                  {busy === 'preview' ? 'Painting…' : 'Regenerate image'}</Button>
              </Card>
              <Card well className="reveal reveal-d3">
                <div className="row center gap-10">
                  <Icon name="circle-info" style={{ color: 'var(--ink-3)' }} />
                  <span className="muted" style={{ fontSize: 12.5 }}>Edit any scene before rendering — changes here drive the final film.</span>
                </div>
              </Card>
            </div>

            <Card span={12} className="reveal reveal-d4">
              <div className="row center between">
                <span className="label-sm">All scenes</span>
                <Button variant="ghost" icon="rotate-right" disabled={genAll} onClick={regenAll}>
                  {genAll ? 'Regenerating…' : 'Regenerate all'}
                </Button>
              </div>
              <div className="scene-grid mt-16">
                {scenes.map((s, i) => (
                  <div key={s.id} className={`scene ${i === cur ? 'is-current' : ''}`} onClick={() => move(i)}>
                    <Thumb variant={i} aspect={aspect} label={String(i + 1).padStart(2, '0')} src={s.has_preview ? imgUrl(s) : null} />
                    <div className="scene__cap">{s.title || `Scene ${i + 1}`}</div>
                  </div>
                ))}
              </div>
            </Card>
          </div>

          {lightbox && (
            <div onClick={() => setLightbox(null)}
              style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.82)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24, cursor: 'zoom-out' }}>
              <img src={lightbox} alt="" style={{ maxWidth: '95%', maxHeight: '95%', objectFit: 'contain', borderRadius: 8, boxShadow: '0 24px 70px rgba(0,0,0,.6)' }} />
            </div>
          )}
        </>
      )}
    </div>
  )
}
