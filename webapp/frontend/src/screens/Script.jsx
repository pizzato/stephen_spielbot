import { useState, useEffect } from 'react'
import { Card, Field, Segmented, ResolutionPicker, Button, Chip, Icon, Thumb, Banner, RegenLabel, GuidedRegenButton, VersionStrip, InpaintModal, voiceMetaMap, voiceLabel, SceneTypeControls } from '../components.jsx'
import { api, fileUrl } from '../api.js'

// Quick-instruction presets for the "tell it how" Re-generate popovers.
const REGEN_CHIPS = {
  title: ['Shorter', 'Punchier', 'More literal'],
  narration: ['Shorten', 'Expand', 'Simpler', 'More dramatic'],
  image_prompt: ['More detail', 'Simpler', 'Wider shot'],
  video_prompt: ['More motion', 'Slower pace', 'Static camera'],
  image: ['More detail', 'Brighter', 'Different angle'],
  cover: ['Bolder', 'Simpler', 'More dramatic'],
  look: ['More detail', 'Different angle', 'Friendlier'],
}

// Shared style for the floating arrow / close controls in the enlarged-image view.
const LB_BTN = {
  position: 'absolute', zIndex: 2, border: 'none', color: '#fff',
  background: 'rgba(20,22,24,.55)', backdropFilter: 'blur(6px)',
  width: 46, height: 46, borderRadius: '50%', fontSize: 18,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
}

// Read a picked file into a base64 data-URL for upload (same shape the backend's
// image endpoints expect).
const fileToDataUrl = (file) => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(r.result)
  r.onerror = () => reject(new Error('Could not read that file.'))
  r.readAsDataURL(file)
})

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
  const [coverMsg, setCoverMsg] = useState('')
  const [coverHist, setCoverHist] = useState(null)
  const [coverEdit, setCoverEdit] = useState(false)
  const [coverEditErr, setCoverEditErr] = useState('')

  // Characters tab — per-script cast the LLM identified (lives in the work dir,
  // not the global catalogue). charBusy holds the char id currently mutating.
  const [characters, setCharacters] = useState(job?.characters || [])
  const [charBusy, setCharBusy] = useState('')
  const [charMsg, setCharMsg] = useState('')
  const [aliasDraft, setAliasDraft] = useState({})
  // Character look lightbox — { id, ver }: which character and which of its kept
  // look versions is shown full-resolution.
  const [charLightbox, setCharLightbox] = useState(null)
  // Named voices (per-character voice picker) + the global character catalogue
  // (their names are valid dialogue speakers too), loaded from config once.
  const [voiceOpts, setVoiceOpts] = useState([])
  const [voiceMeta, setVoiceMeta] = useState({})
  const [globalCast, setGlobalCast] = useState([])
  useEffect(() => {
    api.getConfig().then((c) => {
      // /api/config nests the config under "config"; voices there are
      // {name,path,gender,age,accent} rows while top-level "voices" is names.
      const cfg = c?.config || c || {}
      setVoiceOpts((cfg.voices || []).map((v) => v?.name).filter(Boolean))
      setVoiceMeta(voiceMetaMap(cfg.voices))
      setGlobalCast((cfg.characters || []).map((x) => x?.name).filter(Boolean))
    }).catch(() => {})
  }, [])

  // Scenes tab
  const [scenes, setScenes] = useState(job?.scenes || [])
  const [cur, setCur] = useState(0)
  const [lightbox, setLightbox] = useState(null)
  const [inpaint, setInpaint] = useState(false)
  const [inpaintErr, setInpaintErr] = useState('')
  const [genAll, setGenAll] = useState(false)
  const [genAllMsg, setGenAllMsg] = useState('')
  const [regenStatus, setRegenStatus] = useState('')
  const [fieldBusy, setFieldBusy] = useState('')
  const [confirmDelScript, setConfirmDelScript] = useState(false)

  // Sync state and switch to Cover when a new job loads
  useEffect(() => {
    setScenes(job?.scenes || [])
    setCharacters(job?.characters || [])
    setAliasDraft({})
    setCharMsg('')
    setCur(0)
    setStyle(job?.style || '')
    setResolution(job?.resolution || meta.config?.resolution || meta.default_resolution || '')
    setCoverTitle(job?.title || '')
    setDescription('')
    setCoverUrl('')
    setCoverMsg('')
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
        api.coverHistory(job.work_dir).then((r) => { if (alive) setCoverHist(r.history) }).catch(() => {})
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
          return u ? { ...s, preview_path: u.preview_path, has_preview: u.has_preview, history: u.history } : s
        }))
      })
      .catch((e) => setError(e.message))
      .finally(() => setGenAll(false))
  }, [job?.job_id])

  // Character look images are rendered by a background task right after the
  // script is created, so on the Characters tab keep polling briefly while any
  // character is still missing its look. Skips while the user is mid-edit.
  useEffect(() => {
    if (view !== 'characters' || !job?.job_id) return
    if (charBusy) return
    if (!characters.some((c) => !c.has_image)) return
    let alive = true
    let tries = 0
    let timer = null
    const poll = async () => {
      try {
        const r = await api.scriptCharacters(job.job_id)
        if (!alive) return
        setCharacters(r.characters || [])
        if ((r.characters || []).some((c) => !c.has_image) && tries++ < 12) timer = setTimeout(poll, 4000)
      } catch { /* best-effort */ }
    }
    timer = setTimeout(poll, 4000)
    return () => { alive = false; clearTimeout(timer) }
  }, [view, job?.job_id, characters, charBusy])

  // ── Scripts tab ──────────────────────────────────────────────────────────────
  // Drop a loaded/duplicated script into the editor (fills voice/resolution
  // defaults the render needs). The job-id effect above switches to the Cover tab.
  const applyLoaded = (loaded) => setJob({
    ...loaded,
    voice: loaded.voice || meta.config?.default_voice || '',
    voice_robotic: loaded.voice_robotic ?? !!meta.config?.default_voice_robotic,
    resolution: loaded.resolution || meta.config?.resolution || meta.default_resolution || '',
  })

  const loadScript = async (workDir) => {
    setBusy('load:' + workDir); setError('')
    try { applyLoaded(await api.loadScript(workDir)) }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }

  // Copy this script into a fresh work dir and open the copy, so rendering it
  // again produces a new film without overwriting the original.
  const duplicateScript = async (workDir) => {
    setBusy('dup:' + workDir); setError('')
    try {
      applyLoaded(await api.duplicateScript(workDir))
      await refreshScripts()
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
  const regenTitle = async (instruction = '') => {
    setYtBusy('title'); setError('')
    try {
      const r = await api.ytPostTitle(job.work_dir, coverTitle || job.title || '', instruction)
      setCoverTitle(r.title || '')
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const genDescription = async () => {
    setYtBusy('desc'); setError(''); setCoverMsg('')
    try {
      const r = await api.ytDescribe({ work_dir: job.work_dir, title: coverTitle || job.title || '' })
      setDescription(r.description || '')
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  // Persist the edited title + description back to the script so they survive a
  // reload and feed render/publish; also updates the in-memory job so Approve
  // (which sends job.title) renders with the edited title.
  const saveCover = async () => {
    setBusy('savecover'); setError(''); setCoverMsg('')
    try {
      const title = coverTitle.trim()
      await api.ytPostSave({ work_dir: job.work_dir, title, description, queue_item_id: job.queue_item_id || '' })
      setJob({ ...job, title, video_title: title })
      setCoverMsg('Title and description saved.')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const regenCover = async (instruction = '') => {
    setYtBusy('cover'); setError('')
    let pollTimer = null
    try {
      const { task_id: tid } = await api.ytCover({ work_dir: job.work_dir, title: coverTitle || job.title || '', resolution, instruction })
      await new Promise((resolve, reject) => {
        const check = async () => {
          try {
            const s = await api.ytCoverStatus(tid)
            if (s.status === 'succeeded') { setCoverUrl(s.cover_url || ''); if (s.history) setCoverHist(s.history); resolve() }
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

  // Pick a kept cover version, or masked-edit the cover with the style's edit engine.
  const selectCover = async (versionId) => {
    setYtBusy('cover'); setError('')
    try {
      const r = await api.coverSelect(job.work_dir, versionId)
      setCoverUrl(r.cover_url || ''); setCoverHist(r.history)
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const deleteCover = async (versionId) => {
    setYtBusy('cover'); setError('')
    try {
      const r = await api.coverDelete(job.work_dir, versionId)
      setCoverHist(r.history)
    } catch (e) { setError(e.message) } finally { setYtBusy('') }
  }

  const applyCoverEdit = async (mask, editPrompt, denoise) => {
    setYtBusy('coveredit'); setCoverEditErr('')
    try {
      const r = await api.coverInpaint(job.work_dir, mask, editPrompt, denoise)
      setCoverUrl(r.cover_url || ''); setCoverHist(r.history); setCoverEdit(false)
    } catch (e) { setCoverEditErr(e.message) } finally { setYtBusy('') }
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
        return u ? { ...s, preview_path: u.preview_path, has_preview: u.has_preview, history: u.history, cb: Date.now() } : s
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
  const patchScene = (patch) => setScenes((arr) => arr.map((s, i) => i === cur ? { ...s, ...patch } : s))
  const aspect = (() => { const m = /\((\d+)[×x](\d+)\)/.exec(resolution || ''); return m ? `${m[1]} / ${m[2]}` : '16 / 9' })()
  const imgUrl = (s) => (s && s.preview_path) ? fileUrl(s.preview_path) + (s.cb ? `&t=${s.cb}` : '') : ''

  // ── Lightbox (enlarged scene image) ──────────────────────────────────────────
  // `lightbox` is { scene, ver }: which scene index and which of that scene's
  // generated image versions are shown enlarged. Left/right step between scenes;
  // up/down step between the versions made for a scene; arrow keys do the same.
  const selVerIdx = (s) => {
    const vs = s?.history?.versions || []
    const i = vs.findIndex((v) => v.id === s?.history?.selected)
    return i < 0 ? 0 : i
  }
  const openLightbox = () => setLightbox({ scene: cur, ver: selVerIdx(d) })
  const lbMove = (delta) => setLightbox((lb) => {
    if (!lb) return lb
    const ns = Math.min(total - 1, Math.max(0, lb.scene + delta))
    return ns === lb.scene ? lb : { scene: ns, ver: selVerIdx(scenes[ns]) }
  })
  const lbVerMove = (delta) => setLightbox((lb) => {
    if (!lb) return lb
    const vs = scenes[lb.scene]?.history?.versions || []
    const nv = Math.min(vs.length - 1, Math.max(0, lb.ver + delta))
    return nv === lb.ver ? lb : { ...lb, ver: nv }
  })
  const lbArrow = (icon, title, disabled, onPress, pos) => (
    <button type="button" title={title} disabled={disabled}
      onClick={(e) => { e.stopPropagation(); onPress() }}
      style={{ ...LB_BTN, ...pos, opacity: disabled ? 0.28 : 1, cursor: disabled ? 'default' : 'pointer' }}>
      <Icon name={icon} />
    </button>
  )

  // Arrow keys navigate the enlarged image; Escape closes it. Active only while open.
  useEffect(() => {
    if (!lightbox) return
    const onKey = (e) => {
      const k = e.key
      if (k === 'ArrowLeft') { e.preventDefault(); lbMove(-1) }
      else if (k === 'ArrowRight') { e.preventDefault(); lbMove(1) }
      else if (k === 'ArrowUp') { e.preventDefault(); lbVerMove(-1) }
      else if (k === 'ArrowDown') { e.preventDefault(); lbVerMove(1) }
      else if (k === 'Escape') { e.preventDefault(); setLightbox(null) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [lightbox, scenes, total])

  const lbVersions = lightbox ? (scenes[lightbox.scene]?.history?.versions || []) : []
  const lbMulti = lbVersions.length > 1
  const lbSrc = lightbox
    ? (lbMulti && lbVersions[lightbox.ver] ? fileUrl(lbVersions[lightbox.ver].path) : imgUrl(scenes[lightbox.scene] || {}))
    : ''

  const regenField = async (field, instruction = '') => {
    setFieldBusy(field); setError('')
    try {
      const r = await api.regenField(job.job_id, d.id, field, {
        title: d.title || '', narration: d.narration || '',
        image_prompt: d.image_prompt || '', video_prompt: d.video_prompt || '',
        instruction,
      })
      setField(field, r.value)
    } catch (e) { setError(e.message) } finally { setFieldBusy('') }
  }

  const fieldLabel = (text, field, icon) => (
    <RegenLabel icon={icon} busy={fieldBusy === field}
      onRegen={(instr) => regenField(field, instr)} chips={REGEN_CHIPS[field]}>{text}</RegenLabel>
  )

  const persist = async (idx = cur, override = null) => {
    const s = override || scenes[idx]
    if (!s) return
    try {
      await api.saveScene(job.job_id, s.id, {
        title: s.title || '', image_prompt: s.image_prompt || '',
        video_prompt: s.video_prompt || '', narration: s.narration || '',
        mode: s.mode || 'narration', lines: s.lines || [], duration: s.duration || 0,
      })
    } catch (e) { setError(e.message) }
  }

  // Dialogue speakers: the script's own cast plus the global character catalogue
  // (both render — the resolver falls back to catalogue portraits/voices). The
  // shot editing itself lives in the shared SceneTypeControls component.
  const castOpts = [...new Set([...characters.map((c) => c.name), ...globalCast])].filter(Boolean)

  const move = async (to) => {
    if (to < 0 || to >= total) return
    await persist(cur)
    setCur(to)
  }

  const regen = async (instruction = '') => {
    setBusy('preview'); setError('')
    try {
      await persist(cur)
      const r = await api.regenPreview(job.job_id, scenes[cur].id, resolution, style, instruction)
      setScenes((arr) => arr.map((s, i) => i === cur ? { ...s, preview_path: r.preview_path, has_preview: true, history: r.history, cb: Date.now() } : s))
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const selectVersion = async (versionId) => {
    setBusy('preview'); setError('')
    try {
      const r = await api.selectPreview(job.job_id, scenes[cur].id, versionId)
      setScenes((arr) => arr.map((s, i) => i === cur ? { ...s, preview_path: r.preview_path, has_preview: true, history: r.history, cb: Date.now() } : s))
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const deleteVersion = async (versionId) => {
    setBusy('preview'); setError('')
    try {
      const r = await api.deletePreview(job.job_id, scenes[cur].id, versionId)
      setScenes((arr) => arr.map((s, i) => i === cur ? { ...s, history: r.history } : s))
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const applyInpaint = async (mask, editPrompt, denoise) => {
    setBusy('inpaint'); setInpaintErr('')
    try {
      const r = await api.inpaintScene(job.job_id, scenes[cur].id, mask, editPrompt, denoise)
      setScenes((arr) => arr.map((s, i) => i === cur ? { ...s, preview_path: r.preview_path, has_preview: true, history: r.history, cb: Date.now() } : s))
      setInpaint(false)
    } catch (e) { setInpaintErr(e.message) } finally { setBusy('') }
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
        // Approving from the Script screen means the user reviewed these scenes
        // — release it to render (auto-start) rather than parking it for review.
        approved: true,
      })
      setJob({ ...job, scenes, style })
      if (r.started) onGenerate(job.work_dir)
      else go('queue')
    } catch (e) { setError(e.message); setBusy('') }
  }

  // ── Characters tab ─────────────────────────────────────────────────────────────
  // Every mutating call returns the fresh { characters } list; charBusy locks the
  // affected card while its op runs. Field edits are local until blur (saveCharacter).
  const charOp = async (id, run) => {
    setCharBusy(id); setError(''); setCharMsg('')
    try { const r = await run(); setCharacters(r.characters || []) }
    catch (e) { setError(e.message) } finally { setCharBusy('') }
  }
  const setCharField = (id, key, val) =>
    setCharacters((arr) => arr.map((c) => (c.id === id ? { ...c, [key]: val } : c)))
  const saveCharacter = (c) => charOp(c.id, () => api.updateScriptCharacter(job.job_id, c.id, {
    name: c.name || '', aliases: c.aliases || [], description: c.description || '',
  }))
  const setCharVoice = (c, v) => { setCharField(c.id, 'voice', v); charOp(c.id, () => api.updateScriptCharacter(job.job_id, c.id, { voice: v })) }
  const addCharacter = () => charOp('add', () =>
    api.addScriptCharacter(job.job_id, { name: '', aliases: [], description: '' }))
  const removeCharacter = (c) => charOp(c.id, () => api.deleteScriptCharacter(job.job_id, c.id))
  const genCharLook = (c, instruction = '') => charOp(c.id, () => api.generateScriptCharacterPortrait(job.job_id, c.id, instruction))
  const clearCharLook = (c) => charOp(c.id, () => api.clearScriptCharacterImage(job.job_id, c.id))
  const uploadCharLook = (c, file) => file && charOp(c.id, async () =>
    api.setScriptCharacterImage(job.job_id, c.id, file.name, await fileToDataUrl(file)))
  const promoteCharacter = (c) => charOp(c.id, async () => {
    const r = await api.promoteScriptCharacter(job.job_id, c.id)
    setCharMsg(`Saved “${c.name || 'character'}” to your character catalogue.`)
    return r
  })
  const selectCharVersion = (c, versionId) =>
    charOp(c.id, () => api.selectScriptCharacterImage(job.job_id, c.id, versionId))
  const deleteCharVersion = (c, versionId) =>
    charOp(c.id, () => api.deleteScriptCharacterImage(job.job_id, c.id, versionId))

  // ── Character look lightbox ─────────────────────────────────────────────────
  // Open on the selected version; up/down (and arrow keys) flip between the looks
  // kept for that character so the user can compare them at full resolution.
  const charSelVerIdx = (c) => {
    const vs = c?.history?.versions || []
    const i = vs.findIndex((v) => v.id === c?.history?.selected)
    return i < 0 ? Math.max(0, vs.length - 1) : i
  }
  const openCharLightbox = (c) => c.has_image && setCharLightbox({ id: c.id, ver: charSelVerIdx(c) })
  const clbVerMove = (delta) => setCharLightbox((lb) => {
    if (!lb) return lb
    const vs = characters.find((x) => x.id === lb.id)?.history?.versions || []
    const nv = Math.min(vs.length - 1, Math.max(0, lb.ver + delta))
    return nv === lb.ver ? lb : { ...lb, ver: nv }
  })
  useEffect(() => {
    if (!charLightbox) return
    const onKey = (e) => {
      const k = e.key
      if (k === 'ArrowUp' || k === 'ArrowLeft') { e.preventDefault(); clbVerMove(-1) }
      else if (k === 'ArrowDown' || k === 'ArrowRight') { e.preventDefault(); clbVerMove(1) }
      else if (k === 'Escape') { e.preventDefault(); setCharLightbox(null) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [charLightbox, characters])
  const clbChar = charLightbox ? characters.find((c) => c.id === charLightbox.id) : null
  const clbVersions = clbChar?.history?.versions || []
  const clbSrc = charLightbox
    ? (clbVersions[charLightbox.ver] ? fileUrl(clbVersions[charLightbox.ver].path) : (clbChar?.image_url || ''))
    : ''
  // Aliases edit as a comma-separated string; parse to an array only on blur.
  const aliasValue = (c) => (aliasDraft[c.id] ?? (c.aliases || []).join(', '))
  const commitAliases = (c) => {
    const raw = aliasDraft[c.id]
    setAliasDraft((d) => { const n = { ...d }; delete n[c.id]; return n })
    if (raw === undefined) return
    const aliases = raw.split(',').map((s) => s.trim()).filter(Boolean)
    setCharField(c.id, 'aliases', aliases)
    charOp(c.id, () => api.updateScriptCharacter(job.job_id, c.id, {
      name: c.name || '', aliases, description: c.description || '',
    }))
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
              <Button variant="primary" icon="floppy-disk" disabled={busy === 'savecover' || !coverTitle.trim()} onClick={saveCover}>
                {busy === 'savecover' ? 'Saving…' : 'Save'}
              </Button>
              <Button variant="ghost" icon="rotate" onClick={() => {
                const b = job.create_brief || {}
                go('create', {
                  title: b.video_title || job.video_title || job.title || '',
                  description: b.topic || job.topic || '',
                  scenes: b.n_scenes || job.scenes?.length || null,
                  resolution: b.resolution || job.resolution || '',
                  styleName: b.style_name || job.style_name || '',
                  voice: b.voice || job.voice || '',
                  voice_robotic: b.voice_robotic ?? job.voice_robotic,
                  visualStyle: b.visual_style || '',
                  autoApprove: b.auto_approve,
                  queueItemId: job.queue_item_id || null,
                })
              }}>Re-draft</Button>
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
          {view === 'characters' && job && (
            <Button variant="primary" icon="user-plus" disabled={!!charBusy} onClick={addCharacter}>
              {charBusy === 'add' ? 'Adding…' : 'Add character'}
            </Button>
          )}
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {job?.queue_item_id && view === 'scenes' && <Banner tone="info">Editing a queued request — “Save to queue slot” keeps its position and lets it render straight from this script.</Banner>}
      {genAll && <Banner tone="info">{genAllMsg}</Banner>}
      {!genAll && regenStatus && <Banner tone="ok">{regenStatus}</Banner>}
      {view === 'cover' && coverMsg && <Banner tone="ok">{coverMsg}</Banner>}
      {view === 'characters' && charMsg && <Banner tone="ok">{charMsg}</Banner>}

      <div className="reveal reveal-d1" style={{ marginBottom: 20 }}>
        <Segmented value={view} onChange={(v) => { setView(v); setError('') }} options={[
          { value: 'scripts', label: 'Scripts' },
          { value: 'cover', label: 'Cover' },
          { value: 'characters', label: 'Characters' },
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
                <Button variant="ghost" icon="copy" disabled={!!busy} onClick={() => duplicateScript(s.work_dir)}>
                  {busy === 'dup:' + s.work_dir ? 'Duplicating…' : 'Duplicate'}
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
              <Field label={<RegenLabel busy={ytBusy === 'title'} disabled={!job.work_dir} onRegen={regenTitle} chips={REGEN_CHIPS.title}>Title</RegenLabel>} hint="Max 100 characters.">
                <input className="input" value={coverTitle} maxLength={100} onChange={(e) => { setCoverTitle(e.target.value); setCoverMsg('') }} />
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
                  onChange={(e) => { setDescription(e.target.value); setCoverMsg('') }}
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
              <GuidedRegenButton block variant="ghost" icon="rotate-right"
                label={coverUrl ? 'Regenerate cover' : 'Generate cover'} busyLabel="Generating…"
                busy={ytBusy === 'cover'} disabled={!!ytBusy}
                onRegen={regenCover} chips={REGEN_CHIPS.cover} />

              <Button variant="ghost" block icon="wand-magic-sparkles" disabled={!coverUrl || !!ytBusy}
                onClick={() => { setCoverEditErr(''); setCoverEdit(true) }}>Edit cover</Button>
              <VersionStrip versions={coverHist?.versions} selected={coverHist?.selected}
                onSelect={selectCover} onDelete={deleteCover} aspect={aspect} busy={ytBusy === 'cover' || ytBusy === 'coveredit'} />
            </Card>
            <Card well className="reveal reveal-d3">
              <div className="row center gap-10">
                <Icon name="circle-info" style={{ color: 'var(--ink-3)' }} />
                <span className="muted" style={{ fontSize: 12.5 }}>Click <strong>Save</strong> to keep the title and description; the cover image is saved when generated. All are reused when publishing.</span>
              </div>
            </Card>
          </div>

          {coverEdit && (
            <InpaintModal src={coverUrl} aspect={aspect} busy={ytBusy === 'coveredit'} error={coverEditErr}
              onApply={applyCoverEdit} onClose={() => setCoverEdit(false)} />
          )}
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

                <SceneTypeControls scene={d} castOpts={castOpts}
                  onChange={(patch, commit) => { patchScene(patch); if (commit) persist(cur, { ...scenes[cur], ...patch }) }}
                  onCommit={() => persist(cur)} />

                {(d.mode || 'narration') === 'narration' && (
                  <Field label={fieldLabel('Narration', 'narration', 'microphone-lines')}>
                    <textarea className="textarea" rows={4} value={d.narration || ''} onChange={(e) => setField('narration', e.target.value)} onBlur={() => persist(cur)} />
                  </Field>
                )}

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
                <div className="mt-16" onClick={() => d.has_preview && openLightbox()}
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
                <GuidedRegenButton block variant="ghost" icon="rotate-right"
                  label="Regenerate image" busyLabel="Painting…"
                  busy={busy === 'preview'} disabled={!!busy}
                  onRegen={regen} chips={REGEN_CHIPS.image} />
                <Button variant="ghost" block icon="wand-magic-sparkles" disabled={!d.has_preview || !!busy}
                  onClick={() => { setInpaintErr(''); setInpaint(true) }}>Edit image</Button>
                <VersionStrip versions={d.history?.versions} selected={d.history?.selected}
                  onSelect={selectVersion} onDelete={deleteVersion} aspect={aspect} busy={busy === 'preview' || busy === 'inpaint'} />
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
              {lbSrc
                ? <img src={lbSrc} alt="" onClick={(e) => e.stopPropagation()}
                    style={{ maxWidth: '90%', maxHeight: '90%', objectFit: 'contain', borderRadius: 8, boxShadow: '0 24px 70px rgba(0,0,0,.6)', cursor: 'default' }} />
                : <span onClick={(e) => e.stopPropagation()} style={{ color: 'rgba(255,255,255,.8)', fontSize: 14, cursor: 'default' }}>No image for this scene yet.</span>}

              {/* Scene / image counters */}
              <div onClick={(e) => e.stopPropagation()}
                style={{ position: 'absolute', top: 18, left: 22, color: 'rgba(255,255,255,.92)', fontSize: 13, fontWeight: 600, display: 'flex', gap: 10, cursor: 'default' }}>
                <span>Scene {lightbox.scene + 1} / {total}</span>
                {lbMulti && <span style={{ opacity: 0.65 }}>· Image {lightbox.ver + 1} / {lbVersions.length}</span>}
              </div>

              {/* Close */}
              <button type="button" title="Close (Esc)" onClick={(e) => { e.stopPropagation(); setLightbox(null) }}
                style={{ ...LB_BTN, top: 14, right: 16, width: 40, height: 40, fontSize: 16, cursor: 'pointer' }}>
                <Icon name="xmark" />
              </button>

              {/* Previous / next scene */}
              {total > 1 && lbArrow('chevron-left', 'Previous scene (←)', lightbox.scene <= 0,
                () => lbMove(-1), { left: 18, top: '50%', transform: 'translateY(-50%)' })}
              {total > 1 && lbArrow('chevron-right', 'Next scene (→)', lightbox.scene >= total - 1,
                () => lbMove(1), { right: 18, top: '50%', transform: 'translateY(-50%)' })}

              {/* Other images generated for this scene */}
              {lbMulti && lbArrow('chevron-up', 'Previous image for this scene (↑)', lightbox.ver <= 0,
                () => lbVerMove(-1), { top: 64, left: '50%', transform: 'translateX(-50%)' })}
              {lbMulti && lbArrow('chevron-down', 'Next image for this scene (↓)', lightbox.ver >= lbVersions.length - 1,
                () => lbVerMove(1), { bottom: 22, left: '50%', transform: 'translateX(-50%)' })}
            </div>
          )}

          {inpaint && (
            <InpaintModal src={imgUrl(d)} aspect={aspect} busy={busy === 'inpaint'} error={inpaintErr}
              onApply={applyInpaint} onClose={() => setInpaint(false)} />
          )}
        </>
      )}

      {/* ── Characters tab ───────────────────────────────────────────────────── */}
      {view === 'characters' && !job && (
        <div className="bento">
          <Card span={7} well>
            <p className="body-1" style={{ margin: 0 }}>Load a script first to manage its characters.</p>
            <div className="row gap-10 mt-16">
              <Button variant="primary" icon="folder-open" onClick={() => setView('scripts')}>Browse scripts</Button>
              <Button variant="ghost" icon="wand-magic-sparkles" onClick={() => go('create')}>Create new</Button>
            </div>
          </Card>
        </div>
      )}

      {view === 'characters' && job && (
        <div className="bento">
          <Card span={12} well className="reveal reveal-d1">
            <div className="row center gap-10">
              <Icon name="user-group" style={{ color: 'var(--ink-3)' }} />
              <span className="muted" style={{ fontSize: 12.5 }}>
                The main characters for this video. Their name and look are woven into the scene images so they
                stay consistent across the film. Edit them or accept as is — they stay with this script unless you
                <strong> save one to your catalogue</strong> to reuse in future videos.
              </span>
            </div>
          </Card>

          {characters.length === 0 && (
            <Card span={12} well className="reveal reveal-d2">
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                No recurring characters were identified for this script. Click <strong>Add character</strong> to define one.
              </p>
            </Card>
          )}

          {characters.map((c, i) => {
            const b = charBusy === c.id
            return (
              <Card key={c.id} span={6} padLg className={`reveal reveal-d${(i % 3) + 1}`}>
                <div className="row gap-16 row--wrap" style={{ alignItems: 'flex-start' }}>
                  <div style={{ width: 176, flex: '0 0 auto' }}>
                    <div onClick={() => openCharLightbox(c)}
                      style={{ position: 'relative', borderRadius: 'var(--r-md)', overflow: 'hidden', aspectRatio: '1 / 1', background: 'var(--paper-2)', cursor: c.has_image ? 'zoom-in' : 'default' }}>
                      {c.has_image
                        ? <img src={c.image_url} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
                        : <div className={`gfill ${b ? 'skel' : 'g' + (i % 6)}`} style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                            {!b && <Icon name="user" style={{ color: 'var(--ink-3)', fontSize: 26 }} />}
                          </div>}
                      {c.has_image && (
                        <span style={{ position: 'absolute', right: 8, bottom: 8, background: 'rgba(45,51,53,.72)', color: '#fff', fontSize: 11, fontWeight: 600, padding: '3px 8px', borderRadius: 6, display: 'inline-flex', alignItems: 'center', gap: 5, backdropFilter: 'blur(4px)' }}>
                          <Icon name="up-right-and-down-left-from-center" /> Full size
                        </span>
                      )}
                    </div>
                    <div className="stack gap-6 mt-10">
                      <GuidedRegenButton block size="sm" variant="ghost" icon="rotate-right"
                        label={c.has_image ? 'Regenerate look' : 'Generate look'} busyLabel="Painting…"
                        busy={b} disabled={b}
                        onRegen={(instr) => genCharLook(c, instr)} chips={REGEN_CHIPS.look} />

                      <label className="btn btn--ghost btn--sm btn--block" style={{ cursor: b ? 'default' : 'pointer' }}>
                        <Icon name="upload" /> Upload
                        <input type="file" accept="image/*" hidden disabled={b}
                          onChange={(e) => { uploadCharLook(c, e.target.files?.[0]); e.target.value = '' }} />
                      </label>
                      {c.has_image && (
                        <Button variant="quiet" size="sm" block icon="trash-can" disabled={b} onClick={() => clearCharLook(c)}>Remove look</Button>
                      )}
                    </div>
                  </div>

                  <div className="stack gap-14" style={{ flex: 1, minWidth: 200 }}>
                    <Field label="Name">
                      <input className="input" value={c.name || ''}
                        onChange={(e) => setCharField(c.id, 'name', e.target.value)}
                        onBlur={() => saveCharacter(c)} />
                    </Field>
                    <Field label="Also called" hint="Comma-separated aliases the narration may use.">
                      <input className="input" value={aliasValue(c)}
                        onChange={(e) => setAliasDraft((d) => ({ ...d, [c.id]: e.target.value }))}
                        onBlur={() => commitAliases(c)} />
                    </Field>
                    <Field label="Appearance" hint="Fixed look — drawn the same way in every scene.">
                      <textarea className="textarea" rows={4} value={c.description || ''}
                        onChange={(e) => setCharField(c.id, 'description', e.target.value)}
                        onBlur={() => saveCharacter(c)} />
                    </Field>
                    <Field label="Voice" hint="Cloned voice this character speaks with in dialogue scenes.">
                      <select className="input" value={c.voice || ''} disabled={b}
                        onChange={(e) => setCharVoice(c, e.target.value)}>
                        <option value="">Style narrator (default)</option>
                        {voiceOpts.map((v) => <option key={v} value={v}>{voiceLabel(v, voiceMeta)}</option>)}
                      </select>
                    </Field>
                    <div className="row gap-10 row--wrap">
                      <Button variant="ghost" icon="bookmark" disabled={b || !(c.name || '').trim()} onClick={() => promoteCharacter(c)}>Save to catalogue</Button>
                      <Button variant="quiet" icon="trash-can" disabled={b} onClick={() => removeCharacter(c)}>Delete</Button>
                    </div>
                  </div>
                </div>
                <VersionStrip versions={c.history?.versions} selected={c.history?.selected}
                  onSelect={(vid) => selectCharVersion(c, vid)} onDelete={(vid) => deleteCharVersion(c, vid)}
                  aspect="1 / 1" busy={b} />
              </Card>
            )
          })}

          {charLightbox && (
            <div onClick={() => setCharLightbox(null)}
              style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.82)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24, cursor: 'zoom-out' }}>
              {clbSrc
                ? <img src={clbSrc} alt="" onClick={(e) => e.stopPropagation()}
                    style={{ maxWidth: '90%', maxHeight: '90%', objectFit: 'contain', borderRadius: 8, boxShadow: '0 24px 70px rgba(0,0,0,.6)', cursor: 'default' }} />
                : <span onClick={(e) => e.stopPropagation()} style={{ color: 'rgba(255,255,255,.8)', fontSize: 14, cursor: 'default' }}>No look for this character yet.</span>}

              {/* Character name / look counter */}
              <div onClick={(e) => e.stopPropagation()}
                style={{ position: 'absolute', top: 18, left: 22, color: 'rgba(255,255,255,.92)', fontSize: 13, fontWeight: 600, display: 'flex', gap: 10, cursor: 'default' }}>
                <span>{clbChar?.name || 'Character'}</span>
                {clbVersions.length > 1 && <span style={{ opacity: 0.65 }}>· Look {charLightbox.ver + 1} / {clbVersions.length}</span>}
              </div>

              {/* Close */}
              <button type="button" title="Close (Esc)" onClick={(e) => { e.stopPropagation(); setCharLightbox(null) }}
                style={{ ...LB_BTN, top: 14, right: 16, width: 40, height: 40, fontSize: 16, cursor: 'pointer' }}>
                <Icon name="xmark" />
              </button>

              {/* Flip between kept looks for this character */}
              {clbVersions.length > 1 && lbArrow('chevron-left', 'Previous look (←)', charLightbox.ver <= 0,
                () => clbVerMove(-1), { left: 18, top: '50%', transform: 'translateY(-50%)' })}
              {clbVersions.length > 1 && lbArrow('chevron-right', 'Next look (→)', charLightbox.ver >= clbVersions.length - 1,
                () => clbVerMove(1), { right: 18, top: '50%', transform: 'translateY(-50%)' })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
